"""Focused production invariants for the single D2222 pulse flow."""

from __future__ import annotations

import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.config as cfg
from backend.database import db
from backend.pipeline import Pipeline
from backend.plc.plc_service import PLCService


class _FakeRFID:
    def __init__(self) -> None:
        self.called_at = None
        self.deadline = None

    def trigger_read(self, callback, stop_event=None, deadline=None):
        self.called_at = time.monotonic()
        self.deadline = deadline
        return True


def _bare_pipeline() -> Pipeline:
    """Create only the state needed by the session orchestrator (no device I/O)."""
    p = Pipeline.__new__(Pipeline)
    p._session_lock = threading.RLock()
    p._session_active = False
    p._session_id = 0
    p._session_deadline = 0.0
    p._session_finalized = False
    p._session_vin = None
    p._session_rfid = None
    p._session_stop = threading.Event()
    p._session_done = threading.Event()
    p._session_watchdog = None
    p._session_start_ts = 0.0
    p._rfid_grace_applied = False
    p._ocr_attempts = 0
    p._dropped_triggers = 0
    p._rfid_lock = threading.Lock()
    p._current_rfid = None
    p.stats = {"frames": 0, "detections": 0, "vins": 0, "rfid_reads": 0, "fps": 0.0}
    p.on_vin_done = None
    p.rfid_service = _FakeRFID()
    p._ensure_camera_stream = lambda: True
    p.camera_started_at = None
    p.start_processing = lambda: setattr(p, "camera_started_at", time.monotonic()) or True
    p.stop_processing = lambda: None
    p.pause_camera_stream = lambda: None
    p.disconnect_camera = lambda: None
    return p


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class D2222HardDeadlineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_patch = patch.object(db, "DB_PATH", Path(self._tmp.name) / "test.db")
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        self._tmp.cleanup()

    def test_d2222_rising_edge_is_the_only_pulse_start(self):
        with (patch.object(cfg.PLC, "mode", "simulator"),
              patch.object(cfg.PLC, "signal_kind", "value"),
              patch.object(cfg.PLC, "trigger_on_value", 1),
              patch.object(cfg.PLC, "trigger_mode", "pulse")):
            starts = []
            stops = []
            service = PLCService(on_start=lambda: starts.append(time.monotonic()),
                                 on_stop=lambda: stops.append(time.monotonic()))

            service._handle_transition(1)
            service._handle_transition(1)  # held high is not another edge
            service._handle_transition(0)  # falling edge does not close the session
            service._handle_transition(1)

        self.assertEqual(len(starts), 2)
        self.assertEqual(stops, [])
        for removed in ("dual_trigger", "rfid_signal_address", "camera_signal_address",
                        "exit_signal_address", "exit_grace_sec"):
            self.assertFalse(hasattr(cfg.PLC, removed))

    def test_hard_deadline_keeps_success_open_and_finalizes_once(self):
        duration = 0.20
        with (patch.object(cfg.SESSION, "timeout_sec", duration),
              patch.object(cfg.SESSION, "hold_until_deadline", True),
              patch.object(cfg.SESSION, "require_rfid", True),
              patch.object(cfg.PLC, "camera_delay_sec", 0.0),
              patch.object(cfg.PLC, "auto_off_on_vin", False),
              patch.object(cfg.RFID, "enabled", True)):
            p = _bare_pipeline()
            p.plc_on()
            sid = p._session_id
            accepted_at = p._session_start_ts
            self.assertAlmostEqual(p._session_deadline - accepted_at, duration, places=3)
            self.assertIsNotNone(p.rfid_service.called_at)
            self.assertIsNotNone(p.camera_started_at)
            self.assertLess(abs(p.rfid_service.called_at - p.camera_started_at), 0.05)
            self.assertEqual(p.rfid_service.deadline, p._session_deadline)

            # Even complete results and explicit early-close paths cannot shorten it.
            with p._session_lock:
                p._session_vin = {"vin": "NSTFA814ATJ123456", "conf": 0.99,
                                  "rel_path": None, "raw_vin": "NSTFA814ATJ123456",
                                  "model": "QY"}
                p._session_rfid = {"raw": "30001234", "number": "1234", "ts": None}
            p._maybe_complete_session()
            p.plc_off()
            p._finalize_session("SUCCESS")
            self.assertTrue(p._session_active)
            self.assertEqual(db.get_all_records(), [])

            self.assertTrue(_wait_until(lambda: not p._session_active, timeout=1.0))
            finalized_at = time.monotonic()
            self.assertGreaterEqual(finalized_at - accepted_at, duration - 0.01)
            rows = db.get_all_records()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], sid)
            self.assertEqual(rows[0]["status"], "SUCCESS")

            p._finalize_session("LATE_DUPLICATE")
            self.assertEqual(len(db.get_all_records()), 1)

    def test_legacy_mode_can_still_finish_early(self):
        with (patch.object(cfg.SESSION, "timeout_sec", 5.0),
              patch.object(cfg.SESSION, "hold_until_deadline", False),
              patch.object(cfg.SESSION, "require_rfid", False),
              patch.object(cfg.PLC, "camera_delay_sec", 0.0),
              patch.object(cfg.PLC, "auto_off_on_vin", False),
              patch.object(cfg.RFID, "enabled", False)):
            p = _bare_pipeline()
            p.plc_on()
            with p._session_lock:
                p._session_vin = {"vin": "NSTFA814ATJ123456", "conf": 0.99,
                                  "rel_path": None, "raw_vin": "NSTFA814ATJ123456",
                                  "model": "QY"}
            p._maybe_complete_session()

            self.assertFalse(p._session_active)
            self.assertEqual(len(db.get_all_records()), 1)


if __name__ == "__main__":
    unittest.main()
