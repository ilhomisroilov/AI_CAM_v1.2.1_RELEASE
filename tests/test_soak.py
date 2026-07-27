"""
Rapid MVP — Tier 3: SOAK (accelerated, 1000 session).

⚠️ CHEKLOV: test infratuzilmasi vaqt chegarasi sabab 2 SOATLIK real soak
IMKONSIZ. Buning o'rniga ACCELERATED soak: 1000 ketma-ket simulated session
(fake, tarmoqsiz) va resurs O'SISHI cheklanganligini tekshiradi:
  * thread count nazoratsiz O'SMAYDI (session watchdog thread'lari tugaydi);
  * sessiya registri CHEGARALANGAN (_session_history_max) — xotira sizmaydi;
  * DB aynan 1000 ta yozuv bilan o'sadi (birdan-bir, invariant);
  * accepted_D2222 == finalized == 1000.

HAQIQIY uzoq-muddatli soak (RAM/CPU/GPU/open-handles drift, worker restart
barqarorligi soatlar davomida) HIL/staging'da bajarilishi kerak (HIL PENDING).
"""
from __future__ import annotations

import gc
import threading
import time

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until

_TERMINAL = (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
             SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


@pytest.mark.slow
def test_accelerated_soak_1000_sessions_bounded_resources(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 3000)   # uzun, lekin erta yopiladi
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 30.0)
    p = pipeline_instance

    gc.collect()
    threads_before = threading.active_count()
    N = 1000

    for i in range(1, N + 1):
        p.plc_on()
        s = _active(p)
        sid = s.session_id
        p._on_ocr_result(make_ocr_result(sid, vin=f"NSTFA814ATJ{i:06d}"))
        p.on_exit_signal()                       # VIN bor + RFID o'chiq -> erta SUCCESS
        assert wait_until(lambda s=s: s.state in _TERMINAL, timeout=15.0)

    # Watchdog thread'lari tugashiga qisqa vaqt beramiz.
    assert wait_until(lambda: threading.active_count() <= threads_before + 5, timeout=10.0), \
        f"thread count nazoratsiz o'sdi: {threading.active_count()} (boshlang'ich {threads_before})"

    # --- Resurs chegaralari ---
    # Sessiya registri CHEGARALANGAN (xotira sizmaydi) — 1000 session ochilsa ham
    # _sessions <= _session_history_max (200).
    with p._session_lock:
        assert len(p._sessions) <= p._session_history_max + 1

    # --- Invariant 1000 miqyosda ---
    accepted = p._trigger_sequence - p._dropped_trigger_count
    assert accepted == N
    assert p._dropped_trigger_count == 0
    assert db.count_records() == N
    rows = db.get_all_records()
    assert len({r["session_id"] for r in rows}) == N        # hammasi ALOHIDA
    assert all(r["status"] == "SUCCESS" for r in rows)
    assert p._metrics["session_mismatch_total"] == 0
    assert p._metrics["database_write_failure_total"] == 0
