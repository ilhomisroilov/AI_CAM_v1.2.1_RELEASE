"""v1.3.0 — production body-cycle invariants (queue DISABLED / ignore mode).

The shared `pipeline_instance` fixture runs in simulator/queue mode; these tests
opt back into the PRODUCTION semantics (pending_trigger_queue_enabled=False,
minimum_body_interval_sec=90) and assert:

  * a trigger during an active cycle is DUPLICATE_TRIGGER_IGNORED (not queued,
    no second session/record);
  * a rising edge sooner than the minimum body interval is SUSPICIOUS_EARLY_TRIGGER
    (suppressed);
  * a trigger after the interval is accepted (new cycle);
  * suppressed triggers are counted and forensically audited.
"""
from __future__ import annotations

from pathlib import Path

from backend import config as cfg
from conftest import make_ocr_result, wait_until


def _production_mode(monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "pending_trigger_queue_enabled", False)
    monkeypatch.setattr(cfg.SESSION, "minimum_body_interval_sec", 90.0)


def test_duplicate_trigger_during_active_cycle_is_ignored_not_queued(pipeline_instance, monkeypatch):
    p = pipeline_instance
    _production_mode(monkeypatch)
    p.plc_on()
    first = p._active_session_id
    assert first is not None

    p.plc_on()  # duplicate/bounce/external pulse during the active cycle

    assert p._active_session_id == first, "no second session may open during an active cycle"
    assert len(p._pending_triggers) == 0, "must be ignored, NOT queued"
    assert p._suppressed_trigger_count == 1
    assert p._dropped_trigger_count == 0


def test_suspicious_early_trigger_is_suppressed(pipeline_instance, monkeypatch):
    p = pipeline_instance
    _production_mode(monkeypatch)
    p.plc_on()
    sid = p._active_session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))
    assert wait_until(lambda: p._sessions[sid].state.value == "SUCCESS", timeout=3.0)

    before = p._suppressed_trigger_count
    p.plc_on()  # immediately after (gap ~0s << 90s)

    assert p._suppressed_trigger_count == before + 1
    # the early trigger must NOT open a fresh non-terminal cycle
    from backend.pipeline import _TERMINAL_SESSION_STATES
    non_terminal = [s for s in p._sessions.values() if s.state not in _TERMINAL_SESSION_STATES]
    assert non_terminal == []


def test_trigger_after_minimum_interval_is_accepted(pipeline_instance, monkeypatch):
    p = pipeline_instance
    _production_mode(monkeypatch)
    p.plc_on()
    sid = p._active_session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))
    assert wait_until(lambda: p._sessions[sid].state.value == "SUCCESS", timeout=3.0)

    # simulate the >=90s takt gap deterministically
    p._last_accepted_trigger_mono -= 91.0
    p.plc_on()

    assert wait_until(lambda: p._active_session_id not in (None, sid), timeout=3.0)
    assert p._active_session_id != sid
    assert p._suppressed_trigger_count == 0


def test_suppressed_trigger_is_audited(pipeline_instance, monkeypatch):
    p = pipeline_instance
    _production_mode(monkeypatch)
    from backend.config import RUNTIME_DIR
    audit_dir = Path(RUNTIME_DIR) / "audit"
    before = set(audit_dir.glob("suppressed_triggers_*.jsonl")) if audit_dir.exists() else set()

    p.plc_on()
    p.plc_on()  # suppressed -> audited

    files = set(audit_dir.glob("suppressed_triggers_*.jsonl"))
    assert files, "a suppressed-trigger audit file must be written"
    text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "DUPLICATE_TRIGGER_IGNORED" in text
