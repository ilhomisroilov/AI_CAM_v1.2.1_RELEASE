"""
============================================================
test_v121_safety_invariants.py — safety invariants at risk during the OCR refactor
============================================================
The session-safety review (docs/v1.2.1/SESSION_SAFETY_REVIEW.md) flagged two
things to lock BEFORE any OCR-engine work:

  * "Non-terminal sessions are NOT removed from session history" had NO direct
    test — a draining WAITING_RESULTS session that is pruned would have its late
    OCR/RFID result rejected as UNKNOWN_SESSION, silently losing traceability.
  * The single highest regression risk of a new engine adapter is dropping the
    ownership identifiers (session_id / capture_generation / job_id). We lock that
    the legacy adapter round-trips them even on non-OK (EMPTY/unavailable) results.
"""
from __future__ import annotations

import numpy as np

from backend.ai import ocr_contract as C
from backend.ai.engines import PaddleOCRLegacyAdapter


# ---------------------------------------------------------------------------
# Draining (WAITING_RESULTS) session must survive history pruning
# ---------------------------------------------------------------------------
def test_prune_keeps_draining_waiting_results_session(pipeline_instance):
    from backend.pipeline import SessionState

    p = pipeline_instance
    p.plc_on()
    draining = p._active_session_id
    assert p._sessions[draining].state == SessionState.ACTIVE

    # Hand off capture -> session keeps draining (still awaiting OCR/RFID).
    p._release_capture(draining, reason="test_handoff")
    assert p._sessions[draining].state == SessionState.WAITING_RESULTS

    # Shrink history and flood with terminal sessions to force pruning.
    p._session_history_max = 1
    for _ in range(5):
        p.plc_on()
        sid = p._active_session_id
        if sid is not None:
            p._finalize_session(sid, reason="TEST_FORCE_CLOSE")

    # The draining session must NOT be pruned — otherwise its late result would be
    # rejected as an unknown session and traceability would be lost.
    assert draining in p._sessions, "WAITING_RESULTS session was wrongly pruned"
    assert p._sessions[draining].state == SessionState.WAITING_RESULTS


# ---------------------------------------------------------------------------
# Adapter round-trips ownership identifiers even on non-OK results
# ---------------------------------------------------------------------------
def _request(session_id="S", capture_generation="G", job_id="J"):
    return C.OCRRecognitionRequest(
        session_id=session_id, capture_generation=capture_generation, job_id=job_id,
        crops=[C.CropRef(crop_index=0, image=np.zeros((10, 30, 3), dtype=np.uint8))],
        submitted_at=1.0, charset_profile=C.ALPHANUMERIC_36)


class _EmptyEngine:
    def read(self, img):
        return []   # engine runs, finds nothing -> not accepted


def test_adapter_preserves_identifiers_on_empty_result():
    adapter = PaddleOCRLegacyAdapter()
    adapter.attach_test_engine(_EmptyEngine())
    result = adapter.recognize(_request())
    assert result.engine_status in (C.EMPTY,)
    assert (result.session_id, result.capture_generation, result.job_id) == ("S", "G", "J")
    assert result.normalized_sequence == ""


def test_adapter_preserves_identifiers_on_unavailable_engine():
    adapter = PaddleOCRLegacyAdapter()
    adapter.attach_test_engine(None)   # deliberately unavailable
    result = adapter.recognize(_request())
    assert result.engine_status == C.ENGINE_UNAVAILABLE
    assert (result.session_id, result.capture_generation, result.job_id) == ("S", "G", "J")


def test_adapter_preserves_identifiers_when_no_crops():
    adapter = PaddleOCRLegacyAdapter()
    adapter.attach_test_engine(_EmptyEngine())
    req = C.OCRRecognitionRequest(session_id="S", capture_generation="G", job_id="J",
                                  crops=[], submitted_at=1.0)
    result = adapter.recognize(req)
    assert (result.session_id, result.capture_generation, result.job_id) == ("S", "G", "J")
