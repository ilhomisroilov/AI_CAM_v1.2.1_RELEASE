"""
Simulation test for OCRProcessPool crash/timeout/restart handling (Section 1).
Uses a FAKE worker target (no PaddleOCR) to verify that a native crash or hang in
an OCR worker does NOT kill the main process, that crash and timeout statuses are
separate, and that the dead/hung worker is auto-recreated.

Run:  python tests/test_ocr_process_sim.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402

from backend.ai.ocr_process import (  # noqa: E402
    OCRProcessPool, OK, OCR_ENGINE_CRASH, OCR_ENGINE_TIMEOUT,
)


def _fake_worker_main(cfg, in_q, out_q):
    """First image pixel decides behavior: 0=crash(os._exit), 1=hang, else echo OK."""
    while True:
        item = in_q.get()
        if item is None:
            break
        task_id, img, det = item
        code = int(img.reshape(-1)[0])
        if code == 0:
            os._exit(139)                       # native crash (no Python exception)
        if code == 1:
            time.sleep(30)                      # hang -> pool timeout
            continue
        out_q.put((task_id, OK, [("NSTFB814ETJ039378", 0.97)], {"fake": True}))


def _img(code):
    a = np.full((8, 8, 3), 200, dtype=np.uint8)
    a.reshape(-1)[0] = code
    return a


def main() -> int:
    pool = OCRProcessPool(
        cfg={"lang": "en"}, n_workers=1,
        task_timeout_sec=1.5, start_timeout_sec=2.0, max_restarts=10,
        worker_target=_fake_worker_main,
    )
    pool.start()

    r = pool.run_batch([(_img(5), False)])
    assert r[0] and r[0].status == OK
    print("  PASS  normal OK task")

    r = pool.run_batch([(_img(0), False)])
    assert r[0] and r[0].status == OCR_ENGINE_CRASH
    print(f"  PASS  SIGSEGV-like crash -> {r[0].status} (main alive)")

    r = pool.run_batch([(_img(7), False)])
    assert r[0] and r[0].status == OK
    print("  PASS  worker auto-recreated after crash")

    r = pool.run_batch([(_img(1), False)])
    assert (r[0] and r[0].status == OCR_ENGINE_TIMEOUT
            and r[0].meta.get("reason") == "task_timeout")
    print("  PASS  native hang -> OCR_ENGINE_TIMEOUT (CRASH'dan alohida)")

    r = pool.run_batch([(_img(9), False)])
    assert r[0] and r[0].status == OK
    print("  PASS  recovered after timeout")

    r = pool.run_batch([(_img(5), False), (_img(0), False), (_img(6), False)])
    assert r[0].status == OK and r[1].status == OCR_ENGINE_CRASH and r[2].status == OK
    print("  PASS  mixed batch aligned (OK / CRASH / OK)")

    pool.shutdown()
    print("\nALL PROCESS-POOL SIM TESTS PASSED — main survived every crash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
