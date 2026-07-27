"""
Rapid MVP — Tier 2: BURST stress (100 / 250 / 500 ketma-ket session).

Har miqyosda: overlap, pending queue, throughput ostida §16 invariant
(`accepted_D2222 == finalized_DB_records`, duplicate yo'q, mismatch yo'q)
saqlanishini tekshiradi va P50/P95/P99 session latency'ni hisoblaydi.

⚠️ LATENCY HIL PENDING: bu yerda FAKE natijalar (real PaddleOCR/kamera/RFID
YO'Q) — o'lchangan latency test-harness overhead'i, HAQIQIY OCR/RFID latency
EMAS. Haqiqiy P50/P95/P99 faqat real inference/qurilma bilan o'lchanadi.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until

_TERMINAL = (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
             SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)


def _rfid(session_id, number="1234"):
    from backend.rfid.rfid_service import RFIDResult
    now = datetime.now()
    return RFIDResult(session_id=session_id, epc="0000" + number, number=number,
                      antenna=1, rssi=-40.0, first_seen_at=now, received_at=now)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


def _percentiles(values, ps=(50, 95, 99)):
    """Oddiy percentil (nearest-rank) — kichik/tashqi kutubxonasiz."""
    if not values:
        return {p: 0.0 for p in ps}
    s = sorted(values)
    out = {}
    for p in ps:
        k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
        out[p] = s[k]
    return out


def _run_burst(p, n):
    """n ta full-success session (VIN+RFID -> D2223 -> erta SUCCESS). Latency qaytaradi."""
    latencies = []
    for i in range(1, n + 1):
        t0 = time.monotonic()
        p.plc_on()
        s = _active(p)
        sid = s.session_id
        p._on_ocr_result(make_ocr_result(sid, vin=f"NSTFA814ATJ{i:06d}"))
        p._on_rfid_result(_rfid(sid, number=f"{1000 + (i % 9000)}"))
        p.on_exit_signal()                       # ikkalasi tayyor -> erta SUCCESS
        # Timeout SAXOVATLI (15s) — to'liq suite yukida flakiness oldini olish.
        assert wait_until(lambda s=s: s.state in _TERMINAL, timeout=15.0)
        latencies.append((time.monotonic() - t0) * 1000.0)   # ms
    return latencies


@pytest.mark.parametrize("n", [100, 250, 500])
def test_burst_invariants_and_latency(pipeline_instance, monkeypatch, n):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 3000)   # uzun (lekin erta yopiladi)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 30.0)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None

    lat = _run_burst(p, n)

    # --- §16 invariant har miqyosda ---
    accepted = p._trigger_sequence - p._dropped_trigger_count
    assert accepted == n
    assert p._dropped_trigger_count == 0
    rows = db.get_all_records()
    assert len(rows) == n, f"accepted_D2222={n} != finalized={len(rows)}"
    sids = [r["session_id"] for r in rows]
    assert len(set(sids)) == len(sids)                      # duplicate yo'q
    assert p._metrics["session_mismatch_total"] == 0
    assert all(r["status"] == "SUCCESS" for r in rows)      # hammasi full-success

    # --- Latency percentillari (HARNESS overhead, HIL PENDING) ---
    pct = _percentiles(lat)
    # Sanity: harness'da har session yopildi va latency musbat/cheklangan.
    assert pct[50] >= 0 and pct[99] < 5000
    print(f"\n[BURST n={n}] harness session latency ms: "
          f"P50={pct[50]:.1f} P95={pct[95]:.1f} P99={pct[99]:.1f} "
          f"(⚠️ HIL PENDING — fake, real OCR/RFID emas)")
