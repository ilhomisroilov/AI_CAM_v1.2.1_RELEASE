"""v1.3.0 — D2222 raw-signal forensic auditor tests (hardware-independent).

Feeds synthetic sample timelines (mono in seconds, 100 ms poll like production)
and asserts debounce / pulse-width / interval / active-cycle classification.
"""
from __future__ import annotations

from backend.plc.edge_audit import EdgeAuditor, EdgeType, EdgeDecision


def _auditor(**kw):
    kw.setdefault("write_files", False)
    return EdgeAuditor(**kw)


def _pulse(t0, high_s, *, low_before=1.0, low_after=1.0, step=0.1):
    """Raw timeline: low, then high for high_s, then low. Returns [(mono, raw)]."""
    s, t = [], round(t0 - low_before, 3)
    while t < t0 - 1e-9:
        s.append((round(t, 3), 0)); t = round(t + step, 3)
    end = t0 + high_s
    while t < end - 1e-9:
        s.append((round(t, 3), 1)); t = round(t + step, 3)
    tail = end + low_after
    while t <= tail + 1e-9:
        s.append((round(t, 3), 0)); t = round(t + step, 3)
    return s


def _run(aud, samples, active_cycle=False):
    out = []
    for mono, raw in samples:
        e = aud.feed(raw, active_cycle=active_cycle, mono=mono, wall=1_700_000_000 + mono)
        if e is not None:
            out.append(e)
    return out


def test_valid_3s_pulse_one_accepted_rising_and_valid_falling():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 3.0))
    rising = [e for e in evs if e.edge_type == EdgeType.RISING]
    falling = [e for e in evs if e.edge_type == EdgeType.FALLING]
    assert len(rising) == 1 and rising[0].accepted and rising[0].decision == EdgeDecision.ACCEPTED
    assert len(falling) == 1
    assert 2900 <= falling[0].pulse_width_ms <= 3100
    assert falling[0].decision == EdgeDecision.FALLING  # width within [2000,4500]


def test_100ms_noise_pulse_is_filtered():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 0.15))  # 150ms high < 200ms debounce
    assert [e for e in evs if e.edge_type == EdgeType.RISING] == []


def test_repeated_high_samples_fire_once():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 3.0))
    assert sum(1 for e in evs if e.edge_type == EdgeType.RISING) == 1


def test_rising_during_active_cycle_is_duplicate_ignored():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 3.0), active_cycle=True)
    rising = [e for e in evs if e.edge_type == EdgeType.RISING][0]
    assert not rising.accepted
    assert rising.decision == EdgeDecision.DUPLICATE_TRIGGER_IGNORED
    assert rising.reason == "ACTIVE_BODY_CYCLE"


def _two_pulses_gap(gap_s):
    aud = _auditor()
    _run(aud, _pulse(10.0, 3.0))                      # first cycle (accepted ~10.2)
    evs2 = _run(aud, _pulse(10.2 + gap_s, 3.0))       # second, gap from accepted rising
    return [e for e in evs2 if e.edge_type == EdgeType.RISING][0]


def test_trigger_after_20s_is_suspicious_early():
    assert _two_pulses_gap(20.0).decision == EdgeDecision.SUSPICIOUS_EARLY_TRIGGER


def test_trigger_after_89s_is_suspicious_early():
    assert _two_pulses_gap(89.0).decision == EdgeDecision.SUSPICIOUS_EARLY_TRIGGER


def test_trigger_after_91s_is_accepted():
    r = _two_pulses_gap(91.0)
    assert r.accepted and r.decision == EdgeDecision.ACCEPTED


def test_invalid_short_pulse_width():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 1.0))  # 1000ms < 2000ms min
    falling = [e for e in evs if e.edge_type == EdgeType.FALLING][0]
    assert falling.decision == EdgeDecision.INVALID_PLC_PULSE


def test_invalid_long_pulse_width():
    aud = _auditor()
    evs = _run(aud, _pulse(10.0, 5.0))  # 5000ms > 4500ms max
    falling = [e for e in evs if e.edge_type == EdgeType.FALLING][0]
    assert falling.decision == EdgeDecision.INVALID_PLC_PULSE


def test_duplicate_identical_sample_does_not_refire():
    aud = _auditor()
    _run(aud, _pulse(10.0, 3.0))
    before = len(aud.events)
    # replay the steady-high value again at a later mono: no new edge
    aud.feed(0, active_cycle=False, mono=20.0, wall=1_700_000_020.0)
    assert len(aud.events) == before  # already low/debounced; no phantom edge


def test_audit_files_written(tmp_path):
    aud = EdgeAuditor(audit_dir=tmp_path, write_files=True)
    _run(aud, _pulse(10.0, 3.0))
    csvs = list(tmp_path.glob("plc_edges_*.csv"))
    jsonls = list(tmp_path.glob("plc_edges_*.jsonl"))
    assert csvs and jsonls
    assert csvs[0].read_text(encoding="utf-8").strip().splitlines()[0].startswith("seq,")
    assert len(jsonls[0].read_text(encoding="utf-8").strip().splitlines()) >= 2  # rising+falling


def test_stats_counts():
    aud = _auditor()
    _run(aud, _pulse(10.0, 3.0))                 # accepted
    _run(aud, _pulse(30.0, 3.0), active_cycle=True)  # duplicate ignored
    st = aud.stats()
    assert st["accepted"] == 1
    assert st["duplicate_ignored"] == 1
