"""
Rapid MVP — Tier 7: RFID chaos.

RFID ownership/busy/grace avvalgi testlarda qoplangan
(test_plc_rfid_isolation.py, test_session_ownership.py, test_session_grace.py).
Bu fayl QOLGAN data-integrity holatlarini qoplaydi:
  * late RFID (finalize'dan keyin) -> rad + metrika;
  * noma'lum/boshqa sessiya RFID -> rad;
  * bir nechta EPC bir oynada -> ENG KUCHLI RSSI tanlanadi;
  * eski EPC (oynadan tashqari) -> tanlanmaydi (qayta ko'rinsa ham);
  * duplicate EPC upsert -> oxirgisi, ingest hisoblanadi;
  * invalid/bo'sh EPC -> decimal validate rad, extract xavfsiz.

⚠️ REST timeout / SSE disconnect/reconnect — tarmoq qatlamida (r700_stream.py),
HIL PENDING (real reader kerak). Bu yerda faqat data-integrity mantig'i.

RFID natijasi FAQAT o'z session_id'iga yoziladi.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend import config as cfg
from backend.pipeline import SessionState
from backend.rfid.tag_cache import TagCache, TagHit
from backend.rfid import epc_extract
from conftest import wait_until


def _rfid_result(session_id, number="1234", epc=None, antenna=1, rssi=-42.0):
    from backend.rfid.rfid_service import RFIDResult
    now = datetime.now()
    return RFIDResult(session_id=session_id, epc=epc or ("0000" + number), number=number,
                      antenna=antenna, rssi=rssi, first_seen_at=now, received_at=now)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


# ---------------- pipeline-level: late / wrong-session ----------------
def test_late_rfid_after_finalize_rejected(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 0.3)
    p = pipeline_instance
    p.rfid_service = None
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id
    assert wait_until(lambda: sess.state in
                      (SessionState.TIMEOUT, SessionState.PARTIAL_SUCCESS), timeout=5.0)
    late_before = p._metrics["late_rfid_results_total"]
    # Finalize'dan keyingi RFID -> rad, sessiyaga yozilmaydi.
    p._on_rfid_result(_rfid_result(sid, number="5555"))
    assert p._metrics["late_rfid_results_total"] == late_before + 1


def test_rfid_for_unknown_session_rejected(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    before = p._metrics["late_rfid_results_total"]
    p._on_rfid_result(_rfid_result("no-such-session", number="7777"))
    assert p._metrics["late_rfid_results_total"] == before + 1


# ---------------- tag_cache: multiple / old / duplicate EPC ----------------
def test_multiple_epc_strongest_rssi_selected():
    tc = TagCache()
    now = datetime.utcnow()
    tc.upsert(TagHit(epc="1111", antenna=1, rssi=-60.0, timestamp=now))
    tc.upsert(TagHit(epc="2222", antenna=2, rssi=-40.0, timestamp=now))  # eng kuchli
    tc.upsert(TagHit(epc="3333", antenna=3, rssi=-55.0, timestamp=now))
    best = tc.get_best_in_window(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert best.epc == "2222"


def test_old_epc_outside_window_not_selected():
    tc = TagCache()
    now = datetime.utcnow()
    old = now - timedelta(seconds=30)
    tc.upsert(TagHit(epc="OLD", antenna=1, rssi=-30.0, timestamp=old))    # kuchli lekin ESKI
    tc.upsert(TagHit(epc="NEW", antenna=1, rssi=-70.0, timestamp=now))
    best = tc.get_best_in_window(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert best.epc == "NEW"      # eski (oynadan tashqari) tanlanmaydi, qayta ko'rinsa ham


def test_duplicate_epc_upsert_latest_wins():
    tc = TagCache()
    now = datetime.utcnow()
    tc.upsert(TagHit(epc="DUP", antenna=1, rssi=-50.0, timestamp=now))
    tc.upsert(TagHit(epc="DUP", antenna=2, rssi=-35.0, timestamp=now))    # bir xil EPC, yangi
    assert tc.ingest_count == 2
    best = tc.get_best_in_window(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert best.epc == "DUP" and best.rssi == -35.0


# ---------------- epc_extract: invalid / bo'sh ----------------
def test_invalid_epc_decimal_validation():
    assert epc_extract.is_factory_epc_decimal("ABCD", 1000, 9999) is False
    assert epc_extract.is_factory_epc_decimal("", 1000, 9999) is False
    assert epc_extract.is_factory_epc_decimal(None, 1000, 9999) is False
    assert epc_extract.is_factory_epc_decimal("999", 1000, 9999) is False    # chegaradan past
    assert epc_extract.is_factory_epc_decimal("1234", 1000, 9999) is True


def test_extract_rfid_number_safe_on_empty():
    # Bo'sh/None -> xato bermaydi (xavfsiz normalizatsiya).
    assert epc_extract.extract_rfid_number(None) is not None
    assert epc_extract.extract_rfid_number("") is not None
