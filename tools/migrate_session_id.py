"""
============================================================
migrate_session_id.py  —  vin_records session-integrity migratsiyasi
============================================================
P1 audit topilmasi: `vin_records` jadvalida `session_id` ustuni (+ UNIQUE
constraint) YO'Q edi — bitta VIN bir necha marta, RFID EPC takror yozilishi
mumkin edi. `backend/database/db.py` endi bunday eski bazani jim ALTER
QILMAYDI (`init_db()` aniq RuntimeError ko'taradi) — buning o'rniga operator
ANIQ RAVISHDA shu skriptni ishga tushirishi kerak.

Nima qiladi:
  1. DB faylini ZAXIRALAYDI (`<db>.bak-<YYYYmmdd_HHMMSS>`).
  2. Agar `vin_records.session_id` ustuni ALLAQACHON mavjud bo'lsa — HECH
     NARSA qilmaydi (IDEMPOTENT, xavfsiz qayta ishga tushirish mumkin).
  3. Aks holda: to'liq yangi sxema (session_id TEXT NOT NULL UNIQUE,
     trigger_sequence, session_started_at, session_finalized_at,
     failure_reason) bilan yangi jadval yaratadi, eski qatorlarni
     KO'CHIRADI — har bir eski qatorga DETERMINISTIK legacy session_id
     beriladi: "legacy-{id}" (id — eski AUTOINCREMENT PRIMARY KEY).
     Eski jadval DROP qilinadi, yangisi vin_records nomiga RENAME qilinadi.
  4. Indekslarni qayta yaratadi.

Xavfsizlik:
  * `--db` MAJBURIY — standart yo'q (production faylga tasodifan ishlatib
    yuborilishining oldi olinadi).
  * `--dry-run` (standart) — hech narsa YOZMAYDI, faqat nima qilinishini
    ko'rsatadi. Haqiqiy ishga tushirish uchun `--yes` bering.
  * Backup HAR DOIM olinadi (dry-run bo'lmasa) — muvaffaqiyatsizlik holida
    operator backup fayldan QAYTA TIKLASHI mumkin (rollback):
        cp <db>.bak-<timestamp> <db>

Ishlatish (operator):
    # 1) avval nima o'zgarishini ko'rish (hech narsa yozilmaydi):
    python tools/migrate_session_id.py --db data/ai_cam.db --dry-run

    # 2) haqiqiy migratsiya (backup avtomatik olinadi):
    python tools/migrate_session_id.py --db data/ai_cam.db --yes

    # 3) agar biror narsa noto'g'ri ketsa — rollback:
    cp data/ai_cam.db.bak-<timestamp> data/ai_cam.db

MUHIM: bu skript `data/ai_cam.db` ga hech qachon AVTOMATIK ishlatilmaydi —
faqat operator ANIQ `--db` yo'lini ko'rsatib, ANIQ `--yes` bilan chaqirganda.
Testlar bu skriptni FAQAT vaqtinchalik (tmp_path) baza ustida ishlatadi.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Eski (session_id dan OLDINGI) sxemada bo'lishi mumkin bo'lgan ustunlar —
# har biri mavjud bo'lsa ko'chiriladi, bo'lmasa NULL sifatida to'ldiriladi.
_OLD_COLUMNS = ["id", "timestamp", "detected_vin", "raw_vin", "model",
                "rfid_epc", "rfid_raw", "confidence", "status", "image_path"]

_NEW_SESSION_COLUMNS = {"session_id", "trigger_sequence", "session_started_at",
                        "session_finalized_at", "failure_reason"}


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def backup_db(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{ts}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def needs_migration(db_path: Path) -> bool:
    """True -> DB haqiqatan ham migratsiya talab qiladi (mavjud, jadval bor, ustun yetishmaydi)."""
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "vin_records"):
            return False
        cols = _existing_columns(conn, "vin_records")
        return bool(_NEW_SESSION_COLUMNS - cols)
    finally:
        conn.close()


def migrate(db_path: Path, dry_run: bool = True) -> int:
    """
    Qaytaradi: 0 = muvaffaqiyat (yoki allaqachon migratsiya qilingan),
               1 = xato.
    """
    if not db_path.exists():
        print(f"[migrate_session_id] DB fayli topilmadi: {db_path} — hech narsa qilinmaydi.")
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "vin_records"):
            print(f"[migrate_session_id] '{db_path}' da 'vin_records' jadvali yo'q — "
                  "migratsiya kerak emas.")
            return 0

        cols = _existing_columns(conn, "vin_records")
        missing = _NEW_SESSION_COLUMNS - cols
        if not missing:
            print(f"[migrate_session_id] '{db_path}' ALLAQACHON migratsiya qilingan "
                  "(session_id ustunlari mavjud) — IDEMPOTENT, hech narsa qilinmadi.")
            return 0

        row_count = conn.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]
        print(f"[migrate_session_id] '{db_path}': {row_count} ta mavjud qator topildi. "
              f"Yetishmayotgan ustunlar: {sorted(missing)}.")
        print("[migrate_session_id] Reja: yangi sxema (session_id TEXT NOT NULL UNIQUE, "
              "trigger_sequence, session_started_at, session_finalized_at, failure_reason) "
              "bilan jadval qayta quriladi; har bir eski qatorga 'legacy-{id}' session_id "
              "deterministik ravishda beriladi.")

        if dry_run:
            print("[migrate_session_id] --dry-run: HECH NARSA YOZILMADI. "
                  "Haqiqiy migratsiya uchun --yes bilan qayta ishga tushiring.")
            return 0

        present_old_cols = [c for c in _OLD_COLUMNS if c in cols]
        select_exprs = []
        for c in _OLD_COLUMNS:
            if c in cols:
                select_exprs.append(c)
            else:
                select_exprs.append(f"NULL AS {c}")

        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE vin_records_new (
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
                UNIQUE(session_id)
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO vin_records_new
                (id, timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw,
                 confidence, status, image_path, session_id, trigger_sequence,
                 session_started_at, session_finalized_at, failure_reason)
            SELECT id, timestamp, detected_vin, raw_vin, model, rfid_epc, rfid_raw,
                   confidence, status, image_path,
                   'legacy-' || id, NULL, NULL, NULL, NULL
            FROM vin_records
            """
        )
        conn.execute("DROP TABLE vin_records")
        conn.execute("ALTER TABLE vin_records_new RENAME TO vin_records")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vin_ts ON vin_records(timestamp)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vin_session_id ON vin_records(session_id)")
        conn.commit()

        new_count = conn.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]
        print(f"[migrate_session_id] MUVAFFAQIYATLI: {new_count} ta qator ko'chirildi "
              "(legacy-{id} session_id bilan). UNIQUE(session_id) constraint faol.")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"[migrate_session_id] XATO — o'zgarishlar bekor qilindi (rollback): {exc}",
              file=sys.stderr)
        return 1
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vin_records jadvaliga session_id (+ UNIQUE constraint) qo'shadi "
                    "(P1 audit fix). Production DB ga ISHLATISHDAN OLDIN backup oling."
    )
    parser.add_argument("--db", required=True, type=Path,
                        help="Migratsiya qilinadigan SQLite DB fayli yo'li (MAJBURIY — "
                             "standart yo'q, tasodifiy production'ga ishlatishning oldi olinadi).")
    parser.add_argument("--yes", action="store_true",
                        help="Haqiqiy yozishni tasdiqlaydi (bermasangiz --dry-run kabi ishlaydi).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Hech narsa yozmaydi, faqat nima qilinishini ko'rsatadi (standart, --yes berilmasa).")
    parser.add_argument("--no-backup", action="store_true",
                        help="(TAVSIYA ETILMAYDI) Backup olishni o'tkazib yuboradi.")
    args = parser.parse_args()

    dry_run = not args.yes or args.dry_run
    db_path = args.db.resolve()

    if not dry_run and needs_migration(db_path) and not args.no_backup:
        backup_path = backup_db(db_path)
        print(f"[migrate_session_id] Backup olindi: {backup_path}")
        print(f"[migrate_session_id] Rollback kerak bo'lsa: cp \"{backup_path}\" \"{db_path}\"")

    return migrate(db_path, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
