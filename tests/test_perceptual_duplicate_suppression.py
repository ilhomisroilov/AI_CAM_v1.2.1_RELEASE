"""
============================================================
test_perceptual_duplicate_suppression.py
============================================================
Regression-lock tests for the REAL production near-duplicate-frame dedup
already implemented in `backend/ai/ocr_worker.py`
(`OCRWorker._crop_dhash` / `OCRWorker._group_near_duplicate_crops`) and its
plumbing into `backend/ai/vin_fusion.py` via `VariantRead.evidence_group`.

Why this file exists: the hotfix brief calls for "perceptual duplicate
suppression" as new work. Investigation found this is ALREADY IMPLEMENTED
(a 64-bit difference-hash grouping keyed by `VIN.independent_crop_hash_distance`,
default max_dist=5) -- these tests exercise the REAL functions (not a
reimplementation) so:
  1. the lead can see exactly what already exists and does not need to
     reinvent it, and
  2. any hotfix change to this code path is instantly caught by these tests
     if it stops suppressing near-duplicate camera frames as independent
     evidence (which would silently make the pos5/pos10 "independent crop"
     redundancy requirements gameable by a stuck/near-static camera feed).

All tests here are expected to PASS today (regression lock, not new spec).
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.ai.ocr_worker import OCRWorker
from backend.ai.vin_fusion import VariantRead, fuse
from backend import config as cfg


# Entirely synthetic test value; not copied from production evidence.
SYNTHETIC_PREFIX = "NSTFB814E"
SYNTHETIC_YEAR_CODE = "T"
SYNTHETIC_SUFFIX = "J"
SYNTHETIC_SERIAL = "039378"
SYNTHETIC_VIN = (
    SYNTHETIC_PREFIX + SYNTHETIC_YEAR_CODE + SYNTHETIC_SUFFIX + SYNTHETIC_SERIAL
)


def _worker() -> OCRWorker:
    # Lightweight: OCRWorker.__init__ does not start a thread/process pool.
    return OCRWorker(on_result=lambda *a, **k: None)


def _plate_crop(seed: int = 0, noise: int = 0, shape=(48, 160, 3)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=shape, dtype=np.uint8)
    if noise:
        jitter = rng.integers(-noise, noise + 1, size=shape).astype(np.int16)
        base = np.clip(base.astype(np.int16) + jitter, 0, 255).astype(np.uint8)
    return base


# ---------------------------------------------------------------------------
# Unit level: OCRWorker._crop_dhash / _group_near_duplicate_crops
# ---------------------------------------------------------------------------
def test_identical_frames_hash_to_the_same_group():
    w = _worker()
    frame = _plate_crop(seed=1)
    groups = w._group_near_duplicate_crops([frame, frame.copy(), frame.copy()])
    assert len(set(groups.values())) == 1, "byte-identical frames must share one evidence group"


def test_near_identical_frames_within_hash_distance_share_a_group(monkeypatch):
    """A camera repeating (near-)the same frame across successive grabs (small
    sensor noise, no real motion) must be treated as ONE independent
    observation, not several."""
    monkeypatch.setattr(cfg.VIN, "independent_crop_hash_distance", 5)
    w = _worker()
    frame_a = _plate_crop(seed=2, noise=0)
    frame_b = _plate_crop(seed=2, noise=2)   # tiny sensor-noise perturbation of the SAME plate
    groups = w._group_near_duplicate_crops([frame_a, frame_b])
    assert len(set(groups.values())) == 1, (
        "near-identical (small-noise) repeated frames must be grouped as one "
        "independent evidence source"
    )


def test_visually_distinct_frames_get_separate_groups():
    w = _worker()
    frame_a = _plate_crop(seed=10)
    frame_b = _plate_crop(seed=999)   # unrelated random content -> large hash distance
    groups = w._group_near_duplicate_crops([frame_a, frame_b])
    assert len(set(groups.values())) == 2, (
        "genuinely distinct camera frames must be counted as independent evidence"
    )


def test_group_near_duplicate_crops_is_recomputed_fresh_per_call_no_cross_session_leak():
    """The grouping dict is derived fresh from the crops passed in on THIS
    call; it must not carry state between sessions (no memory of a previous
    session's frames bleeding into a new one's grouping)."""
    w = _worker()
    session_a_frames = [_plate_crop(seed=1), _plate_crop(seed=1)]  # 2 identical -> 1 group
    groups_a = w._group_near_duplicate_crops(session_a_frames)
    assert len(set(groups_a.values())) == 1

    session_b_frames = [_plate_crop(seed=42), _plate_crop(seed=43)]  # 2 distinct -> 2 groups
    groups_b = w._group_near_duplicate_crops(session_b_frames)
    assert len(set(groups_b.values())) == 2
    # session B's grouping must not have been influenced by session A's frames/hashes
    assert groups_b == w._group_near_duplicate_crops(session_b_frames)


# ---------------------------------------------------------------------------
# Integration level: evidence_group correctly caps fusion's n_crops
# ---------------------------------------------------------------------------
def test_worker_assigned_evidence_groups_cap_fusion_n_crops():
    """End-to-end plumbing check: evidence_group values produced by the REAL
    `_group_near_duplicate_crops` (not hand-picked in the test) correctly cap
    `FusionResult.n_crops` when handed to `vin_fusion.fuse`, so a camera that
    repeats the same frame cannot manufacture extra 'independent' crops."""
    w = _worker()
    vin = SYNTHETIC_VIN
    real_frame = _plate_crop(seed=7)
    duplicate_of_same_frame = _plate_crop(seed=7)  # byte-identical repeat grab
    groups = w._group_near_duplicate_crops([real_frame, duplicate_of_same_frame])
    assert len(set(groups.values())) == 1

    reads = [
        VariantRead(crop_index=idx, variant_name="raw_resized", ocr_conf=0.95,
                    raw_text=vin, crop_quality=0.9, evidence_group=groups[idx])
        for idx in (0, 1)
    ]
    r = fuse(reads, accept_min_crops=2)
    assert r.n_crops == 1, "two grabs of the same physical frame must fuse to n_crops=1"
    assert r.status == "OCR_AMBIGUOUS", (
        "n_crops=1 must not satisfy accept_min_crops=2 merely because the "
        "duplicate frame arrived under a second crop_index"
    )


def test_two_genuinely_different_frames_are_not_merged_and_can_satisfy_min_crops():
    w = _worker()
    vin = SYNTHETIC_VIN
    frame_a = _plate_crop(seed=100)
    frame_b = _plate_crop(seed=200)
    groups = w._group_near_duplicate_crops([frame_a, frame_b])
    assert len(set(groups.values())) == 2

    reads = [
        VariantRead(crop_index=idx, variant_name="raw_resized", ocr_conf=0.95,
                    raw_text=vin, crop_quality=0.9, evidence_group=groups[idx])
        for idx in (0, 1)
    ]
    r = fuse(reads, accept_min_crops=2)
    assert r.n_crops == 2
    assert r.status == "ACCEPT"
