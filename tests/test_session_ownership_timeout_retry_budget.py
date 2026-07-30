"""
============================================================
test_session_ownership_timeout_retry_budget.py
============================================================
Covers three brief items not exercised by the existing suite:
  * session-ownership no-leak AT THE ORCHESTRATOR LAYER (backend/ai/
    ocr_orchestrator.py) -- tests/test_session_ownership.py and
    tests/test_late_results.py already lock this at the PIPELINE layer
    (Session A's result never lands on Session B); this file locks the same
    invariant one layer down, where the planned hotfix's three-engine
    concurrency actually runs.
  * per-engine TIMEOUT isolation (one engine hanging must not block/delay
    the others or the overall decision).
  * per-engine EXCEPTION isolation (one engine crashing must not cancel or
    corrupt the others' results).
  * retry-budget: OCRRetryGuard must deterministically stop retrying once
    evidence is stable, so a session can never hang waiting on a numeric
    retry allowance after the visual evidence stops changing -- ties
    together `OCRRetryGuard` (existing) with `vin_fusion.fuse` (existing) to
    show the combination terminates a session's OCR loop deterministically.

All tests are expected to PASS today (regression lock protecting isolation
guarantees the hotfix's pos10 verifier / dedup work will run inside).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from backend.ai import ocr_contract as C
from backend.ai.ocr_orchestrator import (
    OcrOrchestrator, ROLE_ENGRAVED, ROLE_PADDLE_ENHANCED, ROLE_PADDLE_RAW,
)
from backend.ai.ocr_retry_guard import OCRRetryGuard, STABLE_AMBIGUITY
from backend.ai.vin_fusion import VariantRead, fuse


# Entirely synthetic test values; not copied from production evidence.
SYNTHETIC_PREFIX = "NSTFA814A"
SYNTHETIC_YEAR_CODE = "T"
SYNTHETIC_SUFFIX = "J"
SYNTHETIC_SERIAL = "123456"
SYNTHETIC_VIN = (
    SYNTHETIC_PREFIX + SYNTHETIC_YEAR_CODE + SYNTHETIC_SUFFIX + SYNTHETIC_SERIAL
)

SYNTHETIC_AMBIGUOUS_PREFIX = "NSTHD41AB"
SYNTHETIC_AMBIGUOUS_VIN = (
    SYNTHETIC_AMBIGUOUS_PREFIX + "V" + SYNTHETIC_SUFFIX + "000764"
)


def _crop(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(20, 60, 3), dtype=np.uint8)


def _request(session_id="REAL", capture_generation=1, job_id=1):
    return C.OCRRecognitionRequest(
        session_id=session_id, capture_generation=capture_generation, job_id=job_id,
        crops=[C.CropRef(crop_index=0, image=_crop())], submitted_at=time.time(),
    )


class _SlowEngine(C.OCRRecognizer):
    engine_id = "SLOW"

    def __init__(self, sleep_sec: float, text: str = SYNTHETIC_VIN):
        self.sleep_sec = sleep_sec
        self.text = text

    def initialize(self):
        pass

    def recognize(self, request):
        time.sleep(self.sleep_sec)
        return C.make_result(request, engine_id=self.engine_id, engine_version="1", engine_status=C.OK,
                             raw_sequence=self.text, normalized_sequence=self.text, sequence_confidence=0.9)

    def health(self):
        return C.EngineHealth(self.engine_id, True, "ready")

    def metadata(self):
        return C.EngineMetadata(self.engine_id, "1", C.ALPHANUMERIC_36)

    def close(self):
        pass


class _CrashingEngine(C.OCRRecognizer):
    engine_id = "CRASH"

    def initialize(self):
        pass

    def recognize(self, request):
        raise RuntimeError("simulated engine crash (e.g. native OCR dependency fault)")

    def health(self):
        return C.EngineHealth(self.engine_id, True, "ready")

    def metadata(self):
        return C.EngineMetadata(self.engine_id, "1", C.ALPHANUMERIC_36)

    def close(self):
        pass


class _MismatchedSessionEngine(C.OCRRecognizer):
    """Simulates a buggy/compromised engine adapter that returns a DIFFERENT
    session_id than the one it was asked to process (e.g. a stale in-process
    cache hit, or a copy/paste bug in a newly-added engine adapter)."""
    engine_id = "MISMATCH"

    def __init__(self, wrong_session_id="OTHER_SESSION"):
        self.wrong_session_id = wrong_session_id

    def initialize(self):
        pass

    def recognize(self, request):
        return C.OCRRecognitionResult(
            session_id=self.wrong_session_id, capture_generation=request.capture_generation,
            job_id=request.job_id, engine_id=self.engine_id, engine_version="1",
            charset_id=C.ALPHANUMERIC_36, raw_sequence="WRONGVIN0000000001",
            normalized_sequence="WRONGVIN0000000001", sequence_confidence=0.9, engine_status=C.OK,
        )

    def health(self):
        return C.EngineHealth(self.engine_id, True, "ready")

    def metadata(self):
        return C.EngineMetadata(self.engine_id, "1", C.ALPHANUMERIC_36)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Session-ownership no-leak at the orchestrator layer
# ---------------------------------------------------------------------------
def test_orchestrator_strips_result_with_mismatched_session_id():
    orch = OcrOrchestrator(
        {ROLE_ENGRAVED: _MismatchedSessionEngine(wrong_session_id="SESSION_B"),
         ROLE_PADDLE_RAW: _SlowEngine(0.0, SYNTHETIC_VIN)},
        timeout_ms=2000,
    )
    out = orch.run(
        _request(session_id="SESSION_A"),
        legacy_result_text=SYNTHETIC_VIN, legacy_conf=0.9)
    assert ROLE_ENGRAVED not in out.engine_results, (
        "an engine result whose session_id does not match the request must "
        "never be attributed to this session's evidence"
    )
    assert out.failures.get(ROLE_ENGRAVED) == "session_id_mismatch"
    assert ROLE_PADDLE_RAW in out.engine_results   # the well-behaved engine is unaffected


def test_orchestrator_never_attributes_wrong_session_evidence_even_when_it_would_win():
    """Even if the mismatched engine's text would otherwise be the ONLY
    candidate, it must still be dropped rather than silently promoted."""
    orch = OcrOrchestrator({ROLE_ENGRAVED: _MismatchedSessionEngine(wrong_session_id="SESSION_B")},
                           timeout_ms=2000)
    out = orch.run(_request(session_id="SESSION_A"))
    assert out.engine_results == {}
    assert "WRONGVIN0000000001" not in (out.final_text or "")


# ---------------------------------------------------------------------------
# Per-engine timeout / exception isolation
# ---------------------------------------------------------------------------
def test_slow_engine_times_out_without_blocking_fast_engines():
    orch = OcrOrchestrator(
        {ROLE_ENGRAVED: _SlowEngine(0.5, "SLOW_RESULT"),
         ROLE_PADDLE_RAW: _SlowEngine(0.01, SYNTHETIC_VIN),
         ROLE_PADDLE_ENHANCED: _SlowEngine(0.01, SYNTHETIC_VIN)},
        timeout_ms=120,
    )
    t0 = time.perf_counter()
    out = orch.run(_request(), legacy_result_text=SYNTHETIC_VIN, legacy_conf=0.9)
    elapsed = time.perf_counter() - t0

    assert ROLE_ENGRAVED in out.timeouts
    assert ROLE_PADDLE_RAW in out.engine_results
    assert ROLE_PADDLE_ENHANCED in out.engine_results
    assert elapsed < 0.5, (
        f"a single slow engine must not stretch total latency toward its own "
        f"sleep time (budget=120ms); took {elapsed*1000:.0f}ms"
    )


def test_crashing_engine_is_isolated_and_recorded_as_a_failure():
    orch = OcrOrchestrator(
        {ROLE_ENGRAVED: _CrashingEngine(),
         ROLE_PADDLE_RAW: _SlowEngine(0.0, SYNTHETIC_VIN)},
        timeout_ms=2000,
    )
    out = orch.run(_request(), legacy_result_text=SYNTHETIC_VIN, legacy_conf=0.9)
    assert ROLE_ENGRAVED in out.failures
    assert "RuntimeError" in out.failures[ROLE_ENGRAVED]
    assert ROLE_ENGRAVED not in out.engine_results
    assert ROLE_PADDLE_RAW in out.engine_results, "a crashing engine must not cancel a healthy sibling"


def test_shadow_mode_final_decision_is_unaffected_by_engine_timeouts_or_crashes():
    """In SHADOW (the safe production default), the final decision is ALWAYS
    the legacy value regardless of what the engines under evaluation do --
    timing out or crashing must not change that."""
    orch = OcrOrchestrator(
        {ROLE_ENGRAVED: _CrashingEngine(), ROLE_PADDLE_RAW: _SlowEngine(1.0, "IGNORED")},
        timeout_ms=50, release_mode="SHADOW",
    )
    out = orch.run(_request(), legacy_result_text=SYNTHETIC_VIN, legacy_conf=0.87)
    assert out.final_text == SYNTHETIC_VIN
    assert out.final_confidence == 0.87
    assert out.decision_reason == "SHADOW_ONLY"


# ---------------------------------------------------------------------------
# Retry-budget: deterministic termination once evidence stabilizes
# ---------------------------------------------------------------------------
def test_retry_guard_stops_retrying_once_ambiguous_fusion_result_is_stable():
    """Ties OCRRetryGuard to a real fuse() OCR_AMBIGUOUS outcome: once the
    SAME crop keeps producing the SAME ambiguous fusion result, the guard
    must deny further retries (STABLE_AMBIGUITY) so the session can
    terminate deterministically within its deadline rather than retrying
    forever on an unresolvable read."""
    guard = OCRRetryGuard()
    crop = np.zeros((16, 32, 3), dtype=np.uint8)
    wrong_year_code_vin = SYNTHETIC_AMBIGUOUS_VIN

    first = guard.evaluate("S", [crop], quality=0.5, remaining_sec=5.0)
    assert first.allowed

    r1 = fuse([VariantRead(0, "raw_resized", 0.93, wrong_year_code_vin, crop_quality=0.5)])
    assert r1.status == "OCR_AMBIGUOUS"
    guard.record_outcome("S", r1.validated_vin)

    # SAME crop again (no new evidence, no quality gain) -> must be denied.
    second = guard.evaluate("S", [crop], quality=0.5, remaining_sec=5.0)
    assert not second.allowed
    assert second.reason == STABLE_AMBIGUITY


def test_retry_guard_permits_retry_when_a_genuinely_new_crop_arrives():
    guard = OCRRetryGuard()
    crop_a = np.zeros((16, 32, 3), dtype=np.uint8)
    crop_b = np.ones((16, 32, 3), dtype=np.uint8) * 255
    guard.evaluate("S", [crop_a], quality=0.5, remaining_sec=5.0)
    decision = guard.evaluate("S", [crop_a, crop_b], quality=0.5, remaining_sec=5.0)
    assert decision.allowed, "genuinely new visual evidence must still be allowed to retry"
