from __future__ import annotations

import numpy as np

from backend.ai.ocr_retry_guard import (
    IMPROVED_QUALITY,
    INSUFFICIENT_DEADLINE,
    NEW_EVIDENCE,
    OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT,
    OCRRetryGuard,
    RETRY_ALLOWED,
    STABLE_AMBIGUITY,
)


def _crop(value: int):
    return np.full((16, 32, 3), value, dtype=np.uint8)


def test_same_evidence_does_not_submit_another_ocr_job():
    guard = OCRRetryGuard()
    first = guard.evaluate("S", [_crop(1)], quality=0.5, remaining_sec=5)
    guard.record_outcome("S", "NSTF?814ETJ000001", positions=[4])
    second = guard.evaluate("S", [_crop(1)], quality=0.5, remaining_sec=5)
    assert first.allowed and first.reason == RETRY_ALLOWED
    assert not second.allowed and second.reason == STABLE_AMBIGUITY


def test_new_crop_permits_retry():
    guard = OCRRetryGuard()
    guard.evaluate("S", [_crop(1)], quality=0.5, remaining_sec=5)
    decision = guard.evaluate("S", [_crop(1), _crop(2)], quality=0.5, remaining_sec=5)
    assert decision.allowed and decision.reason == NEW_EVIDENCE


def test_improved_quality_permits_retry():
    guard = OCRRetryGuard(quality_improvement=0.05)
    crop = _crop(1)
    guard.evaluate("S", [crop], quality=0.5, remaining_sec=5)
    decision = guard.evaluate("S", [crop], quality=0.61, remaining_sec=5)
    assert decision.allowed and decision.reason == IMPROVED_QUALITY


def test_deadline_budget_prevents_pointless_retry():
    guard = OCRRetryGuard(minimum_budget_sec=1.0)
    decision = guard.evaluate("S", [_crop(1)], remaining_sec=0.25)
    assert not decision.allowed and decision.reason == INSUFFICIENT_DEADLINE


def test_plate_exit_is_terminal_in_guard():
    guard = OCRRetryGuard()
    guard.evaluate("S", [_crop(1)], remaining_sec=5)
    guard.mark_plate_exit("S")
    decision = guard.evaluate("S", [_crop(2)], remaining_sec=5)
    assert not decision.allowed
    assert decision.reason == OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT


def test_plate_exit_ambiguous_result_does_not_reacquire_capture_or_wait(
    pipeline_instance,
):
    p = pipeline_instance
    p.plc_on()
    sid = p.capture_owner_id
    assert sid
    p._plate_locked = True
    p._release_capture(sid, reason="plate_exit")
    assert p.capture_owner_id is None

    p._on_ocr_fail(sid, "NSTF?814ETJ000001")

    assert p.capture_owner_id is None
    assert p._plate_locked is True
    assert (
        p._sessions[sid].ocr_terminal_reason
        == OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT
    )
    assert p._sessions[sid].state.value in ("TIMEOUT", "FAILED")
