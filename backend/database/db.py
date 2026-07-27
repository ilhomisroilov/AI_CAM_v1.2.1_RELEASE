"""
============================================================
db.py  —  SQLite ma'lumotlar bazasi qatlami
============================================================
Jadval: vin_records
  id                    INTEGER PRIMARY KEY
  timestamp             TEXT (ISO 8601)
  detected_vin          TEXT   (VALIDATED VIN — strukturaga ko'ra saralangan)
  raw_vin               TEXT   (XOM OCR natijasi — audit/traceability uchun)
  model                 TEXT   (QY | BL7M — VIN prefiksidan)
  confidence            REAL   (final_score)
  image_path            TEXT   (kesilgan VIN rasmi)
  session_id            TEXT   (globally unique — UUID/monotonic, P1 audit fix)
  trigger_sequence      INTEGER (PLC trigger tartib raqami — restart bo'yicha ham o'sadi)
  session_started_at    TEXT
  session_finalized_at  TEXT
  failure_reason        TEXT   (masalan RFID_BUSY, WATCHDOG_ERROR, RESTART_RECOVERY)

Muhim (P1 audit — "DB avtomatik migrate bo'lmasin"):
  * Eski (session_id ustuni yo'q) bazalar UCHUN `init_db()` HECH QACHON jim
    ALTER QILMAYDI. Buning o'rniga ANIQ RuntimeError ko'taradi va operatorni
    `tools/migrate_session_id.py` ni ishga tushirishga yo'naltiradi.
  * `raw_vin/model/status/rfid_epc/rfid_raw` ustunlari (session_id dan OLDINGI
    eski migratsiya) — mavjud (ishlayotgan) xatti-harakat sifatida saqlanadi
    (idempotent auto-ALTER), chunki bu P1 audit topilmasining predmeti EMAS.

Restart-recovery (P1 — "restart paytida hatto TIMEOUT ham yozilmaydi"):
  * Sessiya BOSHLANGANDA `insert_pending_session()` PENDING qator yozadi.
  * Sessiya YAKUNLANGANDA `finalize_session_record()` shu QATORNI UPDATE qiladi
    (yangi INSERT emas — session_id UNIQUE bo'lgani uchun ikkinchi yozuv
    fizik jihatdan mumkin emas).
  * Agar ilova finalize'dan OLDIN qulasa — qator PENDING holida qoladi.
    `init_db()` har safar ishga tushganda `recover_incomplete_sessions()` ni
    chaqiradi — PENDING qatorlarni FAILED (failure_reason=RESTART_RECOVERY)
    qilib yopadi (hech qachon "hech narsa yozilmagan" holat bo'lmaydi).

Thread-safe: har bir amal o'z ulanishini ochadi (fon threadlaridan xavfsiz).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import DB_PATH
from ..logger import log

_lock = threading.Lock()

# Test/tooling uchun DB yo'lini almashtirish (production kodida ISHLATILMAYDI).
# Faqat `tmp_path` asosidagi pytest testlari yoki migration skripti undan
# foydalanishi mumkin — production `data/ai_cam.db` bunda ISHTIROK ETMAYDI.
_DB_PATH_OVERRIDE: Optional[Path] = None


def set_db_path(path) -> None:
    """Joriy DB yo'lini vaqtincha almashtiradi (testlar uchun). None -> standart (config.DB_PATH)."""
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = Path(path) if path is not None else None


def current_db_path() -> Path:
    return _DB_PATH_OVERRIDE if _DB_PATH_OVERRIDE is not None else DB_PATH


# Jadval ustunlari (tartibda) — SELECT/eksport uchun
# rfid_epc = ajratilgan RFID raqami (1000..9999); rfid_raw = to'liq xom EPC (audit)
COLUMNS = ["id", "timestamp", "detected_vin", "raw_vin", "model", "rfid_epc",
           "rfid_raw", "confidence", "status", "image_path",
           "session_id", "trigger_sequence", "session_started_at",
           "session_finalized_at", "failure_reason"]

