"""
factory.py — build the OCR engine router / health report from config (v1.2.1).

Orchestration glue: reads backend.config.OCR_ENGINES and constructs concrete
OCRRecognizer instances + an EngineRouter. Legacy config (no `ocr_engines`
section) yields a legacy-only router (paddleocr_legacy primary, no shadow/fallback)
— i.e. exactly today's behaviour.
"""
from __future__ import annotations

from typing import List, Optional

from .. import ocr_contract as C
from .engraved_sequence import EngravedSequenceRecognizer
from .paddle_legacy import PaddleOCRLegacyAdapter
from .router import EngineRouter

_DISABLED_TOKENS = frozenset(("", "none", "null", "disabled"))


def _is_disabled(engine_id) -> bool:
    return str(engine_id or "").strip().lower() in _DISABLED_TOKENS


def build_engine(engine_id, engines_cfg) -> Optional[C.OCRRecognizer]:
    """Construct one engine by id, or None if disabled. Unknown id -> ValueError."""
    if _is_disabled(engine_id):
        return None
    eid = str(engine_id).strip().lower()
    charset = getattr(engines_cfg, "charset_profile", C.ALPHANUMERIC_36)
    if eid == "paddleocr_legacy":
        return PaddleOCRLegacyAdapter(charset_id=charset)
    if eid == "engraved_sequence":
        return EngravedSequenceRecognizer(
            model_path=getattr(engines_cfg, "engraved_model_path", "") or "",
            charset_id=charset)
    raise ValueError(f"unknown OCR engine id: {engine_id!r}")


def build_router_from_config(engines_cfg=None, logger=None) -> EngineRouter:
    """Build the primary/shadow/fallback router from OCR_ENGINES config."""
    if engines_cfg is None:
        from ...config import OCR_ENGINES as engines_cfg  # type: ignore
    primary = build_engine(engines_cfg.primary_engine, engines_cfg)
    if primary is None:                      # never leave production without a primary
        primary = PaddleOCRLegacyAdapter(
            charset_id=getattr(engines_cfg, "charset_profile", C.ALPHANUMERIC_36))
    shadow = build_engine(getattr(engines_cfg, "shadow_engine", ""), engines_cfg)
    fallback = build_engine(getattr(engines_cfg, "fallback_engine", ""), engines_cfg)
    return EngineRouter(primary=primary, shadow=shadow, fallback=fallback, logger=logger)


def engines_health_report(engines_cfg=None) -> List[dict]:
    """Return the additive /health `components.ocr.engines` array.

    Reports every configured engine's static metadata + live health + its mode,
    without invoking recognition. Safe to call from the health endpoint.
    """
    if engines_cfg is None:
        from ...config import OCR_ENGINES as engines_cfg  # type: ignore
    modes = {}
    if not _is_disabled(engines_cfg.primary_engine):
        modes[str(engines_cfg.primary_engine)] = "primary"
    if not _is_disabled(getattr(engines_cfg, "shadow_engine", "")):
        modes.setdefault(str(engines_cfg.shadow_engine), "shadow")
    if not _is_disabled(getattr(engines_cfg, "fallback_engine", "")):
        modes.setdefault(str(engines_cfg.fallback_engine), "fallback")

    out: List[dict] = []
    for engine_id, mode in modes.items():
        try:
            engine = build_engine(engine_id, engines_cfg)
        except ValueError:
            out.append({"engine_id": engine_id, "mode": mode,
                        "readiness": "unknown", "error": "unknown engine id"})
            continue
        if engine is None:
            continue
        meta = engine.metadata()
        health = engine.health()
        out.append({
            "engine_id": meta.engine_id,
            "engine_version": meta.engine_version,
            "engine_model_id": meta.engine_model_id,
            "engine_model_version": meta.engine_model_version,
            "charset_id": meta.charset_id,
            "mode": mode,
            "readiness": health.status,
            "last_error": health.last_error or "",
            "last_check_at": health.last_check_at,
        })
    return out
