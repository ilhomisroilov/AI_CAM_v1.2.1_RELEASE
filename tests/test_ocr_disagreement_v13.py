"""v1.3.0 items 2/4/5 — disagreement audit, frame independence, weak-position guard."""
from __future__ import annotations

from backend.ai.ocr_disagreement import (
    char_disagreements, independent_frame_count, weak_position_ok,
)


def test_raw_final_confusable_ef_flagged():
    # raw read F, validator produced E at position 0
    d = char_disagreements({"ENGRAVED_V121": "E", "PADDLE_RAW": "F", "PADDLE_ENHANCED": "F"},
                           raw="F", final="E")
    assert d[0]["position"] == 0
    assert "RAW_FINAL_DISAGREEMENT" in d[0]["reasons"]
    assert "CONFUSABLE_E_F" in d[0]["reasons"]
    assert "ENGINE_DISAGREEMENT" in d[0]["reasons"]
    assert d[0]["weak_position"] is True


def test_uv_engine_disagreement_flagged():
    d = char_disagreements({"ENGRAVED_V121": "U", "PADDLE_RAW": "V", "PADDLE_ENHANCED": "V"},
                           raw="V", final="V")
    assert "ENGINE_DISAGREEMENT" in d[0]["reasons"]
    assert "CONFUSABLE_U_V" in d[0]["reasons"]


def test_validator_changed_character_flagged():
    # final 'E' but no engine ever produced 'E'
    d = char_disagreements({"ENGRAVED_V121": "F", "PADDLE_RAW": "F"}, raw="F", final="E")
    assert "VALIDATOR_CHANGED_CHARACTER" in d[0]["reasons"]


def test_agreement_produces_no_disagreement():
    d = char_disagreements({"ENGRAVED_V121": "N", "PADDLE_RAW": "N", "PADDLE_ENHANCED": "N"},
                           raw="N", final="N")
    assert d == []


def test_independent_frames_counts_distinct_source_hashes():
    # 3 preprocess variants of the SAME frame -> 1 independent frame
    variants = [{"source_frame_hash": "h1", "profile": p} for p in ("clahe", "sharpen", "blackhat")]
    assert independent_frame_count(variants) == 1
    two = variants + [{"source_frame_hash": "h2", "profile": "raw"}]
    assert independent_frame_count(two) == 2


def test_weak_position_guard_blocks_single_variant():
    # 'V' from one frame's single engine -> blocked
    assert weak_position_ok("V", engine_chars=["V"], independent_frames=1, engraved_conf=0.5) is False
    # two independent frames -> allowed
    assert weak_position_ok("V", engine_chars=["V"], independent_frames=2, engraved_conf=0.5) is True
    # two engines agree -> allowed
    assert weak_position_ok("V", engine_chars=["V", "V"], independent_frames=1, engraved_conf=0.5) is True
    # high engraved confidence -> allowed
    assert weak_position_ok("V", engine_chars=["V"], independent_frames=1, engraved_conf=0.9) is True


def test_non_weak_char_always_allowed():
    assert weak_position_ok("N", engine_chars=["N"], independent_frames=1, engraved_conf=0.0) is True
