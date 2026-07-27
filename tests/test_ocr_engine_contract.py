"""
============================================================
test_ocr_engine_contract.py — v1.2.1 engine-neutral OCR contract
============================================================
Locks the v1.2.1 architecture-readiness contract (docs/v1.2.1/OCR_ENGINE_CONTRACT.md):

  1.  Legacy Paddle adapter satisfies the new OCRRecognizer contract.
  2.  Missing future model returns a structured ENGINE_NOT_CONFIGURED result.
  3.  Primary engine mode produces the operational result.
  4.  Shadow engine cannot change the production decision.
  5.  Fallback engine is invoked only under configured failure conditions.
  6.  Raw prediction remains available on the result (audit/retraining evidence).
  7.  Generic identifier validation does NOT require NSTF/NSTH prefixes.
  8.  Business validation profiles remain optional and isolated.
  9.  A hypothetical identifier EALDJHFC029411177 passes generic validation.
  10. session_id / capture_generation / job_id survive every OCR callback path.
  18. Health reports engine / model / version / readiness.

No real PaddleOCR engine is loaded — the in-process `_engine` seam is used
(identical approach to tests/test_ocr_contract.py), so these tests are fully
deterministic and network-free.
"""
from __future__ import annotations

import abc

import numpy as np
import pytest

from backend.ai import ocr_contract as C
from backend.ai.engines import (
    EngineRouter,
    EngravedSequenceRecognizer,
    PaddleOCRLegacyAdapter,
)


class _FakeEngine:
    """Deterministic in-process stand-in for PaddleOCR (no network, no pool)."""

    def __init__(self, text: str, conf: float = 0.95):
        self.text = text
        self.conf = conf

    def read(self, img):
        box = [[0, 0], [10, 0], [10, 10], [0, 10]]
        return [(box, self.text, self.conf)]


def _dummy_crop() -> np.ndarray:
    return np.zeros((20, 60, 3), dtype=np.uint8)


def _request(session_id="sess-1", capture_generation=7, job_id=3,
             model_hint="QY", mode="primary") -> C.OCRRecognitionRequest:
    return C.OCRRecognitionRequest(
        session_id=session_id,
        capture_generation=capture_generation,
        job_id=job_id,
        crops=[C.CropRef(crop_index=0, image=_dummy_crop())],
        submitted_at=123.0,
        model_hint=model_hint,
        recognition_mode=mode,
        charset_profile=C.ALPHANUMERIC_36,
    )


def _legacy_adapter(text="NSTFA814ATJ123456", conf=0.95) -> PaddleOCRLegacyAdapter:
    adapter = PaddleOCRLegacyAdapter()
    adapter.attach_test_engine(_FakeEngine(text, conf))   # in-process, no pool
    return adapter


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------
def test_recognizer_is_abstract_interface():
    assert issubclass(C.OCRRecognizer, abc.ABC)
    for method in ("initialize", "recognize", "health", "metadata", "close"):
        assert hasattr(C.OCRRecognizer, method)


def test_legacy_adapter_satisfies_recognizer_contract():
    """1. Legacy Paddle adapter is a real OCRRecognizer."""
    adapter = _legacy_adapter()
    assert isinstance(adapter, C.OCRRecognizer)
    result = adapter.recognize(_request())
    assert isinstance(result, C.OCRRecognitionResult)
    assert result.engine_id == "paddleocr_legacy"
    assert result.engine_status == C.OK
    assert result.normalized_sequence == "NSTFA814ATJ123456"


def test_result_round_trips_session_capture_generation_job_id():
    """10. Ownership identifiers survive the recognition path unchanged."""
    adapter = _legacy_adapter()
    result = adapter.recognize(_request(session_id="sess-42",
                                        capture_generation=99, job_id=5))
    assert result.session_id == "sess-42"
    assert result.capture_generation == 99
    assert result.job_id == 5


def test_raw_prediction_is_preserved():
    """6. Raw prediction remains available for audit/retraining."""
    adapter = _legacy_adapter(text="NSTFA814ATJ123456")
    result = adapter.recognize(_request())
    assert result.raw_sequence  # non-empty
    assert result.raw_sequence == "NSTFA814ATJ123456"


