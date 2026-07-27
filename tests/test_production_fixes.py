"""
============================================================
test_production_fixes.py — AI_CAM production-hardening regress testlari
============================================================
AI_CAM_AUDIT_REPORT.md dagi P1-P8 failure-mode'lari uchun regress testlar.
Fix'lardan KEYIN hammasi PASS bo'lishi shart.

Ishga tushirish (to'liq muhitda — numpy/opencv kerak):
    python tests/test_production_fixes.py
yoki:
    python -m pytest tests/test_production_fixes.py -q

Qamrov:
  P1 — PLC WORD registerdan bit/mask trigger (whole-word flap yo'q)
  P5 — poll davridan qisqa pulsni latch qiladi (o'tkazib yubormaydi)
  P6 — auto-off + held-high signal -> bitta sessiya (re-trigger bo'roni yo'q)
  P2 — session-aware dedup (yangi sessiyada bir xil VIN bloklanmaydi)
  P3 — OCR worker stop-during-job dan keyin tirik qoladi (sentinel poygasi yo'q)
  P8 — min_interval throttle job NI YO'QOTMAYDI
  P4 — DB session_id idempotentlik (bir sessiya = bir qator)
"""
from __future__ import annotations

import os
import sys
import gc
import tempfile
import time
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

RESULTS = []
def check(name, ok):
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


# ------------------------------------------------------------------
# P1 / P5 / P6 — PLC trigger qatlami
# ------------------------------------------------------------------
def test_plc():
    from backend.config import PLC
    from backend.plc.plc_service import PLCService
    from backend.plc.plc_base import PLCInterface

    # P1: WORD register [8720,8721,8720,1,8720], bit0 -> whole-word flap YO'Q
    PLC.mode = "simulator"; PLC.trigger_on_value = 1
    PLC.signal_kind = "bit"; PLC.signal_bit = 0; PLC.trigger_mask = 0
    starts, stops = [], []
    svc = PLCService(on_start=lambda: starts.append(1), on_stop=lambda: stops.append(1))
    svc._last_state = 0; svc._armed = True
    for v in [8720, 8721, 8720, 1, 8720]:
        svc._handle_transition(v)
    check("P1 PLC bit/mask (no whole-word flap)", len(starts) == 2 and len(stops) == 2)

    # P1b: bit0=0 bo'lgan katta word qiymatlar START bermaydi
    s2, st2 = [], []
    svc2 = PLCService(on_start=lambda: s2.append(1), on_stop=lambda: st2.append(1))
    svc2._last_state = 0; svc2._armed = True
    for v in [8720, 8722, 8730, 0, 8720]:
        svc2._handle_transition(v)
    check("P1b high-word bit0=0 -> no false start", len(s2) == 0)

    # P5: 50ms puls vs 500ms poll — simulyator latch
    PLC.poll_interval_ms = 500; PLC.signal_kind = "value"; PLC.signal_bit = -1
    s3 = []
    svc3 = PLCService(on_start=lambda: s3.append(1), on_stop=lambda: None)
    svc3.start(); time.sleep(0.2)
    svc3.set_sim_state(1); time.sleep(0.05); svc3.set_sim_state(0)
    time.sleep(0.9); svc3.stop()
    check("P5 short-pulse latch (50ms vs 500ms poll)", len(s3) >= 1)

    # P6: real-PLC held-high + auto-off -> bitta sessiya (re-trigger bo'roni yo'q)
    class HeldHigh(PLCInterface):
        def __init__(self): self.dones = []
        def connect(self): return True
        def read_signal(self): return 1
        def write_signal(self, a, v): self.dones.append((a, v)); return True
        def close(self): pass
    PLC.mode = "simulator"; PLC.signal_kind = "value"; PLC.trigger_on_value = 1
    PLC.poll_interval_ms = 30; PLC.done_address = "D522"; PLC.require_zero_before_rearm = True
    s4, st4 = [], []
    svc4 = PLCService(on_start=lambda: s4.append(1), on_stop=lambda: st4.append(1))
    fake = HeldHigh(); svc4.plc = fake
    svc4.start(); time.sleep(0.15)
    svc4.notify_vin_done(); time.sleep(0.4); svc4.stop()
    check("P6 held-high + auto-off -> 1 session (no storm)", len(s4) == 1)
    check("P6 DONE handshake written", len(fake.dones) >= 1 and fake.dones[0][0] == "D522")


