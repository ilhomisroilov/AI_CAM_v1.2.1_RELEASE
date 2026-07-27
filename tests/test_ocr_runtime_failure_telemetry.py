"""OCR runtime failure classification, circuit breaker and deadline regressions."""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from backend.ai.ocr_process import (
    EMPTY,
    ENGINE_UNAVAILABLE,
    ERROR,
    OCR_ENGINE_TIMEOUT,
    OCRProcessPool,
    OCRTaskResult,
    _EngineUnavailableError,
    _WorkerEngine,
)
from backend.ai.ocr_worker import OCRWorker


def _img(code: int = 9):
    image = np.full((8, 8, 3), 200, dtype=np.uint8)
    image.reshape(-1)[0] = code
    return image


def _fatal_worker(_cfg, in_q, out_q):
    while True:
        item = in_q.get()
        if item is None:
            return
        task_id, _image, _det = item
        out_q.put((task_id, ENGINE_UNAVAILABLE, [], {
            "reason": "engine_unavailable",
            "phase": "engine_init",
            "fatal": True,
            "err": "RuntimeError: Cannot load cudnn shared library",
        }))


def _python_error_worker(_cfg, in_q, out_q):
    while True:
        item = in_q.get()
        if item is None:
            return
        task_id, _image, _det = item
        out_q.put((task_id, ERROR, [], {
            "reason": "python_exception", "err": "ValueError: bad crop",
        }))


def _slow_worker(_cfg, in_q, out_q):
    while True:
        item = in_q.get()
        if item is None:
            return
        task_id, _image, _det = item
        time.sleep(5)
        out_q.put((task_id, EMPTY, [], {"fake": True}))


def test_cudnn_constructor_failure_does_not_try_six_signatures(monkeypatch):
    class FatalPaddleOCR:
        calls = 0

        def __init__(self, **_kwargs):
            type(self).calls += 1
            raise RuntimeError(
                "(PreconditionNotMet) Cannot load cudnn shared library; "
                "cudnn_dso_handle should not be null"
            )

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=FatalPaddleOCR))
    with pytest.raises(_EngineUnavailableError) as raised:
        _WorkerEngine({"use_gpu": True, "lang": "en"})

    assert FatalPaddleOCR.calls == 1
    assert raised.value.phase == "engine_init"
    assert "cudnn" in raised.value.detail.lower()


def test_fatal_init_opens_pool_circuit_and_cancels_remaining_16_tasks():
    pool = OCRProcessPool(
        cfg={}, n_workers=1, task_timeout_sec=5, start_timeout_sec=5,
        batch_timeout_sec=10, worker_target=_fatal_worker,
    )
    try:
        started = time.monotonic()
        results = pool.run_batch([(_img(i + 2), False) for i in range(16)])
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert len(results) == 16
        assert all(result.status == ENGINE_UNAVAILABLE for result in results)
        assert pool.engine_status == "engine_unavailable"
        assert pool.workers[0].warmed is False
        assert pool.last_batch_telemetry.engine_unavailable == 16
        assert "cudnn" in pool.engine_unavailable_meta["err"].lower()

        # Circuit ochiq: keyingi job workerga ham yuborilmaydi.
        started = time.monotonic()
        second = pool.run_batch([(_img(), False)] * 16)
        assert time.monotonic() - started < 0.25
        assert all(result.status == ENGINE_UNAVAILABLE for result in second)
    finally:
        pool.shutdown()


def test_python_error_metadata_is_kept_and_worker_is_not_marked_warm():
    pool = OCRProcessPool(
        cfg={}, n_workers=1, task_timeout_sec=2, start_timeout_sec=2,
        batch_timeout_sec=4, worker_target=_python_error_worker,
    )
    try:
        result = pool.run_batch([(_img(), False)])[0]
        assert result.status == ERROR
        assert result.meta == {
            "reason": "python_exception", "err": "ValueError: bad crop",
        }
        assert pool.workers[0].warmed is False
        assert pool.last_batch_telemetry.errors == 1
        assert pool.last_batch_telemetry.crashes == 0
        assert pool.last_batch_telemetry.timeouts == 0
    finally:
        pool.shutdown()


def test_preload_failure_exposes_engine_unavailable_status_and_reason():
    pool = OCRProcessPool(
        cfg={}, n_workers=1, task_timeout_sec=2, start_timeout_sec=2,
        worker_target=_fatal_worker,
    )
    try:
        assert pool.preload() is False
        assert pool.engine_status == "engine_unavailable"
        assert pool.engine_unavailable_meta["reason"] == "engine_unavailable"
        assert "cudnn" in pool.engine_unavailable_meta["err"].lower()
    finally:
        pool.shutdown()


def test_batch_deadline_is_timeout_not_crash():
    pool = OCRProcessPool(
        cfg={}, n_workers=1, task_timeout_sec=10, start_timeout_sec=10,
        batch_timeout_sec=0.20, worker_target=_slow_worker,
    )
    try:
        results = pool.run_batch([(_img(), False), (_img(), False)])
        assert [result.status for result in results] == [
            OCR_ENGINE_TIMEOUT, OCR_ENGINE_TIMEOUT,
        ]
        assert results[0].meta["reason"] == "batch_deadline_inflight"
        assert results[1].meta["reason"] == "batch_deadline_not_started"
        assert pool.last_batch_telemetry.timeouts == 2
        assert pool.last_batch_telemetry.crashes == 0
        assert pool.last_batch_telemetry.deadline_exceeded is True
    finally:
        pool.shutdown()


def test_worker_cascade_stops_after_primary_engine_unavailable():
    class CircuitPool:
        def __init__(self):
            self.calls = 0
            self.submitted = 0

        def run_batch(self, tasks, **_kwargs):
            self.calls += 1
            self.submitted += len(tasks)
            meta = {
                "reason": "engine_unavailable", "fatal": True,
                "err": "RuntimeError: Cannot load cudnn shared library",
            }
            return [OCRTaskResult(ENGINE_UNAVAILABLE, [], dict(meta))
                    for _ in tasks]

    def variant(name: str):
        return SimpleNamespace(
            image=np.zeros((8, 8, 3), dtype=np.uint8),
            variant_name=name,
            weight=1.0,
        )

    # 2 crop x raw/CLAHE primary + 12 retry = serverdagi 16 variant modeli.
    tasks = [
        (0, variant("raw_resized")), (1, variant("raw_resized")),
        (0, variant("clahe_unsharp")), (1, variant("clahe_unsharp")),
    ]
    tasks.extend((i % 2, variant(f"blackhat_relief@{i}")) for i in range(12))

    worker = OCRWorker(on_result=lambda *_args: None)
    worker._pool = CircuitPool()
    worker._evidence_groups = {0: 0, 1: 1}
    _fused, reads, telemetry, early = worker._run_variant_cascade(
        tasks, [0.91, 0.95], det=True, deadline=time.monotonic() + 5)

    assert reads == []
    assert early is False
    assert telemetry.engine_unavailable == 4
    assert telemetry.error_details[0]["err"].startswith("RuntimeError")
    assert worker._pool.calls == 1
    assert worker._pool.submitted == 4  # retry'dagi qolgan 12 task yuborilmadi

