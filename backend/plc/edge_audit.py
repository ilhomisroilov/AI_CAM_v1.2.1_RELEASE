"""
============================================================
edge_audit.py — D2222 raw-signal forensic auditor (v1.3.0)
============================================================
Instruments every PLC sample into a *debounced* rising/falling edge timeline so
that extra pulses during an active body-cycle can be PROVEN (external / bounce /
duplicate / early re-trigger) from the raw timeline instead of guessed.

Design goals:
  * Pure logic — no PLC / network / hardware dependency, so it is fully unit
    testable with synthetic sample sequences and injected clocks.
  * Additive — it OBSERVES; it never blocks camera/RFID and never changes the
    live trigger behaviour by itself. The session layer decides what to do with
    the classification (accept / ignore / suppress).
  * One audit row per debounced EDGE (rising/falling) + a short ring-buffer of
    the raw samples around it — the 100 ms samples are NOT flooded into INFO log.

Writes (append, atomic-ish line writes):
    runtime/audit/plc_edges_YYYYMMDD.csv
    runtime/audit/plc_edges_YYYYMMDD.jsonl
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, List, Optional
from collections import deque


class EdgeType:
    RISING = "rising"
    FALLING = "falling"


class EdgeDecision:
    ACCEPTED = "ACCEPTED"                               # a genuine new body-cycle
    DUPLICATE_TRIGGER_IGNORED = "DUPLICATE_TRIGGER_IGNORED"   # arrived during active cycle
    SUSPICIOUS_EARLY_TRIGGER = "SUSPICIOUS_EARLY_TRIGGER"     # < minimum_body_interval_sec
    STARTUP_HIGH = "STARTUP_HIGH"                      # signal already high at startup
    FALLING = "FALLING"                                # falling edge (pulse end)
    INVALID_PLC_PULSE = "INVALID_PLC_PULSE"            # pulse width out of expected band


@dataclass
class EdgeEvent:
    seq: int
    wall_clock: str
    monotonic: float
    raw_value: int
    debounced_value: int
    previous_value: int
    edge_type: str
    stable_low_ms: float
    stable_high_ms: float
    pulse_width_ms: Optional[float]
    time_since_prev_rising_ms: Optional[float]
    body_cycle_id: Optional[str]
    session_id: Optional[str]
    state: Optional[str]
    accepted: bool
    decision: str
    reason: str
    thread: str
    read_sequence: int
    ring_context: List[dict] = field(default_factory=list)

    def to_row(self) -> dict:
        d = asdict(self)
        d["ring_context"] = json.dumps(self.ring_context, separators=(",", ":"))
        return d


CSV_FIELDS = [
    "seq", "wall_clock", "monotonic", "raw_value", "debounced_value",
    "previous_value", "edge_type", "stable_low_ms", "stable_high_ms",
    "pulse_width_ms", "time_since_prev_rising_ms", "body_cycle_id", "session_id",
    "state", "accepted", "decision", "reason", "thread", "read_sequence",
    "ring_context",
]


class EdgeAuditor:
    """Debounced edge state machine + forensic writer.

    Feed one raw sample per PLC poll via :meth:`feed`. A debounced rising or
    falling transition returns an :class:`EdgeEvent` (also written to the audit
    files); all other samples return ``None``.
    """

    def __init__(
        self,
        *,
        debounce_on_ms: float = 200.0,
        debounce_off_ms: float = 200.0,
        expected_pulse_min_ms: float = 2000.0,
        expected_pulse_max_ms: float = 4500.0,
        minimum_body_interval_sec: float = 90.0,
        ring_seconds: float = 7.0,
        audit_dir: Optional[Path] = None,
        clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], float]] = None,
        writer: Optional[Callable[[EdgeEvent], None]] = None,
        write_files: bool = True,
    ) -> None:
        self.on_ms = float(debounce_on_ms)
        self.off_ms = float(debounce_off_ms)
        self.pulse_min_ms = float(expected_pulse_min_ms)
        self.pulse_max_ms = float(expected_pulse_max_ms)
        self.min_interval_sec = float(minimum_body_interval_sec)
        self.ring_seconds = float(ring_seconds)
        self._clock = clock or time.monotonic
        self._wall = wall_clock or time.time
        self._external_writer = writer
        self._write_files = write_files and (audit_dir is not None)
        self._audit_dir = Path(audit_dir) if audit_dir is not None else None

        self._raw = 0
        self._debounced = 0
        self._raw_since = self._clock()
        self._debounced_since = self._raw_since
        self._rise_mono: Optional[float] = None          # debounced-high start
        self._last_rising_mono: Optional[float] = None    # any rising
        self._last_accept_rising_mono: Optional[float] = None
        self._seq = 0
        self._read_seq = 0
        self._ring: Deque[tuple] = deque()
        self._lock = threading.Lock()
        self.events: List[EdgeEvent] = []                 # in-memory (tests/metrics)

    # -- public ---------------------------------------------------------------
    def feed(
        self,
        raw: int,
        *,
        active_cycle: bool = False,
        mono: Optional[float] = None,
        wall: Optional[float] = None,
        body_cycle_id: Optional[str] = None,
        session_id: Optional[str] = None,
        state: Optional[str] = None,
        startup: bool = False,
    ) -> Optional[EdgeEvent]:
        with self._lock:
            mono = self._clock() if mono is None else float(mono)
            wall = self._wall() if wall is None else float(wall)
            raw = 1 if int(raw) else 0
            self._read_seq += 1
            self._ring.append((mono, wall, raw))
            self._trim_ring(mono)

            if raw != self._raw:
                self._raw = raw
                self._raw_since = mono

            if raw == self._debounced:
                return None
            stable_ms = (mono - self._raw_since) * 1000.0
            need = self.on_ms if raw == 1 else self.off_ms
            if stable_ms < need:
                return None

            prev = self._debounced
            self._debounced = raw
            low_ms = high_ms = 0.0
            if raw == 1:  # rising
                low_ms = (mono - self._debounced_since) * 1000.0
                self._debounced_since = mono
                ev = self._make_rising(mono, wall, prev, low_ms, active_cycle,
                                       body_cycle_id, session_id, state, startup)
            else:         # falling
                high_ms = (mono - self._debounced_since) * 1000.0
                self._debounced_since = mono
                ev = self._make_falling(mono, wall, prev, high_ms,
                                        body_cycle_id, session_id, state)
            self._emit(ev)
            return ev

    def stats(self) -> dict:
        rising = [e for e in self.events if e.edge_type == EdgeType.RISING]
        return {
            "total_edges": len(self.events),
            "rising_edges": len(rising),
            "accepted": sum(1 for e in rising if e.accepted),
            "duplicate_ignored": sum(1 for e in rising if e.decision == EdgeDecision.DUPLICATE_TRIGGER_IGNORED),
            "suspicious_early": sum(1 for e in rising if e.decision == EdgeDecision.SUSPICIOUS_EARLY_TRIGGER),
            "invalid_pulses": sum(1 for e in self.events if e.decision == EdgeDecision.INVALID_PLC_PULSE),
        }

    # -- internals ------------------------------------------------------------
    def _make_rising(self, mono, wall, prev, low_ms, active_cycle,
                     body_cycle_id, session_id, state, startup) -> EdgeEvent:
        since_prev = (None if self._last_rising_mono is None
                      else (mono - self._last_rising_mono) * 1000.0)
        self._last_rising_mono = mono
        self._rise_mono = mono

        if startup:
            decision, accepted, reason = EdgeDecision.STARTUP_HIGH, False, "signal already ON at startup"
        elif active_cycle:
            decision, accepted, reason = (EdgeDecision.DUPLICATE_TRIGGER_IGNORED, False,
                                          "ACTIVE_BODY_CYCLE")
        elif (self._last_accept_rising_mono is not None
              and (mono - self._last_accept_rising_mono) < self.min_interval_sec):
            gap = mono - self._last_accept_rising_mono
            decision, accepted, reason = (EdgeDecision.SUSPICIOUS_EARLY_TRIGGER, False,
                                          f"interval {gap:.1f}s < minimum_body_interval {self.min_interval_sec:.0f}s")
        else:
            decision, accepted, reason = EdgeDecision.ACCEPTED, True, ""
            self._last_accept_rising_mono = mono

        return EdgeEvent(
            seq=self._next_seq(), wall_clock=self._iso(wall), monotonic=round(mono, 6),
            raw_value=1, debounced_value=1, previous_value=prev,
            edge_type=EdgeType.RISING, stable_low_ms=round(low_ms, 1), stable_high_ms=0.0,
            pulse_width_ms=None,
            time_since_prev_rising_ms=(None if since_prev is None else round(since_prev, 1)),
            body_cycle_id=body_cycle_id, session_id=session_id, state=state,
            accepted=accepted, decision=decision, reason=reason,
            thread=threading.current_thread().name, read_sequence=self._read_seq,
            ring_context=self._ring_snapshot(),
        )

    def _make_falling(self, mono, wall, prev, high_ms,
                      body_cycle_id, session_id, state) -> EdgeEvent:
        width = (None if self._rise_mono is None else (mono - self._rise_mono) * 1000.0)
        valid = width is not None and self.pulse_min_ms <= width <= self.pulse_max_ms
        if width is not None and not valid:
            decision, reason = EdgeDecision.INVALID_PLC_PULSE, (
                f"pulse {width:.0f}ms outside [{self.pulse_min_ms:.0f},{self.pulse_max_ms:.0f}]ms")
        else:
            decision, reason = EdgeDecision.FALLING, ""
        return EdgeEvent(
            seq=self._next_seq(), wall_clock=self._iso(wall), monotonic=round(mono, 6),
            raw_value=0, debounced_value=0, previous_value=prev,
            edge_type=EdgeType.FALLING, stable_low_ms=0.0, stable_high_ms=round(high_ms, 1),
            pulse_width_ms=(None if width is None else round(width, 1)),
            time_since_prev_rising_ms=None,
            body_cycle_id=body_cycle_id, session_id=session_id, state=state,
            accepted=False, decision=decision, reason=reason,
            thread=threading.current_thread().name, read_sequence=self._read_seq,
            ring_context=self._ring_snapshot(),
        )

    def _emit(self, ev: EdgeEvent) -> None:
        self.events.append(ev)
        if self._external_writer is not None:
            try:
                self._external_writer(ev)
            except Exception:
                pass
        if self._write_files:
            try:
                self._write(ev)
            except Exception:
                pass

    def _write(self, ev: EdgeEvent) -> None:
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        jsonl = self._audit_dir / f"plc_edges_{day}.jsonl"
        csvf = self._audit_dir / f"plc_edges_{day}.csv"
        row = ev.to_row()
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        new = not csvf.exists() or csvf.stat().st_size == 0
        with csvf.open("a", encoding="utf-8", newline="") as f:
            if new:
                f.write(",".join(CSV_FIELDS) + "\n")
            f.write(",".join(_csv_cell(row[k]) for k in CSV_FIELDS) + "\n")

    def _ring_snapshot(self) -> List[dict]:
        return [{"mono": round(m, 4), "raw": r} for (m, _w, r) in self._ring]

    def _trim_ring(self, now: float) -> None:
        cutoff = now - self.ring_seconds
        while self._ring and self._ring[0][0] < cutoff:
            self._ring.popleft()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _iso(wall: float) -> str:
        return datetime.fromtimestamp(wall).isoformat(timespec="milliseconds")


def _csv_cell(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if any(c in s for c in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


def from_config(audit_dir: Optional[Path] = None) -> EdgeAuditor:
    """Build an auditor from the global PLC/SESSION config (used by the service)."""
    from ..config import PLC, SESSION, LOGS_DIR
    base = audit_dir
    if base is None:
        # runtime/audit next to runtime/logs
        base = Path(LOGS_DIR).parent / "audit"
    return EdgeAuditor(
        debounce_on_ms=float(getattr(PLC, "debounce_on_ms", 200.0)),
        debounce_off_ms=float(getattr(PLC, "debounce_off_ms", 200.0)),
        expected_pulse_min_ms=float(getattr(PLC, "expected_pulse_min_ms", 2000.0)),
        expected_pulse_max_ms=float(getattr(PLC, "expected_pulse_max_ms", 4500.0)),
        minimum_body_interval_sec=float(getattr(SESSION, "minimum_body_interval_sec", 90.0)),
        audit_dir=base,
    )
