"""
============================================================
ocr_contract.py  —  Engine-neutral OCR recognition contract (v1.2.1)
============================================================
This module defines the STABLE SEAM between the AI_CAM pipeline and any OCR
recognition engine, so a future trained engraved-alphanumeric model can be
integrated without rewriting camera/PLC/RFID/session/database/API code.

Design authority: docs/v1.2.1/OCR_ENGINE_CONTRACT.md.

STRICT LAYERING — this module is Layer 1/2 only:
  * Layer 1 (visual recognition): image(s) -> raw alphanumeric sequence + evidence.
    It MUST NOT know about vehicle-model prefixes (NSTF/NSTH), VIN semantics, or
    session/PLC/RFID/database concepts.
  * Layer 2 (generic identifier validation): length, charset membership, confidence
    threshold, multi-crop/variant agreement, unknown-character rejection.
  * Layer 3 (business validation profile: ISO VIN, QY, BL7M, ...) lives ELSEWHERE
    (backend/ai/vin_rules.py and the validation-profile layer). This module
    deliberately does NOT import vin_rules — that isolation is asserted by a test
    (tests/test_ocr_engine_contract.py::test_business_validation_is_optional_and_isolated).

The engine interface MUST NOT perform silent character substitution — any
structural correction (0->D, 1->J, ...) is a Layer 2/3 concern, never Layer 1.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import numpy as np

# ===============================================================
# Engine status / failure taxonomy
# ===============================================================
# Mirrors backend/ai/ocr_process.py so the legacy adapter can map 1:1, plus the
# new ENGINE_NOT_CONFIGURED status used by an unconfigured future engine.
OK = "OK"
EMPTY = "EMPTY"                              # engine ran, no characters found
ERROR = "ERROR"                             # caught Python exception, engine alive
ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"   # fatal init/runtime dependency failure
OCR_ENGINE_CRASH = "OCR_ENGINE_CRASH"       # worker process died natively
OCR_ENGINE_TIMEOUT = "OCR_ENGINE_TIMEOUT"   # task/batch deadline exceeded
ENGINE_NOT_CONFIGURED = "ENGINE_NOT_CONFIGURED"  # engine has no model artifact yet

# Statuses under which a fallback engine is allowed to be consulted.
FALLBACK_TRIGGER_STATUSES = frozenset((
    EMPTY, ERROR, ENGINE_UNAVAILABLE, OCR_ENGINE_CRASH, OCR_ENGINE_TIMEOUT,
    ENGINE_NOT_CONFIGURED,
))

# ===============================================================
# Charset profiles (per-engine recognition vocabulary — NOT a business filter)
# ===============================================================
ALPHANUMERIC_36 = "alphanumeric_36"
VIN_ALNUM_33 = "vin_alnum_33"

_CHARSETS = {
    # Full generic engine vocabulary. Includes I/O/Q — a raw OCR engine has no
    # business reason to refuse them; VIN exclusion of I/O/Q is a Layer-3 concern.
    ALPHANUMERIC_36: "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    # VIN alphabet baked into the existing vin_slot_recognizer model (no I/O/Q).
    VIN_ALNUM_33: "0123456789ABCDEFGHJKLMNPRSTUVWXYZ",
}


def charset(charset_id: str) -> str:
    """Return the character string for a registered charset profile."""
    try:
        return _CHARSETS[charset_id]
    except KeyError as exc:
        raise KeyError(f"unknown charset profile: {charset_id!r} "
                       f"(known: {sorted(_CHARSETS)})") from exc


def register_charset(charset_id: str, characters: str) -> None:
    """Register a new charset profile (e.g. when a future engine ships its own)."""
    _CHARSETS[charset_id] = characters


# ===============================================================
# DTOs — request side
# ===============================================================
@dataclass
class CropRef:
    """One candidate crop fed to the recognizer."""
    crop_index: int
    image: np.ndarray
    evidence_group: Optional[int] = None
    source: str = ""

    def __post_init__(self):
        if self.evidence_group is None:
            self.evidence_group = self.crop_index


@dataclass
class OCRRecognitionRequest:
    """Everything a recognizer needs, plus the ownership identifiers that MUST be
    echoed back unchanged on the result (see session-safety review)."""
    session_id: object = None
    capture_generation: object = None
    job_id: object = None
    crops: List[CropRef] = field(default_factory=list)
    submitted_at: float = 0.0
    model_hint: Optional[str] = None          # Layer 1 MUST ignore this
    requested_engine: Optional[str] = None
    recognition_mode: str = "primary"          # primary|shadow|fallback|disabled
    charset_profile: str = ALPHANUMERIC_36


# ===============================================================
# DTOs — result side
# ===============================================================
@dataclass
class OCRCharacterPrediction:
    position: int
    char: str
    confidence: float
    alternatives: List[Tuple[str, float]] = field(default_factory=list)


@dataclass
class CropEvidenceRef:
    crop_index: int
    variant_name: str
    evidence_group: int
    ocr_conf: float
    raw_text: str = ""


@dataclass
class OCRRecognitionResult:
    session_id: object
    capture_generation: object
    job_id: object
    engine_id: str
    engine_version: str
    charset_id: str
    raw_sequence: str
    normalized_sequence: str
    sequence_confidence: float
    engine_status: str
    engine_model_id: Optional[str] = None
    engine_model_version: Optional[str] = None
    char_predictions: Optional[List[OCRCharacterPrediction]] = None
    crop_evidence: List[CropEvidenceRef] = field(default_factory=list)
    raw_payload: Any = None
    boxes: List[Any] = field(default_factory=list)
    latency_ms: float = 0.0
    warnings: List[str] = field(default_factory=list)
    failure_type: Optional[str] = None
    recognized_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.engine_status == OK

    @property
    def accepted(self) -> bool:
        return self.engine_status == OK and bool(self.normalized_sequence)


@dataclass
class EngineMetadata:
    engine_id: str
    engine_version: str
    charset_id: str
    engine_model_id: Optional[str] = None
    engine_model_version: Optional[str] = None
    supports_per_char_confidence: bool = False
    supports_alternatives: bool = False
    max_batch_size: int = 1
    device: str = "auto"


@dataclass
class EngineHealth:
    engine_id: str
    ready: bool
    status: str                 # ready|not_ready|engine_unavailable|not_configured
    detail: str = ""
    last_error: Optional[str] = None
    last_check_at: float = 0.0


# ===============================================================
# Engine interface
# ===============================================================
class OCRRecognizer(abc.ABC):
    """Pure visual recognition engine.

    MUST NOT import vin_rules / pipeline / plc / rfid / database / session
    modules, and MUST NOT silently substitute characters. Any correction or
    vehicle-model decision belongs to Layer 2/3.
    """

    engine_id: str = "abstract"

    @abc.abstractmethod
    def initialize(self) -> None:
        """Lazily load model/pool. Idempotent."""

    @abc.abstractmethod
    def recognize(self, request: OCRRecognitionRequest) -> OCRRecognitionResult:
        """Recognize the sequence in the request's crops."""

    @abc.abstractmethod
    def health(self) -> EngineHealth:
        """Report readiness for /health."""

    @abc.abstractmethod
    def metadata(self) -> EngineMetadata:
        """Static engine/model metadata for /health and audit."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release process pool / model / device."""