def test_engine_metadata_and_health_report_engine_model_version_readiness():
    """18. Health/metadata report engine, model, version, charset, readiness."""
    adapter = _legacy_adapter()
    meta = adapter.metadata()
    assert meta.engine_id == "paddleocr_legacy"
    assert meta.engine_version
    assert meta.charset_id
    health = adapter.health()
    assert health.engine_id == "paddleocr_legacy"
    assert health.status in ("ready", "not_ready", "engine_unavailable")


# ---------------------------------------------------------------------------
# Future engine placeholder
# ---------------------------------------------------------------------------
def test_missing_future_model_returns_engine_not_configured():
    """2. Calling the future engine before a model exists fails clearly."""
    engine = EngravedSequenceRecognizer(model_path="")
    engine.initialize()
    result = engine.recognize(_request(mode="shadow"))
    assert result.engine_status == C.ENGINE_NOT_CONFIGURED
    assert result.failure_type == C.ENGINE_NOT_CONFIGURED
    assert result.normalized_sequence == ""
    assert result.raw_sequence == ""
    assert result.sequence_confidence == 0.0
    # identifiers still echoed even on the short-circuit path
    assert result.session_id == "sess-1"
    assert result.capture_generation == 7
    assert result.job_id == 3


def test_future_engine_health_reports_not_configured():
    engine = EngravedSequenceRecognizer(model_path="")
    engine.initialize()
    health = engine.health()
    assert health.ready is False
    assert health.status == "not_configured"


def test_future_engine_never_fabricates_a_prediction():
    """The placeholder must not invent a VIN even with a plausible model hint."""
    engine = EngravedSequenceRecognizer(model_path="models/does_not_exist.pt")
    engine.initialize()
    result = engine.recognize(_request())
    assert result.normalized_sequence == ""


# ---------------------------------------------------------------------------
# Engine modes (router)
# ---------------------------------------------------------------------------
def test_primary_mode_produces_operational_result():
    """3. Primary engine's result is the operational decision."""
    primary = _legacy_adapter(text="NSTFA814ATJ123456")
    router = EngineRouter(primary=primary, shadow=None, fallback=None)
    result = router.recognize(_request())
    assert result.engine_id == "paddleocr_legacy"
    assert result.normalized_sequence == "NSTFA814ATJ123456"


def test_shadow_engine_cannot_change_production_decision():
    """4. A shadow engine's output never affects the operational result."""
    primary = _legacy_adapter(text="NSTFA814ATJ123456")
    shadow = _legacy_adapter(text="NSTHD41ABUJ999999")   # different reading
    router = EngineRouter(primary=primary, shadow=shadow, fallback=None)
    result = router.recognize(_request())
    # production decision is the PRIMARY reading, not the shadow's
    assert result.normalized_sequence == "NSTFA814ATJ123456"
    assert result.engine_id == "paddleocr_legacy"
    # shadow result is captured for audit but marked non-authoritative
    assert router.last_shadow_result is not None
    assert router.last_shadow_result.normalized_sequence == "NSTHD41ABUJ999999"


def test_fallback_invoked_only_on_configured_failure():
    """5. Fallback runs only when the primary meets a failure condition."""
    # primary reads a valid sequence -> fallback must NOT run
    good_primary = _legacy_adapter(text="NSTFA814ATJ123456")
    fallback_spy = _legacy_adapter(text="NSTHD41ABUJ000001")
    router = EngineRouter(primary=good_primary, shadow=None, fallback=fallback_spy)
    router.recognize(_request())
    assert router.last_fallback_invoked is False

    # primary produces an ENGINE_UNAVAILABLE-class failure -> fallback runs
    failing_primary = PaddleOCRLegacyAdapter()
    failing_primary.attach_test_engine(None)   # no engine -> failure result
    router2 = EngineRouter(primary=failing_primary, shadow=None, fallback=fallback_spy)
    result2 = router2.recognize(_request())
    assert router2.last_fallback_invoked is True
    assert result2.normalized_sequence == "NSTHD41ABUJ000001"


def test_router_preserves_identifiers_in_all_modes():
    primary = _legacy_adapter()
    shadow = _legacy_adapter(text="NSTHD41ABUJ000002")
    router = EngineRouter(primary=primary, shadow=shadow, fallback=None)
    result = router.recognize(_request(session_id="S", capture_generation="G", job_id="J"))
    assert (result.session_id, result.capture_generation, result.job_id) == ("S", "G", "J")
    assert router.last_shadow_result.session_id == "S"


