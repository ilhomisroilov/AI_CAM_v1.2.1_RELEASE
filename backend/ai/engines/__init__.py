"""
backend/ai/engines — OCRRecognizer implementations and the mode router (v1.2.1).

Each engine here satisfies backend.ai.ocr_contract.OCRRecognizer and MUST NOT
import vin_rules / pipeline / plc / rfid / database / session modules. That
boundary is asserted by tests/test_ocr_engine_contract.py.
"""
from __future__ import annotations

from .engraved_sequence import EngravedSequenceRecognizer
from .factory import (
    build_engine,
    build_router_from_config,
    engines_health_report,
)
from .paddle_legacy import PaddleOCRLegacyAdapter
from .router import EngineRouter

__all__ = [
    "EngravedSequenceRecognizer",
    "PaddleOCRLegacyAdapter",
    "EngineRouter",
    "build_engine",
    "build_router_from_config",
    "engines_health_report",
]
