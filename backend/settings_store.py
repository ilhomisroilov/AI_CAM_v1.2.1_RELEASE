"""
============================================================
settings_store.py  —  config/settings.yaml o'qish/yozish qatlami
============================================================
Settings sahifasi shu modul orqali settings.yaml ni xavfsiz o'qiydi/yozadi.
  * read_raw()   — fayl matni (xom YAML)
  * read_parsed()— parse qilingan dict
  * write_raw()  — YAML ni VALIDATSIYA qilib (parse) yozadi
  * write_data() — dict -> YAML -> yozadi
  * defaults()   — config.py dataclass standart qiymatlari (Restore Defaults)

MUHIM: yozishdan oldin YAML albatta parse qilinadi — buzuq sintaksis yozilmaydi.
Auto-restart YO'Q (sanoat qutisida xavfli) — saqlangach foydalanuvchi ilovani
o'zi qayta ishga tushiradi. Ba'zi qiymatlar apply_settings() bilan darhol qo'llanadi.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict

from . import config as C


def _yaml():
    import yaml  # lazy — config ham shunday ishlatadi
    return yaml


def read_raw() -> str:
    p = C.SETTINGS_PATH
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_parsed() -> Dict[str, Any]:
    try:
        return _yaml().safe_load(read_raw()) or {}
    except Exception:
        return {}


def write_raw(text: str) -> str:
    """YAML ni validatsiya qilib yozadi. Buzuq bo'lsa xato ko'taradi (yozilmaydi)."""
    parsed = _yaml().safe_load(text)
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError("settings.yaml ildizi obyekt (key: value) bo'lishi kerak.")
    C.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    C.SETTINGS_PATH.write_text(text, encoding="utf-8")
    return text


def write_data(data: Dict[str, Any]) -> str:
    text = _yaml().safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return write_raw(text)


def _fields(obj) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        if isinstance(v, tuple):
            v = list(v)
        out[f.name] = v
    return out


def defaults() -> Dict[str, Any]:
    """config.py dataclass standartlari (pristine) — Restore Defaults uchun."""
    return {
        "camera": _fields(C.CameraConfig()),
        "plc": _fields(C.PLCConfig()),
        "rfid": _fields(C.RFIDConfig()),
        "session": _fields(C.SessionConfig()),
        "detection": _fields(C.DetectionConfig()),
        "ocr": _fields(C.OCRConfig()),
        "vin": _fields(C.VINConfig()),
        "pos5_verifier": _fields(C.Pos5VerifierConfig()),
        "vin_slot_recognizer": _fields(C.VinSlotRecognizerConfig()),
        "server": _fields(C.ServerConfig()),
        "auth": _fields(C.AuthConfig()),
        "operations": _fields(C.OperationsConfig()),
    }
