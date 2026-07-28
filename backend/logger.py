"""
============================================================
logger.py  —  Real-time, kategoriyalangan logging tizimi
============================================================
Vazifalar:
  1. Diskka aylanuvchi (rotating) per-source log fayllar (10MB x 30):
       logs/system.log  (HAMMASI)
       logs/plc.log     (PLC)
       logs/rfid.log    (RFID)
       logs/camera.log  (CAMERA)
       logs/ocr.log     (OCR/AI)
       logs/errors.log  (ERROR/CRITICAL)
  2. Web UI uchun xotirada oxirgi N ta logni saqlash (thread-safe ring buffer)
     — har bir yozuvga MANBA (source) va daraja (level) biriktiriladi.
  3. Kumulyativ statistik hisoblagichlar (error/warning/plc reconnect/rfid read/
     ocr success/camera disconnect) — System Logs sahifasi uchun.

Manba (source) aniqlash: avval xabardagi [TAG] prefiks, so'ng chaqiruvchi modul.
Mavjud API (log, ui_handler, get_since, snapshot) ORQAGA MOS — eski kod buzilmaydi.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from .config import LOGS_DIR, OPERATIONS

_MAX_UI_LOGS = 1000          # UI ring-buffer hajmi (yengil, lekin kengroq tarix)

# Manbalar (ERROR — daraja bo'yicha alohida chelak, funksional manba emas)
SOURCES = ["SYSTEM", "PLC", "RFID", "CAMERA", "OCR", "API", "AUTH", "DATABASE"]

# ---- xabar [TAG] -> manba ----
_TAG_SOURCE = {
    "PLC": "PLC", "MELSEC": "PLC",
    "RFID": "RFID", "R700": "RFID", "TAG": "RFID", "EPC": "RFID",
    "CAMERA": "CAMERA", "BLOB": "CAMERA", "COLA": "CAMERA", "LECTOR": "CAMERA",
    "OCR": "OCR", "VIN": "OCR", "TRIGGER": "OCR", "YOLO": "OCR", "DETECT": "OCR",
    "DB": "DATABASE", "DATA": "DATABASE",
    "AUTH": "AUTH", "LOGIN": "AUTH",
    "API": "API",
    "SESSION": "SYSTEM", "INIT": "SYSTEM", "ALARM": "SYSTEM",
    "COMPLETE": "SYSTEM", "ARRIVE": "SYSTEM", "AUTOCOMPLETE": "SYSTEM",
}

# ---- chaqiruvchi modul -> manba (TAG topilmasa) ----
_MODULE_SOURCE = {
    "plc_melsec": "PLC", "plc_service": "PLC", "plc_simulator": "PLC", "plc_base": "PLC",
    "rfid_service": "RFID", "rfid_reader": "RFID", "r700_client": "RFID",
    "r700_stream": "RFID", "epc_extract": "RFID", "tag_cache": "RFID",
    "camera_client": "CAMERA",
    "ocr_worker": "OCR", "detector": "OCR", "vin_postprocess": "OCR",
    "vin_rules": "OCR", "crop_quality": "OCR", "dataset_collector": "OCR",
    "server": "API", "auth": "AUTH", "db": "DATABASE", "settings_store": "API",
}

_TAG_RE = re.compile(r"\[([A-Z/ ]{2,16})\]")

# ---- kumulyativ hisoblagich naqshlari (xabar bo'yicha) ----
_PAT_PLC_RECONNECT = re.compile(r"PLC ulandi", re.I)
_PAT_RFID_READ = re.compile(r"RFID o'qildi|teg topildi|RFID_EPC=", re.I)
_PAT_OCR_SUCCESS = re.compile(r"VIN aniqlandi", re.I)
_PAT_CAM_DISCONNECT = re.compile(r"Kamera uzildi|kamera.*uzil", re.I)


class _SecretSanitizer(logging.Filter):
    """Credential/tokenlarni disk, console va UI handlerlardan oldin maskalaydi."""

    _CHECK_PASSWORD_RE = re.compile(
        r"(?i)(\bsMN\s+CheckPassword\s+\S+\s+)(\S+)"
    )
    _KEY_VALUE_RE = re.compile(
        r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    )
    _AUTH_RE = re.compile(
        r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+"
    )
    _COOKIE_RE = re.compile(r"(?i)(\bCookie\s*:\s*)[^\r\n]+")

    @classmethod
    def _known_secrets(cls) -> List[str]:
        """Runtime configdagi sirlarni oladi (test/UI orqali o'zgarsa ham aktual)."""
        try:
            from . import config as config_mod
        except Exception:
            return []
        values: List[str] = []
        for section_name in ("CAMERA", "RFID", "AUTH"):
            section = getattr(config_mod, section_name, None)
            if section is None:
                continue
            for attr in ("password", "operator_password", "token", "api_key"):
                value = getattr(section, attr, None)
                if isinstance(value, str) and value and value != "********":
                    values.append(value)
        return values

    @classmethod
    def _sanitize(cls, text: str) -> str:
        out = str(text)
        out = cls._CHECK_PASSWORD_RE.sub(r"\1***", out)
        out = cls._AUTH_RE.sub(r"\1***", out)
        out = cls._COOKIE_RE.sub(r"\1***", out)
        out = cls._KEY_VALUE_RE.sub(r"\1***", out)
        # Ma'lum secret boshqa kontekstda loglangan bo'lsa ham maskalanadi.
        for secret in sorted(set(cls._known_secrets()), key=len, reverse=True):
            left = r"(?<!\w)" if secret[0].isalnum() or secret[0] == "_" else ""
            right = r"(?!\w)" if secret[-1].isalnum() or secret[-1] == "_" else ""
            out = re.sub(left + re.escape(secret) + right, "***", out)
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = self._sanitize(rendered)
        record.args = ()
        return True


def categorize(record: logging.LogRecord) -> str:
    """Log yozuvining funksional manbasini aniqlaydi (TAG -> modul -> SYSTEM)."""
    try:
        msg = record.getMessage()
    except Exception:
        msg = str(record.msg)
    m = _TAG_RE.search(msg or "")
    if m:
        tag = m.group(1).strip().upper().replace(" ", "")
        if tag in _TAG_SOURCE:
            return _TAG_SOURCE[tag]
    mod = getattr(record, "module", "") or ""
    if mod in _MODULE_SOURCE:
        return _MODULE_SOURCE[mod]
    return "SYSTEM"


class _SourceFilter(logging.Filter):
    """Har bir yozuvga record.source biriktiradi (handlerlar uchun)."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.source = categorize(record)
        return True


class _OnlySource(logging.Filter):
    def __init__(self, source: str) -> None:
        super().__init__()
        self._source = source
    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "source", "SYSTEM") == self._source


class _OnlyErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class _RingBufferHandler(logging.Handler):
    """Loglarni xotiradagi deque ga yozadi + kumulyativ statistikani yuritadi."""

    def __init__(self, maxlen: int = _MAX_UI_LOGS) -> None:
        super().__init__()
        self._buf: Deque[Dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._counts = {
            "total": 0, "DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0,
            "plc_reconnect": 0, "rfid_read": 0, "ocr_success": 0, "camera_disconnect": 0,
        }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        source = getattr(record, "source", None) or categorize(record)
        level = record.levelname
        with self._lock:
            self._seq += 1
            self._buf.append({
                "id": self._seq,
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "tsfull": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "epoch": record.created,
                "level": level,
                "source": source,
                "msg": msg,
            })
            self._counts["total"] += 1
            if level in self._counts:
                self._counts[level] += 1
            if _PAT_PLC_RECONNECT.search(msg):
                self._counts["plc_reconnect"] += 1
            if _PAT_RFID_READ.search(msg):
                self._counts["rfid_read"] += 1
            if _PAT_OCR_SUCCESS.search(msg):
                self._counts["ocr_success"] += 1
            if _PAT_CAM_DISCONNECT.search(msg):
                self._counts["camera_disconnect"] += 1

    # ---- back-compat ----
    def get_since(self, last_id: int = 0) -> List[Dict]:
        with self._lock:
            return [e for e in self._buf if e["id"] > last_id]

    def snapshot(self) -> List[Dict]:
        with self._lock:
            return list(self._buf)

    # ---- new ----
    def stats(self) -> Dict:
        with self._lock:
            return dict(self._counts)

    def last_id(self) -> int:
        with self._lock:
            return self._seq

    def query(self, source: Optional[str] = None, level: Optional[str] = None,
              q: Optional[str] = None, limit: int = 200, offset: int = 0) -> Dict:
        """Filtrlangan, sahifalangan yozuvlar (eng yangidan eskiga)."""
        lvl = (level or "").upper() or None    # ANIQ daraja mosligi ("faqat ERROR")
        src = (source or "").upper() or None
        ql = (q or "").lower().strip() or None
        with self._lock:
            items = list(self._buf)
        out = []
        for e in reversed(items):           # eng yangi birinchi
            if src == "ERROR":
                if _LEVELS.get(e["level"], 0) < logging.ERROR:
                    continue
            elif src and e["source"] != src:
                continue
            if lvl is not None and e["level"] != lvl:
                continue
            if ql and ql not in e["msg"].lower() and ql not in e["source"].lower():
                continue
            out.append(e)
        total = len(out)
        page = out[offset:offset + limit]
        return {"total": total, "items": page}

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            for k in self._counts:
                self._counts[k] = 0


_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}

# --- Yagona ring-buffer handler (UI bilan baham ko'riladi) ---
ui_handler = _RingBufferHandler()

# Per-source fayl handlerlari (clear/diagnostika uchun saqlanadi)
_FILE_HANDLERS: List[logging.handlers.RotatingFileHandler] = []
LOG_FILES: Dict[str, str] = {}

_ROTATE_BYTES = 10 * 1024 * 1024     # 10 MB
_ROTATE_KEEP = 30                    # oxirgi 30 fayl


def _add_file(logger: logging.Logger, fmt: logging.Formatter, filename: str,
              flt: Optional[logging.Filter]) -> None:
    path = LOGS_DIR / filename
    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=_ROTATE_BYTES, backupCount=_ROTATE_KEEP, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_SecretSanitizer())
    if flt is not None:
        fh.addFilter(flt)
    logger.addHandler(fh)
    _FILE_HANDLERS.append(fh)
    LOG_FILES[filename.replace(".log", "")] = str(path)


def setup_logger(name: str = "ai_cam") -> logging.Logger:
    """Asosiy loggerni sozlaydi: konsol + per-source fayllar + UI buffer."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)       # DEBUG darajagacha qo'llab-quvvatlanadi
    logger.propagate = False
    logger.addFilter(_SourceFilter())    # record.source ni o'rnatadi (handlerlardan oldin)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-5s] [%(source)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1) Optional per-source files. systemd defaults to console/journald only;
    # sites that enable files also install the bounded logrotate policy.
    if OPERATIONS.file_logging_enabled:
        _add_file(logger, fmt, "system.log", None)               # hammasi
        _add_file(logger, fmt, "plc.log", _OnlySource("PLC"))
        _add_file(logger, fmt, "rfid.log", _OnlySource("RFID"))
        _add_file(logger, fmt, "camera.log", _OnlySource("CAMERA"))
        _add_file(logger, fmt, "ocr.log", _OnlySource("OCR"))
        _add_file(logger, fmt, "errors.log", _OnlyErrors())

    # 2) Konsol
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(_SecretSanitizer())
    logger.addHandler(ch)

    # 3) UI ring buffer
    ui_handler.setFormatter(fmt)
    ui_handler.addFilter(_SecretSanitizer())
    logger.addHandler(ui_handler)

    return logger


def clear_files() -> None:
    """Joriy per-source fayllarni bo'shatadi (backuplar saqlanadi)."""
    for fh in _FILE_HANDLERS:
        try:
            fh.acquire()
            if fh.stream:
                fh.stream.truncate(0)
                fh.stream.seek(0)
        except Exception:
            pass
        finally:
            try:
                fh.release()
            except Exception:
                pass


_LINE_RE = re.compile(r"^\[(.*?)\]\s+\[(.*?)\]\s+\[(.*?)\]\s+(.*)$")


def search_files(q: Optional[str] = None, source: Optional[str] = None,
                 level: Optional[str] = None, start: Optional[str] = None,
                 end: Optional[str] = None, limit: int = 500) -> List[Dict]:
    """system.log dan (chuqurroq tarix) filtrlangan yozuvlarni qidiradi."""
    path = LOG_FILES.get("system")
    if not path:
        return []
    want_lvl = (level or "").upper() or None    # ANIQ daraja mosligi
    src = (source or "").upper() or None
    ql = (q or "").lower().strip() or None
    s = (start or "").strip() or None
    e = (end or "").strip() or None
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []
    for line in reversed(lines):           # eng yangi birinchi
        m = _LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        ts, lvl, so, msg = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4)
        if src == "ERROR":
            if _LEVELS.get(lvl, 0) < logging.ERROR:
                continue
        elif src and so != src:
            continue
        if want_lvl is not None and lvl != want_lvl:
            continue
        if ql and ql not in msg.lower() and ql not in so.lower():
            continue
        if s and ts < s:
            continue
        if e and ts > e:
            continue
        out.append({"tsfull": ts, "ts": ts[11:], "level": lvl, "source": so, "msg": msg})
        if len(out) >= limit:
            break
    return out


# Global logger — barcha modullar shu nusxani import qiladi
log = setup_logger()
