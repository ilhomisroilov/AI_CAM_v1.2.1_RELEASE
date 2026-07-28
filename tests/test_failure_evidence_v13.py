"""v1.3.0 items 10/11 — failure-evidence images.

Invariant: decoded_frames > 0 AND no final VIN => evidence image path is non-empty.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backend.config import CROPS_DIR


def _crop():
    return np.random.randint(0, 255, (48, 160, 3), dtype=np.uint8)


def test_evidence_uses_best_crop(pipeline_instance):
    p = pipeline_instance
    p.plc_on()
    sess = p._sessions[p._active_session_id]
    sess.frames_decoded = 5
    sess.best_crop = _crop()
    rel = p._save_failure_evidence(sess)
    assert rel and rel.startswith("crops/")
    assert (Path(CROPS_DIR) / rel.split("/", 1)[1]).exists()
    assert "crop" in rel


def test_evidence_falls_back_to_latest_frame(pipeline_instance):
    p = pipeline_instance
    p.plc_on()
    sess = p._sessions[p._active_session_id]
    sess.frames_decoded = 3
    sess.best_crop = None
    p._latest_frame = np.random.randint(0, 255, (64, 200, 3), dtype=np.uint8)
    rel = p._save_failure_evidence(sess)
    assert rel and "frame" in rel
    assert (Path(CROPS_DIR) / rel.split("/", 1)[1]).exists()


def test_no_evidence_when_nothing_decoded(pipeline_instance):
    p = pipeline_instance
    p.plc_on()
    sess = p._sessions[p._active_session_id]
    sess.best_crop = None
    p._latest_frame = None
    assert p._save_failure_evidence(sess) is None


def test_finalize_no_vin_with_decoded_frames_has_evidence(pipeline_instance):
    """The item-11 invariant, end to end through finalize."""
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id
    sess = p._sessions[sid]
    sess.frames_decoded = 4
    sess.best_crop = _crop()
    p._finalize_session(sid, reason="TEST_NO_VIN")
    assert sess.vin is None
    assert sess.evidence_rel_path is not None and sess.evidence_rel_path.startswith("crops/")