# ===============================================================
# Layer 2 — generic identifier validation (no business/VIN knowledge)
# ===============================================================
@dataclass
class GenericIdentifierPolicy:
    """Engine/format-neutral acceptance policy. Knows nothing about NSTF/NSTH,
    vehicle models, or position-5 semantics."""
    expected_length: int = 17
    charset_id: str = ALPHANUMERIC_36
    min_confidence: float = 0.5
    min_crops: int = 1
    allow_unknown_chars: bool = False


@dataclass
class GenericValidationResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)


def validate_generic(sequence: str,
                     sequence_confidence: float,
                     policy: GenericIdentifierPolicy,
                     n_crops: int = 1) -> GenericValidationResult:
    """Layer-2 validation: length, charset, confidence, crop agreement.

    Deliberately format-agnostic: a VIN, a factory identifier, or a hypothetical
    engraved sequence such as ``EALDJHFC029411177`` are all judged by the same
    generic rules. No vehicle-model prefix is ever required.
    """
    reasons: List[str] = []
    seq = (sequence or "").upper()

    if policy.expected_length and len(seq) != policy.expected_length:
        reasons.append(f"length={len(seq)}!={policy.expected_length}")

    if not policy.allow_unknown_chars:
        allowed = set(charset(policy.charset_id))
        bad = sorted({ch for ch in seq if ch not in allowed})
        if bad:
            reasons.append(f"charset_violation={''.join(bad)}")

    if sequence_confidence < policy.min_confidence:
        reasons.append(f"confidence={sequence_confidence:.3f}<{policy.min_confidence:.3f}")

    if n_crops < policy.min_crops:
        reasons.append(f"crops={n_crops}<{policy.min_crops}")

    return GenericValidationResult(ok=not reasons, reasons=reasons)


def make_result(request: OCRRecognitionRequest,
                *,
                engine_id: str,
                engine_version: str,
                engine_status: str,
                charset_id: Optional[str] = None,
                raw_sequence: str = "",
                normalized_sequence: str = "",
                sequence_confidence: float = 0.0,
                engine_model_id: Optional[str] = None,
                engine_model_version: Optional[str] = None,
                char_predictions: Optional[List[OCRCharacterPrediction]] = None,
                crop_evidence: Optional[List[CropEvidenceRef]] = None,
                raw_payload: Any = None,
                boxes: Optional[List[Any]] = None,
                latency_ms: float = 0.0,
                warnings: Optional[List[str]] = None,
                failure_type: Optional[str] = None) -> OCRRecognitionResult:
    """Build an OCRRecognitionResult while GUARANTEEING the ownership identifiers
    (session_id / capture_generation / job_id) are echoed from the request. Every
    engine and short-circuit path should use this so identifiers are never lost."""
    return OCRRecognitionResult(
        session_id=request.session_id,
        capture_generation=request.capture_generation,
        job_id=request.job_id,
        engine_id=engine_id,
        engine_version=engine_version,
        charset_id=charset_id or request.charset_profile,
        raw_sequence=raw_sequence,
        normalized_sequence=normalized_sequence,
        sequence_confidence=sequence_confidence,
        engine_status=engine_status,
        engine_model_id=engine_model_id,
        engine_model_version=engine_model_version,
        char_predictions=char_predictions,
        crop_evidence=list(crop_evidence or []),
        raw_payload=raw_payload,
        boxes=list(boxes or []),
        latency_ms=latency_ms,
        warnings=list(warnings or []),
        failure_type=failure_type if failure_type is not None else (
            None if engine_status == OK else engine_status),
        recognized_at=time.time(),
    )