# session_id integrity ustunlari — P1 audit fix. Bu ustunlar YO'Q bo'lsa
# `init_db()` jim ALTER QILMAYDI (pastga qarang: _validate_session_schema).
_SESSION_COLUMNS = {"session_id", "trigger_sequence", "session_started_at",
                    "session_finalized_at", "failure_reason"}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(current_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_columns(conn) -> set:
    rows = conn.execute("PRAGMA table_info(vin_records)").fetchall()
    return {r[1] for r in rows}


def _migrate_legacy_columns(conn) -> None:
    """
    Eski bazaga (session_id dan OLDINGI davr) yangi ustunlarni qo'shadi
    (idempotent). Bu P1 audit topilmasining predmeti EMAS — mavjud ishlayotgan
    xatti-harakat sifatida saqlanadi. session_id/trigger_sequence/... ustunlari
    BU YERDA QO'SHILMAYDI — ular uchun _validate_session_schema() qat'iy talab
    qo'yadi (jim ALTER YO'Q).
    """
    cols = _existing_columns(conn)
    if "raw_vin" not in cols:
        conn.execute("ALTER TABLE vin_records ADD COLUMN raw_vin TEXT")
        log.info("DB migratsiya: 'raw_vin' ustuni qo'shildi.")
    if "model" not in cols:
        conn.execute("ALTER TABLE vin_records ADD COLUMN model TEXT")
        log.info("DB migratsiya: 'model' ustuni qo'shildi.")
    if "status" not in cols:
        conn.execute("ALTER TABLE vin_records ADD COLUMN status TEXT")
        log.info("DB migratsiya: 'status' ustuni qo'shildi.")
    if "rfid_epc" not in cols:
        conn.execute("ALTER TABLE vin_records ADD COLUMN rfid_epc TEXT")
        log.info("DB migratsiya: 'rfid_epc' ustuni qo'shildi.")
    if "rfid_raw" not in cols:
        conn.execute("ALTER TABLE vin_records ADD COLUMN rfid_raw TEXT")
        log.info("DB migratsiya: 'rfid_raw' ustuni qo'shildi.")
    # --- Rapid MVP: D2222/D2223 traceability + latency (ADDITIVE observability;
    # integrity ustuni EMAS -> auto-ALTER xavfsiz, RuntimeError kerak emas). ---
    for col, decl in (("d2222_timestamp", "TEXT"), ("d2223_timestamp", "TEXT"),
                      ("ocr_latency_ms", "REAL"), ("rfid_latency_ms", "REAL"),
                      ("total_session_ms", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE vin_records ADD COLUMN {col} {decl}")
            log.info(f"DB migratsiya: '{col}' ustuni qo'shildi.")


def _validate_session_schema(conn) -> None:
    """
    P1 audit fix: session_id (+ hamrohlari) ustunlari YO'Q bo'lsa jim ALTER
    QILINMAYDI — ANIQ xato ko'taradi va operatorni migration skriptga
    yo'naltiradi. Jadval umuman mavjud bo'lmasa (yangi DB) — bu funksiya
    hech narsa qilmaydi, `init_db()` CREATE TABLE bilan to'g'ridan-to'g'ri
    to'liq (yangi) sxemani yaratadi.
    """
    cols = _existing_columns(conn)
    if not cols:
        return   # jadval hali yo'q — CREATE TABLE to'liq sxema bilan yaratadi
    missing = _SESSION_COLUMNS - cols
    if missing:
        raise RuntimeError(
            "DB SXEMASI ESKIRGAN: 'vin_records' jadvalida session-integrity "
            f"ustunlari yo'q: {sorted(missing)}. Bu P1 xavfsizlik nazorati — "
            "ilova bunday holatda jim ALTER QILMAYDI (production ma'lumotlarini "
            "buzish xavfi). Talab qilinadigan qadamlar:\n"
            "  1) DB faylini zaxiralang (backup).\n"
            "  2) `python tools/migrate_session_id.py --db <db_yo'li> --yes` ishga tushiring.\n"
            "  3) Ilovani qayta ishga tushiring.\n"
            f"DB yo'li: {current_db_path()}"
        )


def init_db() -> None:
    """
    Bazani va jadvalni yaratadi. Mavjud (eski) baza bo'lsa — session-integrity
    sxemasi tekshiriladi (auto-ALTER YO'Q, faqat aniq xato). Startupda,
    tugallanmagan (PENDING) sessiyalar restart-recovery siyosati bo'yicha
    FAILED qilib yopiladi (P1 — "restart paytida hech narsa yozilmaydi" fix).
    """
    with _lock, closing(_connect()) as conn, conn:
        _validate_session_schema(conn)     # eski DB uchun jim ALTER emas — aniq xato
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vin_records (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp            TEXT    NOT NULL,
                detected_vin         TEXT    NOT NULL,
                raw_vin              TEXT,
                model                TEXT,
                rfid_epc             TEXT,
                rfid_raw             TEXT,
                confidence           REAL    NOT NULL,
                status               TEXT,
                image_path           TEXT,
                session_id           TEXT    NOT NULL,
                trigger_sequence     INTEGER,
                session_started_at   TEXT,
                session_finalized_at TEXT,
                failure_reason       TEXT,
                d2222_timestamp      TEXT,
                d2223_timestamp      TEXT,
                ocr_latency_ms       REAL,
                rfid_latency_ms      REAL,
                total_session_ms     REAL,
                UNIQUE(session_id)
            )
            """
        )
        _migrate_legacy_columns(conn)      # eski (session_id dan oldingi) ustunlar — mavjud xatti-harakat
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vin_ts ON vin_records(timestamp)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vin_session_id ON vin_records(session_id)")
    log.info(f"Ma'lumotlar bazasi tayyor: {current_db_path()}")

    try:
        recovered = recover_incomplete_sessions()
        if recovered:
            log.warning(f"[RESTART RECOVERY] {recovered} ta tugallanmagan (PENDING) sessiya "
                        "FAILED sifatida yopildi (oldingi jarayon RAM holatini yo'qotgan).")
    except Exception as exc:
        log.error(f"Restart-recovery xatosi: {exc}")


# ===================================================================
# Sessiya bilan bog'liq yozuv (P0-2 / P1 fix): INSERT-PENDING + UPDATE-FINALIZE
# ===================================================================
def insert_pending_session(session_id: str, trigger_sequence: Optional[int],
                            session_started_at: str) -> int:
    """
    Sessiya BOSHLANGANDA chaqiriladi. PENDING placeholder qator yozadi —
    agar ilova finalize'dan OLDIN qulasa, bu qator DB'da PENDING holida qoladi
    va keyingi ishga tushirishda `recover_incomplete_sessions()` uni FAILED
    qiladi (restart-recovery, "hatto TIMEOUT ham yozilmaydi" muammosi fix).

    session_id UNIQUE bo'lgani uchun bir xil session_id bilan ikkinchi chaqiruv
    sqlite3.IntegrityError ko'taradi (bitta sessiya = bitta qator kafolati).
    """
    if not session_id:
        raise ValueError("insert_pending_session: session_id majburiy.")
    with _lock, closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO vin_records "
            "(timestamp, detected_vin, confidence, status, image_path, "
            " session_id, trigger_sequence, session_started_at) "
            "VALUES (?, 'PENDING', 0.0, 'PENDING', NULL, ?, ?, ?)",
            (session_started_at, session_id, trigger_sequence, session_started_at),
        )
        return int(cur.lastrowid)


def finalize_session_record(session_id: str, *, timestamp: str, vin: str,
                             confidence: float, image_path: Optional[str],
                             raw_vin: Optional[str] = None,
                             model: Optional[str] = None,
                             status: str = "OK",
                             rfid_epc: Optional[str] = None,
                             rfid_raw: Optional[str] = None,
                             session_finalized_at: Optional[str] = None,
                             failure_reason: Optional[str] = None,
                             d2222_timestamp: Optional[str] = None,
                             d2223_timestamp: Optional[str] = None,
                             ocr_latency_ms: Optional[float] = None,
                             rfid_latency_ms: Optional[float] = None,
                             total_session_ms: Optional[float] = None) -> int:
    """
    Sessiya YAKUNLANGANDA chaqiriladi: `insert_pending_session()` yozgan
    PENDING qatorni yakuniy natija bilan UPDATE qiladi (yangi INSERT EMAS).
    Shu sababli bitta sessiya uchun DB'da HAR DOIM roppa-rosa bitta qator
    bo'ladi — hatto ikki marta finalize chaqirilsa ham (P0-2 ikkinchi yozuv fix).

    Agar PENDING qator topilmasa (masalan eski/legacy chaqiruv yoki test) —
    xavfsiz zaxira sifatida yangi qator INSERT qilinadi.
    """
    if not session_id:
        raise ValueError("finalize_session_record: session_id majburiy.")
    with _lock, closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE vin_records SET timestamp=?, detected_vin=?, raw_vin=?, model=?, "
            "rfid_epc=?, rfid_raw=?, confidence=?, status=?, image_path=?, "
            "session_finalized_at=?, failure_reason=?, d2222_timestamp=?, "
            "d2223_timestamp=?, ocr_latency_ms=?, rfid_latency_ms=?, total_session_ms=? "
            "WHERE session_id=?",
            (timestamp, vin, raw_vin, model, rfid_epc, rfid_raw, confidence, status,
             image_path, session_finalized_at, failure_reason, d2222_timestamp,
             d2223_timestamp, ocr_latency_ms, rfid_latency_ms, total_session_ms, session_id),
        )
        if cur.rowcount == 0:
            cur = conn.execute(
                "INSERT INTO vin_records "
                "(timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw, "
                " confidence, status, image_path, session_id, session_finalized_at, "
                " failure_reason, d2222_timestamp, d2223_timestamp, ocr_latency_ms, "
                " rfid_latency_ms, total_session_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (timestamp, vin, raw_vin, model, rfid_epc, rfid_raw, confidence, status,
                 image_path, session_id, session_finalized_at, failure_reason,
                 d2222_timestamp, d2223_timestamp, ocr_latency_ms, rfid_latency_ms,
                 total_session_ms),
            )
        return int(cur.lastrowid)


def recover_incomplete_sessions(reason: str = "RESTART_RECOVERY") -> int:
    """
    Restart-recovery siyosati: ilova ishga tushganda (`init_db()` ichidan)
    chaqiriladi. Oldingi jarayon finalize qilishga ULGURMAGAN (PENDING holatda
    qolgan) sessiyalarni FAILED holatiga o'tkazadi (failure_reason=RESTART_RECOVERY).

    Qaror asosi: sessiya RAM holati restart paytida albatta yo'qoladi — VIN/RFID
    progressini "tiklash" mumkin emas (tarmoq holati yo'q). Shu sababli yagona
    xavfsiz siyosat — PENDING qatorni ANIQ FAILED deb belgilash (jim tashlab
    qo'yish EMAS), operator/audit buni keyinchalik ko'rishi uchun.

    Qaytaradi: yangilangan (recover qilingan) qatorlar soni.
    """
    ts_iso = _now_iso()
    with _lock, closing(_connect()) as conn, conn:
        cols = _existing_columns(conn)
        if "status" not in cols or "session_id" not in cols:
            return 0     # eski sxema — validate_session_schema allaqachon xato ko'targan bo'lardi
        cur = conn.execute(
            "UPDATE vin_records SET status='FAILED', failure_reason=?, "
            "session_finalized_at=? WHERE status='PENDING'",
            (reason, ts_iso),
        )
        return cur.rowcount


def insert_record(timestamp: str, vin: str, confidence: float,
                  image_path: Optional[str], *,
                  session_id: str,
                  raw_vin: Optional[str] = None,
                  model: Optional[str] = None,
                  status: str = "OK",
                  rfid_epc: Optional[str] = None,
                  rfid_raw: Optional[str] = None,
                  trigger_sequence: Optional[int] = None,
                  session_started_at: Optional[str] = None,
                  session_finalized_at: Optional[str] = None,
                  failure_reason: Optional[str] = None) -> int:
    """
    To'g'ridan-to'g'ri (bir bosqichli) yozuv — PLC sessiyasidan TASHQARI
    (qo'lda Start) darhol yozish uchun (`Pipeline._legacy_write_record`).
    PLC-sessiya oqimi endi `insert_pending_session` + `finalize_session_record`
    juftligidan foydalanadi (restart-recovery + idempotent finalize uchun).

      vin       -> validated (saralangan) VIN
      raw_vin   -> xom OCR natijasi (audit)
      model     -> QY | BL7M (VIN prefiksidan)
      rfid_epc  -> ajratilgan RFID raqami (1000..9999)
      rfid_raw  -> to'liq xom EPC (audit/traceability)
      status    -> ishlov berish holati (OK / SUCCESS / TIMEOUT / FAILED / ...)
      session_id -> MAJBURIY, globally unique (UNIQUE constraint — P1 audit fix)
    """
    if not session_id:
        raise ValueError("insert_record: session_id majburiy (bo'sh bo'lishi mumkin emas) — "
                          "P1 audit: bitta sessiya = bitta yozuv (UNIQUE constraint).")
    with _lock, closing(_connect()) as conn, conn:
        try:
            cur = conn.execute(
                "INSERT INTO vin_records "
                "(timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw, "
                " confidence, status, image_path, session_id, trigger_sequence, "
                " session_started_at, session_finalized_at, failure_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, vin, raw_vin, model, rfid_epc, rfid_raw,
                 confidence, status, image_path, session_id, trigger_sequence,
                 session_started_at, session_finalized_at, failure_reason),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # UNIQUE constraint fizik dublikatni bloklaydi. Shu session qayta
            # yetkazilganda caller'ni yiqitmaymiz: mavjud row ID ni qaytarish
            # insert API'ni idempotent qiladi (late callback/retry uchun muhim).
            row = conn.execute(
                "SELECT id FROM vin_records WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                log.warning(
                    f"DB: session_id={session_id!r} allaqachon yozilgan "
                    f"(qator #{row[0]}) — duplicate insert bloklandi (idempotent)."
                )
                return int(row[0])
            raise


def get_records(limit: int = 500, order: str = "DESC",
                sort_by: str = "timestamp",
                vin: Optional[str] = None,
                start: Optional[str] = None,
                end: Optional[str] = None,
                epc: Optional[str] = None,
                model: Optional[str] = None,
                min_score: Optional[float] = None) -> List[Dict]:
    """
    Yozuvlarni qaytaradi (saralash + filtr bilan). /history sahifasi uchun.
      vin       -> VIN bo'yicha qidiruv (qisman; validated yoki xom OCR ichida)
      epc       -> RFID_EPC/raw bo'yicha qidiruv (qisman)
      model     -> aniq model (QY/BL7M)
      min_score -> confidence shu qiymatdan katta/teng
      start/end -> vaqt oralig'i ('YYYY-MM-DD HH:MM:SS' yoki 'YYYY-MM-DD')
    """
    sort_col = sort_by if sort_by in {"timestamp", "detected_vin", "confidence", "id", "model"} else "timestamp"
    direction = "DESC" if order.upper() == "DESC" else "ASC"

    where: List[str] = []
    params: List = []
    if vin:
        where.append("(UPPER(detected_vin) LIKE ? OR UPPER(IFNULL(raw_vin,'')) LIKE ?)")
        like = f"%{vin.strip().upper()}%"
        params += [like, like]
    if epc:
        where.append("(UPPER(IFNULL(rfid_epc,'')) LIKE ? OR UPPER(IFNULL(rfid_raw,'')) LIKE ?)")
        elike = f"%{epc.strip().upper()}%"
        params += [elike, elike]
    if model:
        where.append("UPPER(IFNULL(model,'')) = ?")
        params.append(model.strip().upper())
    if min_score is not None:
        where.append("confidence >= ?")
        params.append(float(min_score))
    if start:
        where.append("timestamp >= ?")
        params.append(start.strip())
    if end:
        where.append("timestamp <= ?")
        params.append(end.strip())
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with _lock, closing(_connect()) as conn, conn:
        rows = conn.execute(
            f"SELECT id, timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw, "
            f"confidence, status, image_path, session_id, trigger_sequence, "
            f"session_started_at, session_finalized_at, failure_reason, "
            f"d2222_timestamp, d2223_timestamp, ocr_latency_ms, rfid_latency_ms, "
            f"total_session_ms "
            f"FROM vin_records{where_sql} ORDER BY {sort_col} {direction} LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_records(vin: Optional[str] = None,
                    start: Optional[str] = None,
                    end: Optional[str] = None,
                    epc: Optional[str] = None,
                    model: Optional[str] = None,
                    min_score: Optional[float] = None) -> List[Dict]:
    """Eksport uchun barcha (yoki filtrlangan) yozuvlar."""
    return get_records(limit=1_000_000, order="DESC", vin=vin, start=start, end=end,
                       epc=epc, model=model, min_score=min_score)


def count_records() -> int:
    with _lock, closing(_connect()) as conn, conn:
        return int(conn.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0])
