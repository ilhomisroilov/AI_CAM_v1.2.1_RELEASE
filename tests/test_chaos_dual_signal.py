"""
Rapid MVP — Tier 2 (burst) + Tier 5 (PLC chaos) — YANGI dual-signal mashinasi.

Eski PLC/kamera/RFID chaos (disconnect/reconnect, busy, DB idempotency) avvalgi
remediation testlarida qoplangan. Bu fayl AYNAN D2222/D2223 grace mashinasining
chaos-chidamliligini tekshiradi:
  * D2223 faol sessiyasiz kelsa (bo'sh liniya / noto'g'ri tartib) -> xavfsiz no-op;
  * ko'p sessiya bir vaqtda GRACE_WAIT'da (overlap burst) -> aralashmaydi;
  * targeted D2223 faqat O'Z sessiyasini yopadi;
  * rapid D2222 burst -> navbat, hech biri yo'qolmaydi, invariant saqlanadi.

Real qurilma ULANMAYDI; tmp_path DB; deterministik.
"""
from __future__ import annotations

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until

_TERMINAL = (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
             SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)


def _exit(monkeypatch, grace_ms=3000, max_dur=30.0):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", grace_ms)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", max_dur)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


# ------------------------------------------------------------------
# Tier 5: D2223 faol sessiyasiz (D2223 D2222'dan oldin / bo'sh liniya)
# ------------------------------------------------------------------
def test_d2223_without_active_session_is_safe(pipeline_instance, monkeypatch):
    _exit(monkeypatch)
    p = pipeline_instance
    # Hech qanday sessiya ochilmagan holda D2223 -> xavfsiz no-op (crash yo'q, record yo'q).
    p.on_exit_signal()
    assert db.count_records() == 0
    assert p._active_session_id is None
    # Keyin normal D2222 -> yangi sessiya muammosiz ochiladi.
    p.plc_on()
    assert _active(p) is not None


# ------------------------------------------------------------------
# Tier 2: burst overlap — bir vaqtda bir nechta sessiya GRACE_WAIT'da
# ------------------------------------------------------------------
def test_burst_overlap_multiple_grace_sessions(pipeline_instance, monkeypatch):
    _exit(monkeypatch, grace_ms=3000)     # uzun grace -> bir vaqtda grace'da tursin
    p = pipeline_instance
    sessions = []
    # 5 kuzov: har birini ochamiz va D2223 bilan grace'ga o'tkazamiz (natijasiz).
    for i in range(5):
        p.plc_on()
        s = _active(p)
        sessions.append(s)
        p.on_exit_signal()                 # -> GRACE_WAIT, capture-owner bo'shaydi
        assert s.state == SessionState.GRACE_WAIT

    # Hammasi bir vaqtda grace'da (overlap).
    assert p.status()["session"]["finalizing_session_count"] == 5

    # Endi har biriga O'Z natijasini beramiz (grace ichida) -> har biri o'z VIN'i bilan yopiladi.
    for i, s in enumerate(sessions):
        p._on_ocr_result(make_ocr_result(s.session_id, vin=f"NSTFA814ATJ90000{i}"))

    for s in sessions:
        assert wait_until(lambda s=s: s.state == SessionState.SUCCESS)
    rows = db.get_all_records()
    assert len(rows) == 5
    # Har VIN o'z sessiyasiga (aralashmagan).
    by_sid = {r["session_id"]: r["detected_vin"] for r in rows}
    for i, s in enumerate(sessions):
        assert by_sid[s.session_id] == f"NSTFA814ATJ90000{i}"


# ------------------------------------------------------------------
# Tier 5: targeted D2223 faqat O'Z sessiyasini yopadi
# ------------------------------------------------------------------
def test_targeted_d2223_closes_only_its_session(pipeline_instance, monkeypatch):
    _exit(monkeypatch, grace_ms=3000)
    p = pipeline_instance
    p.plc_on()
    a = _active(p)
    p.on_exit_signal(a.session_id)         # A -> grace
    p.plc_on()
    b = _active(p)
    assert b.state == SessionState.ACTIVE and a.state == SessionState.GRACE_WAIT

    # A ni yopamiz (grace ichida natija) — B ta'sirlanmasligi kerak.
    p._on_ocr_result(make_ocr_result(a.session_id, vin="NSTFA814ATJ900010"))
    assert wait_until(lambda: a.state == SessionState.SUCCESS)
    assert b.state == SessionState.ACTIVE   # B hali faol (yopilmadi)


# ------------------------------------------------------------------
# Tier 2/5: rapid D2222 burst faol sessiya paytida -> navbat, yo'qolmaydi
# ------------------------------------------------------------------
def test_rapid_d2222_burst_queues_all(pipeline_instance, monkeypatch):
    _exit(monkeypatch, grace_ms=40, max_dur=0.3)
    monkeypatch.setattr(cfg.SESSION, "max_pending_triggers", 20)
    p = pipeline_instance
    # Birinchi sessiya faol; keyin tez 6 ta D2222 (navbatga).
    p.plc_on()
    for _ in range(6):
        p.plc_on()
    # Barcha 7 trigger qabul qilingan (silent drop yo'q).
    assert p._trigger_sequence - p._dropped_trigger_count == 7
    assert p._dropped_trigger_count == 0
    # Barcha sessiyalar oxir-oqibat yopiladi (navbat avtomatik ishlaydi).
    # Timeout SAXOVATLI (20s) — to'liq suite yukida timer-finalize sekinlashishi
    # mumkin (flakiness oldini olish; invariant mantig'i to'g'ri).
    assert wait_until(lambda: db.count_records() == 7, timeout=20.0)
    rows = db.get_all_records()
    assert len({r["session_id"] for r in rows}) == 7   # hammasi ALOHIDA
