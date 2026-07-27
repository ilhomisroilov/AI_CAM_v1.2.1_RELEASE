"""Explicitly gated real PaddleOCR process-pool integration tests.

The default test suite must be hardware/network/cache independent, therefore real
Paddle execution is enabled only with ``AI_CAM_RUN_REAL_OCR=1``. When explicitly
enabled, missing dependencies and engine failures are FAILURES with the exact pool
status/metadata; they are never silently skipped and no 120-second polling loop is
used.

Production inference uses ``OCRProcessPool``. Session/frame ownership and supervisor
hang/crash recovery are covered deterministically in ``test_ocr_contract.py`` and
``test_ocr_process_worker.py``.
"""
from __future__ import annotations

import importlib.util
import os
import time

import pytest

from backend.ai.ocr_process import OK, OCRProcessPool
from backend.ai.ocr_worker import OCRWorker, normalize_vin


pytestmark = pytest.mark.real_ocr

_RUN_REAL = os.getenv("AI_CAM_RUN_REAL_OCR", "").strip() == "1"


def _load_crop():
    import cv2
    import numpy as np

    # Release-safe deterministic sample: never ship a photographed production
    # VIN merely to exercise the opt-in real-engine integration test.
    image = np.full((180, 1200, 3), 235, dtype=np.uint8)
    cv2.putText(
        image,
        "TEST123456789ABCD",
        (20, 120),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.2,
        (20, 20, 20),
        5,
        cv2.LINE_AA,
    )
    return image


@pytest.fixture(scope="module")
def real_pool():
    if not _RUN_REAL:
        pytest.skip(
            "real PaddleOCR opt-in test: AI_CAM_RUN_REAL_OCR=1 bilan alohida ishga tushiring"
        )
    if importlib.util.find_spec("paddleocr") is None:
        pytest.fail(
            "AI_CAM_RUN_REAL_OCR=1, ammo paddleocr o'rnatilmagan",
            pytrace=False,
        )

    cfg = OCRWorker._pool_cfg()
    device_override = os.getenv("AI_CAM_REAL_OCR_DEVICE", "").strip().lower()
    if device_override in {"cpu", "gpu"}:
        cfg["use_gpu"] = device_override == "gpu"

    pool = OCRProcessPool(
        cfg=cfg,
        n_workers=1,
        task_timeout_sec=30.0,
        start_timeout_sec=90.0,
        batch_timeout_sec=90.0,
    )
    pool.start()
    try:
        if not pool.preload():
            pytest.fail(
                "real PaddleOCR preload failed: "
                f"status={pool.engine_status} meta={pool.engine_unavailable_meta}",
                pytrace=False,
            )
        yield pool
    finally:
        pool.shutdown()


def _infer(pool: OCRProcessPool, crop):
    result = pool.run_batch(
        [(crop, True)],
        batch_timeout_sec=30.0,
    )[0]
    assert result.status == OK, (
        f"real PaddleOCR inference failed: status={result.status} meta={result.meta} "
        f"telemetry={pool.get_telemetry()}"
    )
    assert result.fragments, f"real PaddleOCR EMPTY: meta={result.meta}"
    return result


def test_real_paddleocr_process_pool_inference(real_pool):
    """Real PaddleOCR production poolning alohida child processida inference qiladi."""
    crop = _load_crop()
    parent_pid = os.getpid()
    worker_pid = real_pool.workers[0].proc.pid

    result = _infer(real_pool, crop)
    raw = normalize_vin("".join(text for text, _conf in result.fragments))
    confidence = max(float(conf) for _text, conf in result.fragments)

    assert worker_pid and worker_pid != parent_pid
    assert len(raw) == 17, f"raw={raw!r} fragments={result.fragments!r}"
    assert confidence > 0.5, f"confidence={confidence}"
    assert real_pool.last_batch_telemetry.crashes == 0
    assert real_pool.last_batch_telemetry.timeouts == 0
    assert real_pool.last_batch_telemetry.errors == 0


def test_real_model_loaded_once_second_job_fast(real_pool):
    """Preload qilingan model o'sha worker/PIDda qayta ishlatiladi."""
    crop = _load_crop()
    same_pid = real_pool.workers[0].proc.pid

    _infer(real_pool, crop)
    started = time.monotonic()
    _infer(real_pool, crop)
    second_elapsed = time.monotonic() - started

    assert real_pool.workers[0].proc.pid == same_pid
    assert real_pool._total_restarts == 0
    assert second_elapsed < 15.0, f"ikkinchi job {second_elapsed:.1f}s"
