"""v1.3.0 items 1 & 6 — strict YOLO submit gate + VIN-locked retry cancel."""
from __future__ import annotations

import numpy as np

from backend import config as cfg
from conftest import make_ocr_result


def _crop():
    return np.random.randint(0, 255, (48, 160, 3), dtype=np.uint8)


def test_yolo_899_must_not_submit(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.DETECTION, "ocr_submit_min_yolo_conf", 0.90)
    p.plc_on()
    p._session_max_yolo_conf = 0.899
    before = p._ocr_attempts
    assert p._submit_ocr_frames([_crop()], reason="test") is False
    assert p._ocr_attempts == before, "sub-0.90 YOLO must not reach an OCR attempt"


def test_yolo_900_may_submit(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.DETECTION, "ocr_submit_min_yolo_conf", 0.90)
    monkeypatch.setattr(p.ocr, "submit_frames", lambda *a, **k: None)
    p.plc_on()
    p._session_max_yolo_conf = 0.90
    before = p._ocr_attempts
    assert p._submit_ocr_frames([_crop()], reason="test") is True
    assert p._ocr_attempts == before + 1


def test_best_q_cannot_override_yolo_gate(pipeline_instance, monkeypatch):
    """A high crop quality must not rescue a low YOLO confidence."""
    p = pipeline_instance
    monkeypatch.setattr(cfg.DETECTION, "ocr_submit_min_yolo_conf", 0.90)
    monkeypatch.setattr(p, "_session_best_crop_quality", 0.99)
    p.plc_on()
    p._session_max_yolo_conf = 0.80
    assert p._submit_ocr_frames([_crop()], reason="test") is False


def test_vin_locked_cancels_further_ocr(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.DETECTION, "ocr_submit_min_yolo_conf", 0.0)  # isolate the lock
    monkeypatch.setattr(p.ocr, "submit_frames", lambda *a, **k: None)
    p.plc_on()
    sid = p._active_session_id
    p._sessions[sid].vin_locked = True
    p._session_max_yolo_conf = 0.99
    assert p._submit_ocr_frames([_crop()], reason="retry") is False
    assert p._sessions[sid].ocr_terminal_reason == "VIN_LOCKED"


def test_accepted_vin_sets_lock(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(p.ocr, "submit_frames", lambda *a, **k: None)
    p.plc_on()
    sid = p._active_session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))
    assert p._sessions[sid].vin_locked is True
