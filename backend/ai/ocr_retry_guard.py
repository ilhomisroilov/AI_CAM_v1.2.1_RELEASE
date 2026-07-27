"""Stable-evidence OCR retry policy.

Retries are admitted only for new visual evidence or a material quality gain.
Plate exit and insufficient deadline budget are terminal, so a session never
waits for HARD_DEADLINE merely because a numeric retry allowance remains.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

RETRY_ALLOWED = "RETRY_ALLOWED"
NEW_EVIDENCE = "NEW_EVIDENCE"
IMPROVED_QUALITY = "IMPROVED_QUALITY"
STABLE_AMBIGUITY = "STABLE_AMBIGUITY"
INSUFFICIENT_DEADLINE = "INSUFFICIENT_DEADLINE"
OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT = "OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT"


def _image_hash(image) -> str:
    array = np.ascontiguousarray(np.asarray(image))
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(array.tobytes())
    return digest.hexdigest()


def evidence_signature(crops: Iterable, crop_boxes: Optional[Iterable] = None) -> str:
    """Order-independent signature for distinct selected visual evidence."""
    crop_hashes = sorted({_image_hash(crop) for crop in crops if crop is not None})
    boxes = sorted(repr(box) for box in (crop_boxes or ()))
    payload = "|".join(crop_hashes + boxes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def uncertain_positions(raw: str) -> tuple[int, ...]:
    text = str(raw or "")
    return tuple(i for i, char in enumerate(text) if char in ("?", "*", "_"))


@dataclass
class RetryDecision:
    allowed: bool
    reason: str
    signature: str


@dataclass
class _State:
    signature: str = ""
    quality: float = 0.0
    raw_result: str = ""
    uncertain: tuple[int, ...] = field(default_factory=tuple)
    terminal_reason: str = ""


class OCRRetryGuard:
    def __init__(self, quality_improvement: float = 0.05,
                 minimum_budget_sec: float = 0.75) -> None:
        self.quality_improvement = float(quality_improvement)
        self.minimum_budget_sec = float(minimum_budget_sec)
        self._states: dict[object, _State] = {}

    def reset(self, session_id=None) -> None:
        if session_id is None:
            self._states.clear()
        else:
            self._states.pop(session_id, None)

    def evaluate(self, session_id, crops, *, quality: float = 0.0,
                 crop_boxes=None, remaining_sec: Optional[float] = None,
                 plate_exited: bool = False) -> RetryDecision:
        signature = evidence_signature(crops, crop_boxes)
        state = self._states.get(session_id)
        if plate_exited:
            state = state or _State()
            state.terminal_reason = OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT
            self._states[session_id] = state
            return RetryDecision(False, state.terminal_reason, signature)
        if remaining_sec is not None and remaining_sec < self.minimum_budget_sec:
            state = state or _State()
            state.terminal_reason = INSUFFICIENT_DEADLINE
            self._states[session_id] = state
            return RetryDecision(False, INSUFFICIENT_DEADLINE, signature)
        if state is None:
            self._states[session_id] = _State(signature=signature, quality=float(quality))
            return RetryDecision(True, RETRY_ALLOWED, signature)
        if state.terminal_reason:
            return RetryDecision(False, state.terminal_reason, signature)
        if signature != state.signature:
            state.signature = signature
            state.quality = float(quality)
            return RetryDecision(True, NEW_EVIDENCE, signature)
        if float(quality) >= state.quality + self.quality_improvement:
            state.quality = float(quality)
            return RetryDecision(True, IMPROVED_QUALITY, signature)
        return RetryDecision(False, STABLE_AMBIGUITY, signature)

    def record_outcome(self, session_id, raw_result: str,
                       positions: Optional[Iterable[int]] = None) -> None:
        state = self._states.setdefault(session_id, _State())
        state.raw_result = str(raw_result or "")
        state.uncertain = tuple(
            positions if positions is not None else uncertain_positions(raw_result)
        )

    def mark_plate_exit(self, session_id) -> None:
        state = self._states.setdefault(session_id, _State())
        state.terminal_reason = OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT

    def state(self, session_id) -> dict:
        state = self._states.get(session_id, _State())
        return {
            "signature": state.signature,
            "quality": state.quality,
            "raw_result": state.raw_result,
            "uncertain_positions": state.uncertain,
            "terminal_reason": state.terminal_reason,
        }
