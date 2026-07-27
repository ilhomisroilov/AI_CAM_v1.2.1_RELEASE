"""
============================================================
test_ocr_contract.py — OCR contract (session_id/frame_id) + session-scoped dedup
============================================================
Audit topilmasi (P1 — Dedup): `ai/ocr_worker.py` da `_is_duplicate` global
`_last_vin` + vaqt oynasi (duplicate_window_sec) ishlatar edi — sessiyaga
BOG'LIQ EMAS edi. Oqibat: qabul qilingan VIN `return` bilan tashlanardi va
`on_result` chaqirilmasdi -> yangi sessiya VIN'siz TIMEOUT bo'lardi.

Bu fayl OCRWorker'ni TO'G'RIDAN-TO'G'RI (Pipeline'siz) sinaydi — HAQIQIY
PaddleOCR ENGINE ISHLATILMAYDI (og'ir + tarmoqqa bog'liq bo'lishi mumkin);
`_engine` FAKE ob'ekt bilan almashtiriladi (monkeypatch), shunda
`_process_job` to'liq deterministik, tarmoqsiz ishlaydi.
"""
from __future__ import annotations

import numpy as np

from backend.ai.ocr_worker import OCRJob, OCRResult, OCRWorker


class _FakeEngine:
    """PaddleOCR o'rniga — bitta oldindan belgilangan matnni "o'qiydi" (tarmoqsiz, deterministik)."""

    def __init__(self, text: str, conf: float = 0.95):
        self.text = text
        self.conf = conf

    def read(self, img):
        box = [[0, 0], [10, 0], [10, 10], [0, 10]]
        return [(box, self.text, self.conf)]


def _make_worker(text="NSTFA814ATJ123456", conf=0.95) -> OCRWorker:
    results = []
    w = OCRWorker(on_result=lambda r: results.append(r))
    w._engine = _FakeEngine(text, conf)     # tarmoqsiz — PaddleOCR yuklanmaydi
    # Bu fayl acceptance redundancy'ni emas, DTO/dedup contractini sinaydi.
    # Deterministik bitta fake readni shu test doirasida legacy opt-in bilan
    # o'tkazamiz; production config accept_single_read=False bo'lib qoladi.
    production_params = w._fuse_params
    w._fuse_params = lambda: {
        **production_params(), "accept_min_crops": 1, "accept_single_read": True
    }
    w._results = results                    # test uchun qulaylik (rasmiy API emas)
    return w


def _dummy_crop() -> np.ndarray:
    return np.zeros((20, 60, 3), dtype=np.uint8)


def test_ocr_job_result_carries_session_id_and_frame_id():
    """OCR contract: chiqish (OCRResult) kirish (session_id, frame_id) bilan bog'lanadi."""
    w = _make_worker()
    job = OCRJob(frames=[_dummy_crop()], session_id="sess-42", frame_id=7,
                capture_timestamp=123.0, model_hint="QY")
    w._process_job(job)

    assert len(w._results) == 1
    result: OCRResult = w._results[0]
    assert result.session_id == "sess-42"
    assert result.frame_id == 7
    assert result.vin == "NSTFA814ATJ123456"
    assert result.attempt_count >= 1
    assert result.completed_at > 0
    assert result.model in ("QY", "BL7M", None)


def test_submit_frames_auto_assigns_frame_id_and_timestamp_when_omitted():
    """submit_frames() frame_id/capture_timestamp berilmasa avtomatik to'ldiradi (OCR contract INPUT)."""
    w = _make_worker()
    w._enabled = True
    w.submit_frames([_dummy_crop()], session_id="sess-1")
    job: OCRJob = w._queue.get_nowait()
    assert job.session_id == "sess-1"
    assert job.frame_id is not None
    assert job.capture_timestamp is not None


def test_dedup_rejects_duplicate_vin_within_same_session():
    """10) Bir sessiya ICHIDA takroriy VIN rad etiladi (accepted natija yo'q bo'lib qoladi)."""
    w = _make_worker()
    assert w._is_duplicate("session-A", "NSTFA814ATJ123456") is False   # birinchi -> qabul
    assert w._is_duplicate("session-A", "NSTFA814ATJ123456") is True    # ikkinchi (bir xil sessiya) -> DUPLICATE


def test_dedup_allows_same_vin_across_two_different_sessions():
    """9) Bir xil VIN ikki BOSHQA sessiyada qonuniy qabul qilinadi (dedup sessiyaga bog'liq)."""
    w = _make_worker()
    assert w._is_duplicate("session-A", "NSTFA814ATJ123456") is False
    assert w._is_duplicate("session-B", "NSTFA814ATJ123456") is False   # BOSHQA sessiya -> qonuniy


def test_dedup_legacy_global_window_when_session_id_none():
    """session_id=None (qo'lda/sessiyasiz rejim) -> eski global vaqt-oynali dedup (orqaga moslik)."""
    w = _make_worker()
    assert w._is_duplicate(None, "NSTFA814ATJ123456") is False
    assert w._is_duplicate(None, "NSTFA814ATJ123456") is True    # global oyna ichida -> duplicate


def test_process_job_end_to_end_respects_session_dedup():
    """_process_job orqali: bir sessiyada ikkinchi marta xuddi shu VIN kelsa on_result chaqirilmaydi."""
    w = _make_worker()
    job1 = OCRJob(frames=[_dummy_crop()], session_id="sess-dup")
    job2 = OCRJob(frames=[_dummy_crop()], session_id="sess-dup")
    w._process_job(job1)
    w._process_job(job2)
    assert len(w._results) == 1, "ikkinchi (duplicate) natija on_result ga yuborilmasligi kerak"
