"""
============================================================
test_pre_ocr_telemetry.py — Pre-OCR failure stage observability
============================================================
INCIDENT: ko'p sessiyalarda session ochilgan, kamera `mLIStart` javob bergan,
RFID ishlagan, lekin `[OCR TRIGGER]` UMUMAN bo'lmagan. Mavjud logging
qaysi bosqichda yo'qotish sodir bo'lganini ko'rsatmaydi, shuning uchun
incident forensikasi imkonsiz bo'lib qoldi.

Bu fayl tekshiradi: har sessiya o'z zanjirini (payload -> decode -> detection
-> crop -> OCR submit -> OCR result) SANAYDI va yakunda ANIQ `failure_stage`
bilan yopiladi. "UNKNOWN" — qabul qilinmaydigan natija.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend import config as cfg
from backend.pipeline import SessionState

from conftest import make_ocr_result, wait_until


@pytest.fixture
def telemetry_pipeline(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.SESSION, "hold_until_deadline", False)
    monkeypatch.setattr(cfg.SESSION, "require_rfid", False)
    monkeypatch.setattr(cfg.SESSION, "timeout_sec", 5.0)
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    monkeypatch.setattr(p, "pause_camera_stream", lambda: None)
    monkeypatch.setattr(p, "_ensure_camera_stream", lambda: True)
    return p


def _session(p):
    return p._sessions[p._active_session_id]


def test_counters_exist_and_start_at_zero(telemetry_pipeline):
    """Har sessiya o'z pre-OCR hisoblagichlariga ega bo'lishi kerak."""
    p = telemetry_pipeline
    p.plc_on()
    s = _session(p)
    for name in ("payloads_received", "frames_decoded", "decode_failures",
                 "yolo_inference_count", "yolo_detection_count",
                 "crops_created", "crops_rejected",
                 "ocr_jobs_submitted", "ocr_jobs_completed"):
        assert hasattr(s, name), f"Session.{name} hisoblagichi bo'lishi kerak"
        assert getattr(s, name) == 0


def test_no_payload_is_classified(telemetry_pipeline):
    """Kamera umuman payload bermagan sessiya -> NO_PAYLOAD."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._finalize_session(sid, reason="HARD_DEADLINE")
    assert p._sessions[sid].failure_stage == "NO_PAYLOAD"


def test_decode_failure_is_classified(telemetry_pipeline):
    """Payload keldi, lekin dekod bo'lmadi -> DECODE_FAILED."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 4)
    p._bump_capture_counter("decode_failures", 4)
    p._finalize_session(sid, reason="HARD_DEADLINE")
    assert p._sessions[sid].failure_stage == "DECODE_FAILED"


def test_no_detection_is_classified(telemetry_pipeline):
    """Kadr dekod bo'ldi, YOLO plastinka topmadi -> NO_DETECTION."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 10)
    p._bump_capture_counter("frames_decoded", 10)
    p._bump_capture_counter("yolo_inference_count", 10)
    p._finalize_session(sid, reason="HARD_DEADLINE")
    assert p._sessions[sid].failure_stage == "NO_DETECTION"


def test_crop_rejected_is_classified(telemetry_pipeline):
    """Detection bor, lekin barcha croplar sifat gate'idan o'tmadi -> CROP_REJECTED."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 10)
    p._bump_capture_counter("frames_decoded", 10)
    p._bump_capture_counter("yolo_detection_count", 6)
    p._bump_capture_counter("crops_created", 6)
    p._bump_capture_counter("crops_rejected", 6)
    p._finalize_session(sid, reason="HARD_DEADLINE")
    assert p._sessions[sid].failure_stage == "CROP_REJECTED"


def test_crop_available_but_ocr_not_submitted_is_invariant_violation(telemetry_pipeline, caplog):
    """
    Crop tayyor bo'lsa-yu OCR ga YUBORILMAGAN bo'lsa — bu SOFTWARE INVARIANT
    buzilishi. CRITICAL log talab qilinadi (jim o'tib ketmasin).
    """
    import logging
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 10)
    p._bump_capture_counter("frames_decoded", 10)
    p._bump_capture_counter("yolo_detection_count", 6)
    p._bump_capture_counter("crops_created", 6)     # qabul qilingan, rad etilmagan
    caplog.set_level(logging.CRITICAL)
    p._finalize_session(sid, reason="HARD_DEADLINE")

    assert p._sessions[sid].failure_stage == "OCR_NOT_SUBMITTED"
    assert any("CROP_AVAILABLE_BUT_OCR_NOT_SUBMITTED" in r.message
               for r in caplog.records if r.levelno == logging.CRITICAL), \
        "invariant buzilishi CRITICAL bilan loglanishi kerak"


def test_ocr_no_read_is_classified(telemetry_pipeline):
    """OCR ishladi, lekin VIN o'qilmadi -> OCR_NO_READ (OCR ishga tushmadi EMAS)."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 10)
    p._bump_capture_counter("frames_decoded", 10)
    p._bump_capture_counter("yolo_detection_count", 6)
    p._bump_capture_counter("crops_created", 6)
    p._bump_capture_counter("ocr_jobs_submitted", 1)
    p._bump_capture_counter("ocr_jobs_completed", 1)
    p._finalize_session(sid, reason="HARD_DEADLINE")
    assert p._sessions[sid].failure_stage == "OCR_NO_READ"


def test_success_has_no_failure_stage(telemetry_pipeline):
    """VIN olingan sessiya -> failure_stage = None (muvaffaqiyat)."""
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 10)
    p._bump_capture_counter("frames_decoded", 10)
    p._bump_capture_counter("yolo_detection_count", 6)
    p._bump_capture_counter("crops_created", 6)
    p._bump_capture_counter("ocr_jobs_submitted", 1)
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))

    assert wait_until(lambda: p._sessions[sid].state == SessionState.SUCCESS, timeout=2.0)
    assert p._sessions[sid].failure_stage is None


def test_missed_capture_window_stage(telemetry_pipeline, monkeypatch):
    """MISSED_CAPTURE_WINDOW o'z failure_stage'ini oladi (UNKNOWN emas)."""
    p = telemetry_pipeline
    monkeypatch.setattr(cfg.SESSION, "max_capture_delay_ms", 1, raising=False)
    import time as _t
    p._start_session(99, _t.time() - 5.0)          # 5s kech boshlandi
    missed = [s for s in p._sessions.values()
              if s.failure_reason == "MISSED_CAPTURE_WINDOW"]
    assert missed, "MISSED_CAPTURE_WINDOW sessiyasi yaratilishi kerak"
    assert missed[0].failure_stage == "MISSED_CAPTURE_WINDOW"


def test_session_summary_line_emitted_once(telemetry_pipeline, caplog):
    """Har sessiya uchun BITTA yakuniy [SESSION SUMMARY] qatori (per-frame spam emas)."""
    import logging
    p = telemetry_pipeline
    p.plc_on()
    sid = p._active_session_id
    p._bump_capture_counter("payloads_received", 3)
    caplog.set_level(logging.INFO)
    p._finalize_session(sid, reason="HARD_DEADLINE")

    summaries = [r for r in caplog.records if "[SESSION SUMMARY]" in r.message]
    assert len(summaries) == 1, f"aynan bitta summary kutilgan, olindi: {len(summaries)}"
    msg = summaries[0].message
    for field in ("session=", "payloads=", "decoded=", "detections=", "crops=",
                  "ocr_submitted=", "ocr_completed=", "failure_stage="):
        assert field in msg, f"summary qatorida '{field}' bo'lishi kerak"