# ---------------------------------------------------------------------------
# Layered validation (Layer 2 generic vs Layer 3 business)
# ---------------------------------------------------------------------------
def test_generic_validation_does_not_require_vin_prefix():
    """7. Generic identifier validation has no NSTF/NSTH knowledge."""
    policy = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.ALPHANUMERIC_36,
                                       min_confidence=0.5)
    res = C.validate_generic("ZZZZ0000000000000", 0.9, policy)
    assert res.ok is True
    # a VIN-format identifier is equally acceptable to the generic layer
    res2 = C.validate_generic("NSTFA814ATJ123456", 0.9, policy)
    assert res2.ok is True


def test_hypothetical_identifier_passes_generic_validation():
    """9. EALDJHFC029411177 passes generic validation (17 chars, in 36-charset)."""
    policy = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.ALPHANUMERIC_36,
                                       min_confidence=0.5)
    res = C.validate_generic("EALDJHFC029411177", 0.9, policy)
    assert res.ok is True
    # NSTFC814ETJ042305 has 17 chars too
    assert C.validate_generic("NSTFC814ETJ042305", 0.9, policy).ok is True


def test_generic_validation_rejects_wrong_length_and_charset_and_conf():
    policy = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.ALPHANUMERIC_36,
                                       min_confidence=0.6)
    assert C.validate_generic("SHORT", 0.9, policy).ok is False              # length
    assert C.validate_generic("EALDJHFC029411177", 0.3, policy).ok is False  # confidence
    # '@' is outside the 36-char alphabet
    assert C.validate_generic("EALDJHFC02941117@", 0.9, policy).ok is False   # charset


def test_charset_36_includes_ioq_but_vin33_excludes_them():
    """6/§6. Recognition charset is 36 chars incl. I/O/Q; VIN charset is 33."""
    cs36 = C.charset(C.ALPHANUMERIC_36)
    for ch in "IOQ":
        assert ch in cs36
    assert len(set(cs36)) == 36
    cs33 = C.charset(C.VIN_ALNUM_33)
    for ch in "IOQ":
        assert ch not in cs33
    assert len(set(cs33)) == 33


def _imported_modules(module) -> set:
    """Collect every module name referenced by an import statement (AST-based)."""
    import ast
    with open(module.__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    return names


def test_business_validation_is_optional_and_isolated():
    """8. Business (VIN) validation is a separate, optional layer."""
    # generic layer accepts a non-VIN identifier...
    policy = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.ALPHANUMERIC_36,
                                       min_confidence=0.5)
    assert C.validate_generic("EALDJHFC029411177", 0.9, policy).ok is True
    # ...and the contract module must NOT import Layer-3 / pipeline / IO modules.
    import backend.ai.ocr_contract as mod
    imports = _imported_modules(mod)
    forbidden = ("vin_rules", "vin_fusion", "pipeline", "plc", "rfid", "database")
    leaked = [name for name in imports for bad in forbidden if bad in name]
    assert not leaked, f"contract module leaked Layer-3/IO imports: {leaked}"


def test_engine_modules_do_not_import_layer3_or_io():
    """8/§2. Engine adapters must not import business/pipeline/IO layers."""
    from backend.ai.engines import paddle_legacy, engraved_sequence, router
    forbidden = ("vin_rules", "pipeline", "plc_service", "rfid_service", "database.db")
    for mod in (paddle_legacy, engraved_sequence, router):
        imports = _imported_modules(mod)
        leaked = [name for name in imports for bad in forbidden if bad in name]
        assert not leaked, f"{mod.__name__} leaked forbidden imports: {leaked}"


def test_charset_profile_recognition_supports_36_even_if_profile_forbids_ioq():
    """The engine's charset (36) is independent of a validation profile forbidding I/O/Q."""
    # An engine may output I/O/Q; a VIN profile forbids them. Both facts coexist.
    policy_vin = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.VIN_ALNUM_33,
                                           min_confidence=0.5)
    # sequence containing 'I' fails the VIN-charset profile...
    assert C.validate_generic("NSTFA814ATJ12345I", 0.9, policy_vin).ok is False
    # ...but passes a 36-charset profile
    policy_36 = C.GenericIdentifierPolicy(expected_length=17, charset_id=C.ALPHANUMERIC_36,
                                          min_confidence=0.5)
    assert C.validate_generic("NSTFA814ATJ12345I", 0.9, policy_36).ok is True
