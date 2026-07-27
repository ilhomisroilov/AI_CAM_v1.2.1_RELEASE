"""
Rapid MVP — Tier 4: OCR PROCESS-WORKER hang/crash/restart testlari.

`OCRProcessSupervisor` osilgan/crash bo'lgan OCR processini hard-timeout bilan
terminate qilib respawn qilishini va supervisor'ning (ya'ni asosiy ilovaning)
TIRIK qolishini tekshiradi. Fake worker (real PaddleOCR YUKLANMAYDI) alohida
processda ishga tushadi.

Acceptance (spec Tier 4):
  * FastAPI/supervisor tirik qoladi;
  * osilgan sessiya belgilangan (hard) vaqtda yopiladi (natijasiz);
  * OCR worker qayta ishga tushadi;
  * keyingi sessiya muvaffaqiyatli ishlaydi.

HECH QANDAY real qurilma ULANMAYDI. Deterministik, kichik timeoutlar (tez).
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from backend.ai.ocr_process import OCRProcessSupervisor
from backend.ai.ocr_worker import OCRJob
from conftest import wait_until


def _job(session_id, frame_id=1):
    crop = np.zeros((32, 96, 3), dtype=np.uint8)
    return OCRJob(frames=[crop], session_id=session_id, frame_id=frame_id,
                  capture_timestamp=time.time(), model_hint="QY")


class _Collector:
    def __init__(self):
        self.results = []
        self._lock = threading.Lock()

    def __call__(self, result):
        with self._lock:
            self.results.append(result)

    def vins(self):
        with self._lock:
            return [r.vin for r in self.results]

    def sessions(self):
        with self._lock:
            return [r.session_id for r in self.results]


@pytest.fixture
def make_sup():
    """Supervisor quruvchi — teardown'da har doim to'xtatiladi (process leak yo'q)."""
    created = []

    def _factory(behavior="normal", soft_ms=200, hard_ms=600, **params):
        params["behavior"] = behavior
        col = _Collector()
        sup = OCRProcessSupervisor(on_result=col, worker_kind="fake",
                                   worker_params=params,
                                   soft_timeout_ms=soft_ms, hard_timeout_ms=hard_ms)
        sup.start()
        created.append(sup)
        return sup, col

    yield _factory
    for sup in created:
        try:
            sup.stop()
        except Exception:
            pass


# ------------------------------------------------------------------
# 1) Normal — natija yetkaziladi
# ------------------------------------------------------------------
def test_normal_job_delivers_result(make_sup):
    sup, col = make_sup(behavior="normal", vin="NSTFA814ATJ100001")
    sup.submit_job(_job("sess-1"))
    assert wait_until(lambda: len(col.results) == 1, timeout=8.0)
    assert col.vins() == ["NSTFA814ATJ100001"]
    assert col.sessions() == ["sess-1"]
    assert sup.metrics["ocr_hard_timeout_total"] == 0
    assert sup.metrics["ocr_worker_crash_total"] == 0


# ------------------------------------------------------------------
# 2) Slow — OCR_SLOW, lekin natija baribir yetkaziladi
# ------------------------------------------------------------------
def test_slow_job_logs_soft_timeout_but_delivers(make_sup):
    # slow 350ms > soft 200ms, lekin < hard 900ms -> soft belgilanadi, natija keladi.
    sup, col = make_sup(behavior="slow", slow_ms=350, soft_ms=200, hard_ms=900,
                        vin="NSTFA814ATJ100002")
    sup.submit_job(_job("sess-slow"))
    assert wait_until(lambda: len(col.results) == 1, timeout=8.0)
    assert sup.metrics["ocr_soft_timeout_total"] >= 1
    assert sup.metrics["ocr_hard_timeout_total"] == 0


# ------------------------------------------------------------------
# 3) Hang — hard timeout -> terminate + respawn (OCR_HUNG, natijasiz)
# ------------------------------------------------------------------
def test_hang_triggers_hard_timeout_and_respawn(make_sup):
    sup, col = make_sup(behavior="hang", soft_ms=150, hard_ms=500)
    sup.submit_job(_job("sess-hang"))
    # Hard timeout -> hung deb belgilanadi, respawn.
    assert wait_until(lambda: sup.metrics["ocr_hard_timeout_total"] >= 1, timeout=8.0)
    assert wait_until(lambda: sup.metrics["ocr_worker_restart_total"] >= 1, timeout=3.0)
    # Osilgan job natija BERMAYDI (session watchdog yopadi).
    assert col.results == []
    # Supervisor TIRIK (worker respawn bo'ldi).
    assert wait_until(lambda: sup.get_stats()["worker_alive"] is True, timeout=5.0)


# ------------------------------------------------------------------
# 4) Crash — process o'ladi -> crash metric + respawn
# ------------------------------------------------------------------
def test_crash_detected_and_respawned(make_sup):
    sup, col = make_sup(behavior="crash", soft_ms=200, hard_ms=1500)
    sup.submit_job(_job("sess-crash"))
    assert wait_until(lambda: sup.metrics["ocr_worker_crash_total"] >= 1, timeout=8.0)
    assert wait_until(lambda: sup.metrics["ocr_worker_restart_total"] >= 1, timeout=3.0)
    assert col.results == []
    assert wait_until(lambda: sup.get_stats()["worker_alive"] is True, timeout=5.0)


# ------------------------------------------------------------------
# 5) Exception — terminal error telemetry, natija yo'q, crash YO'Q, tirik
# ------------------------------------------------------------------
def test_worker_exception_no_result_no_crash(make_sup):
    sup, col = make_sup(behavior="exception", soft_ms=200, hard_ms=1500)
    sup.submit_job(_job("sess-exc"))
    assert wait_until(lambda: sup.metrics["ocr_worker_error_total"] == 1, timeout=3.0)
    assert col.results == []
    assert sup.metrics["ocr_worker_crash_total"] == 0
    assert sup.metrics["ocr_hard_timeout_total"] == 0
    assert sup.get_stats()["worker_alive"] is True
    assert "simulated worker exception" in sup.get_stats()["last_error"]


def test_legacy_real_supervisor_fails_fast_with_actionable_error():
    """Eski nested-real yo'l 120s jim poll qilmaydi; production poolga yo'naltiradi."""
    col = _Collector()
    sup = OCRProcessSupervisor(
        on_result=col, worker_kind="real",
        soft_timeout_ms=200, hard_timeout_ms=1500,
    )
    sup.start()
    try:
        sup.submit_job(_job("legacy-real"))
        assert wait_until(lambda: sup.metrics["ocr_worker_error_total"] == 1, timeout=3.0)
        assert col.results == []
        assert sup.metrics["ocr_hard_timeout_total"] == 0
        assert sup.metrics["ocr_worker_restart_total"] == 0
        assert "OCRProcessPool" in sup.get_stats()["last_error"]
    finally:
        sup.stop()


# ------------------------------------------------------------------
# 6) RECOVERY — osilgan kuzovdan keyin keyingi kuzov MUVAFFAQIYATLI ishlaydi
# ------------------------------------------------------------------
def test_next_session_works_after_hang_restart(make_sup):
    # Faqat "sess-bad" osiladi; respawn'dan keyin "sess-good" normal ishlaydi.
    sup, col = make_sup(behavior="normal", soft_ms=150, hard_ms=500,
                        hang_session="sess-bad", vin="NSTFA814ATJ100006")
    sup.submit_job(_job("sess-bad"))
    assert wait_until(lambda: sup.metrics["ocr_hard_timeout_total"] >= 1, timeout=8.0)
    assert wait_until(lambda: sup.get_stats()["worker_alive"] is True, timeout=5.0)

    # Keyingi (yaxshi) kuzov — respawn qilingan worker uni MUVAFFAQIYATLI ishlaydi.
    sup.submit_job(_job("sess-good"))
    assert wait_until(lambda: len(col.results) == 1, timeout=8.0)
    assert col.sessions() == ["sess-good"]
    assert col.vins() == ["NSTFA814ATJ100006"]


# ------------------------------------------------------------------
# 7) Ketma-ket ko'p job — supervisor barqaror, hammasi yetkaziladi
# ------------------------------------------------------------------
def test_multiple_jobs_all_delivered(make_sup):
    sup, col = make_sup(behavior="normal", vin="NSTFA814ATJ100007")
    for i in range(5):
        sup.submit_job(_job(f"sess-{i}", frame_id=i))
    assert wait_until(lambda: len(col.results) == 5, timeout=10.0)
    assert sorted(col.sessions()) == [f"sess-{i}" for i in range(5)]
    assert sup.metrics["ocr_hard_timeout_total"] == 0
