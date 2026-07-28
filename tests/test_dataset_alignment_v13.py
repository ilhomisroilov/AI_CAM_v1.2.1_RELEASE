"""v1.3.0 items 7/8/9 — character alignment source + training eligibility.

Equal-width 17-sector split must never yield a production training-eligible
sample; engraved-engine boxes are preferred; adaptive projection is training-ready;
a flat line degrades to EQUAL_SPLIT_FALLBACK -> PENDING_REVIEW / not eligible.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("PIL")
from backend.ai.ocr_collector import ActiveLearningCollector, _adaptive_boxes


def _flat_line(W=180, H=48):
    return np.full((H, W), 128, np.float32)


def _structured_line(n=3, W=180, H=48):
    g = np.full((H, W), 210, np.float32)
    for i in range(n):
        x = int((i + 0.5) * W / n)
        g[:, max(0, x - 2):x + 3] = 15  # dark vertical strokes -> projection signal
    return g


def _collector(tmp_path):
    saved = {}
    col = ActiveLearningCollector(
        str(tmp_path),
        store_fn=lambda it: (saved.__setitem__(it["character_position"], it) or True),
        enabled=True,
    )
    return col, saved


def _job(line, boxes=None):
    per_char = []
    for i, ch in enumerate("NST"):
        pc = {"char": ch, "conf": 0.9, "position": i, "reason": "RAW_FINAL_DISAGREEMENT"}
        if boxes is not None:
            pc["box"] = boxes[i]
        per_char.append(pc)
    return {"session_id": "S1", "expected_vin": "NSTFA814ATJ123456",
            "trusted_label": True, "normalized_line": line, "per_char": per_char}


def _metadata(tmp_path):
    return json.load(open(next(iter(tmp_path.rglob("metadata.json"))), encoding="utf-8"))


def test_adaptive_boxes_returns_source():
    boxes, source = _adaptive_boxes(_structured_line(), 3)
    assert len(boxes) == 3
    assert source in ("ADAPTIVE_PROJECTION", "EQUAL_SPLIT_FALLBACK")


def test_flat_line_is_equal_split_not_training_eligible(tmp_path):
    col, saved = _collector(tmp_path)
    col._process(_job(_flat_line()))
    md = _metadata(tmp_path)
    assert md["alignment_source"] == "EQUAL_SPLIT_FALLBACK"
    assert md["training_eligible"] is False
    assert md["review_status"] == "PENDING_REVIEW"
    assert saved and all(it["training_eligible"] is False for it in saved.values())


def test_structured_line_is_adaptive_and_training_eligible(tmp_path):
    col, saved = _collector(tmp_path)
    col._process(_job(_structured_line()))
    md = _metadata(tmp_path)
    assert md["alignment_source"] == "ADAPTIVE_PROJECTION"
    assert md["training_eligible"] is True
    assert all(it["training_eligible"] is True for it in saved.values())


def test_engraved_boxes_are_preferred(tmp_path):
    col, saved = _collector(tmp_path)
    boxes = [(0, 0, 60, 48), (60, 0, 60, 48), (120, 0, 60, 48)]
    col._process(_job(_flat_line(), boxes=boxes))  # flat line, but engine gave real boxes
    md = _metadata(tmp_path)
    assert md["alignment_source"] == "ENGRAVED_BOXES"
    assert md["training_eligible"] is True


def test_no_production_equal_split_is_training_eligible(tmp_path):
    """Acceptance invariant (9.2): equal-split samples are never training-eligible."""
    col, saved = _collector(tmp_path)
    col._process(_job(_flat_line()))
    for it in saved.values():
        assert not (it["alignment_source"].startswith("EQUAL_SPLIT") and it["training_eligible"])
