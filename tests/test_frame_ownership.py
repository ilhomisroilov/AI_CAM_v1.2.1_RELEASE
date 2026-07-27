"""
Rapid MVP — Item 9: capture/inference frame-egalik xavfsizligi.

OCR jobi submit qilinganda joriy FAOL sessiya session_id si biriktiriladi
(pipeline.py: current_sid = _active_session_id -> submit_frames(session_id=...))
va natija _on_ocr_result da egalik bo'yicha tekshiriladi. Bu fayl QO'SHIMCHA
kafolatni tekshiradi: sessiya tugagach (stop_processing) navbatdagi crop buferi
BEKOR qilinadi -> oldingi kuzovning qolgan frame'i keyingi sessiyada submit
bo'lib, uning VIN'iga AYLANMAYDI.

⚠️ PERF: to'liq capture/YOLO/JPEG thread izolyatsiyasi (MJPEG YOLO sabab
sekinlashmasligi) — performance tuninga oid, real kamera bilan o'lchanadi
(HIL/perf PENDING). Bu yerda faqat frame->session KORREKTLIGI.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend import config as cfg


@pytest.fixture
def raw_pipe(monkeypatch):
    """Real Pipeline, lekin OCR/collector hayotiy sikli no-op (thread yo'q).
    stop_processing/start_processing REAL (monkeypatch QILINMAYDI)."""
    from backend.pipeline import Pipeline
    monkeypatch.setattr(cfg.RFID, "enabled", False)
    p = Pipeline()
    monkeypatch.setattr(p.ocr, "start", lambda: None)
    monkeypatch.setattr(p.ocr, "stop", lambda: None)
    monkeypatch.setattr(p.collector, "start", lambda: None)
    monkeypatch.setattr(p.collector, "stop", lambda: None)
    p._camera_connected = True
    return p


def _crop():
    return np.zeros((32, 96, 3), dtype=np.uint8)


def test_stop_processing_discards_pending_frame_buffer(raw_pipe):
    p = raw_pipe
    p._processing = True
    p._event_buf = [(_crop(), 0.9), (_crop(), 0.8)]      # kuzov A ning croplari
    p._confirm = 3
    p._plate_locked = True

    p.stop_processing()                                   # A tugadi (D2223/finalize)

    # A ning qolgan crop buferi BEKOR qilindi.
    assert p._event_buf == []
    assert p._confirm == 0
    assert p._plate_locked is False


def test_new_session_starts_with_empty_buffer(raw_pipe):
    p = raw_pipe
    # Oldingi kuzovdan qolgan (stale) croplar buferda.
    p._event_buf = [(_crop(), 0.95)]
    p._confirm = 2
    p._plate_locked = True
    p._processing = False

    p.start_processing()                                  # kuzov B boshlandi

    # B toza buferdan boshlanadi -> A ning stale cropи B ga submit BO'LMAYDI.
    assert p._event_buf == []
    assert p._confirm == 0
    assert p._plate_locked is False


def test_cross_session_frame_cannot_leak(raw_pipe):
    """A tugab (stop) B boshlansa (start), A ning frame'i B ga o'tmaydi (bufer toza)."""
    p = raw_pipe
    # A ishlamoqda, croplar to'plandi.
    p._processing = True
    p._event_buf = [(_crop(), 0.9)]
    p._confirm = 3

    p.stop_processing()                                   # A -> tugadi
    assert p._event_buf == []

    p.start_processing()                                  # B -> boshlandi
    assert p._event_buf == []                             # B da A ning cropи YO'Q