# ------------------------------------------------------------------
# P2 / P3 / P8 — OCR worker
# ------------------------------------------------------------------
def test_ocr():
    import numpy as np
    from backend.ai.ocr_worker import OCRWorker
    from backend.config import OCR

    VALID = "NSTFA814ATJ123456"   # NSTF -> QY
    BOX = [[0, 0], [1, 0], [1, 1], [0, 1]]
    def crop(): return (np.random.rand(40, 120, 3) * 255).astype(np.uint8)
    class FakeEngine:
        def __init__(self, delay=0.0): self.gpu = False; self.det = True; self.delay = delay
        def read(self, img):
            if self.delay: time.sleep(self.delay)
            return [(BOX, VALID, 0.95)]
    def mk(on_result, on_fail=None, engine=None):
        w = OCRWorker(on_result=on_result, on_fail=on_fail)
        w._engine = engine or FakeEngine(); w._ensure_reader = lambda: True
        return w

    # P2: bir xil VIN, ikki HAR XIL sessiya -> ikkalasi ham yetkaziladi
    res, fails = [], []
    w = mk(lambda *a: res.append(a[0]), lambda *a: fails.append(a))
    OCR.duplicate_window_sec = 90.0
    w._process_job([crop()], None, session_id=1)
    w._process_job([crop()], None, session_id=2)
    check("P2 cross-session VIN delivered (no silent block)", len(res) == 2 and len(fails) == 0)

    # P2b: bir xil sessiyada takror -> ikkinchisi e'tiborsiz
    res2 = []
    w2 = mk(lambda *a: res2.append(a[0]))
    OCR.duplicate_window_sec = 90.0
    w2._process_job([crop()], None, session_id=5)
    w2._process_job([crop()], None, session_id=5)
    check("P2b intra-session dedup", len(res2) == 1)

    # P3: stop() ish vaqtida -> restartdan keyin ham qayta ishlaydi
    res3 = []
    w3 = mk(lambda *a: res3.append(a[0]), engine=FakeEngine(delay=1.0))
    OCR.duplicate_window_sec = 0.0
    w3.start(); w3.submit_frames([crop()], session_id=1); time.sleep(0.25)
    w3.stop(); time.sleep(0.1)
    w3.start(); res3.clear(); w3.submit_frames([crop()], session_id=2); time.sleep(1.2)
    check("P3 worker survives stop-during-job", len(res3) >= 1)
    w3.shutdown()

    # P8: min_interval throttle job NI YO'QOTMAYDI
    res4, fails4 = [], []
    w4 = mk(lambda *a: res4.append(a[0]), lambda *a: fails4.append(a))
    OCR.duplicate_window_sec = 0.0; OCR.min_interval_sec = 0.3
    w4._last_run = time.monotonic(); w4.start()
    w4.submit_frames([crop()], session_id=1); time.sleep(0.7)
    check("P8 throttle no job loss", len(res4) >= 1)
    OCR.min_interval_sec = 0.0; w4.shutdown()

    # P3b: 100x start/stop -> bitta worker thread, sog'lom
    res5 = []
    w5 = mk(lambda *a: res5.append(a[0])); OCR.duplicate_window_sec = 0.0
    for _ in range(100):
        w5.start(); w5.stop()
    w5.start(); res5.clear(); w5.submit_frames([crop()], session_id=1); time.sleep(0.6)
    n = sum(1 for t in threading.enumerate() if t.name == "ocr-worker")
    check("P3b survives 100x start/stop (1 thread)", len(res5) >= 1 and n == 1)
    w5.shutdown()


# ------------------------------------------------------------------
# P4 — DB session_id idempotentlik
# ------------------------------------------------------------------
def test_db():
    from backend.database import db
    original_path = db.DB_PATH
    # Regressiya testi tracked production DB ga yozmasligi kerak.
    with tempfile.TemporaryDirectory(prefix="ai_cam_db_test_") as tmp_dir:
        db.DB_PATH = Path(tmp_dir) / "test_ai_cam.db"
        try:
            db.init_db()
            sid = 9_000_000 + int(time.time()) % 100000   # noyob test sessiya id
            a = db.insert_record("2026-06-26 10:00:00", "NSTFA814ATJ123456", 0.9, None,
                                 model="QY", status="SUCCESS", session_id=sid)
            b = db.insert_record("2026-06-26 10:00:05", "NSTFA814ATJ123456", 0.9, None,
                                 model="QY", status="SUCCESS", session_id=sid)
            check("P4 same session_id -> same row id (idempotent)", a == b)
        finally:
            db.DB_PATH = original_path
            # sqlite3.Connection context manager transactionni yakunlaydi,
            # lekin Windows file handle'i GCgacha ochiq qolishi mumkin.
            gc.collect()


def main():
    print("PLC (P1/P5/P6):");  test_plc()
    print("OCR (P2/P3/P8):");  test_ocr()
    print("DB  (P4):");        test_db()
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok in RESULTS if ok)
    for n, ok in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
    print(f"{passed}/{len(RESULTS)} passed")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
