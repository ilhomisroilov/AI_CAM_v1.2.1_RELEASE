"""Non-blocking live three-engine OCR hook.

The hook is dispatched when the session-owned crop is ready, before the legacy
decision exists. ENGRAVED_V121, PADDLE_RAW and PADDLE_ENHANCED therefore receive
the same owned input stage, including evidence that later becomes AMBIGUOUS or
NO_READ. In SHADOW mode only the later legacy callback writes final semantics.
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import os
import threading
from typing import Optional

import numpy as np

from .ocr_orchestrator import (
    ROLE_ENGRAVED,
    ROLE_PADDLE_ENHANCED,
    ROLE_PADDLE_RAW,
    SHADOW_ONLY,
)
from .ocr_shadow_stage import ShadowOcrStage
from ..database import ocr_v121_db as odb

_lock = threading.Lock()
_stage: Optional[ShadowOcrStage] = None
_collector = None
_built = False
_disabled_reason = None
_executor = futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="ocr-v121-shadow"
)
_submitted: set[tuple[str, str]] = set()
_completed = 0
_failed = 0


def _store_collection(conn_factory, item) -> bool:
    conn = conn_factory()
    try:
        return odb.store_collection_item(conn, item)
    finally:
        conn.close()


def _build() -> None:
    global _stage, _collector, _built, _disabled_reason
    if _built:
        return
    with _lock:
        if _built:
            return
        _built = True
        try:
            from ..config import OCR_RELEASE, PROJECT_ROOT
            from ..database import db as _db
            from ..logger import log

            if str(getattr(OCR_RELEASE, "release_mode", "SHADOW")).upper() == "DISABLED":
                _disabled_reason = "disabled by config"
                return
            engines = {}
            if getattr(OCR_RELEASE, "engraved_enabled", True):
                from .engines.engraved_v121 import EngravedV121Recognizer

                model_dir = PROJECT_ROOT / getattr(
                    OCR_RELEASE,
                    "engraved_model_dir",
                    "models/engraved_ocr_v1.2.1",
                )
                engraved = EngravedV121Recognizer(
                    str(model_dir),
                    expected_length=getattr(
                        OCR_RELEASE, "engraved_expected_length", 17
                    ),
                )
                engraved.initialize()
                engines[ROLE_ENGRAVED] = engraved
            if getattr(OCR_RELEASE, "paddle_raw_enabled", True):
                from .engines.paddle_profiles import PaddleRawRecognizer

                raw = PaddleRawRecognizer()
                raw.initialize()
                engines[ROLE_PADDLE_RAW] = raw
            if getattr(OCR_RELEASE, "paddle_enhanced_enabled", True):
                from .engines.paddle_profiles import PaddleEnhancedRecognizer

                enhanced = PaddleEnhancedRecognizer()
                enhanced.initialize()
                engines[ROLE_PADDLE_ENHANCED] = enhanced

            required = {ROLE_ENGRAVED, ROLE_PADDLE_RAW, ROLE_PADDLE_ENHANCED}
            missing = sorted(required - set(engines))
            if missing:
                _disabled_reason = f"required OCR engines disabled/missing: {missing}"
                return
            unhealthy = {
                role: engine.health().last_error or engine.health().detail
                for role, engine in engines.items()
                if not engine.health().ready
            }
            if unhealthy:
                _disabled_reason = f"OCR engines not ready: {unhealthy}"
                for engine in engines.values():
                    engine.close()
                return

            conn_factory = _db._connect
            connection = conn_factory()
            try:
                odb.migrate(connection)
            finally:
                connection.close()

            collector = None
            if getattr(OCR_RELEASE, "collection_enabled", True):
                from .ocr_collector import ActiveLearningCollector

                root = PROJECT_ROOT / getattr(
                    OCR_RELEASE,
                    "collection_root",
                    "runtime/engraved_ocr_collection",
                )
                collector = ActiveLearningCollector(
                    str(root),
                    store_fn=lambda item: _store_collection(conn_factory, item),
                    target_chars=getattr(
                        OCR_RELEASE, "collection_target_chars", ""
                    ),
                    logger=log,
                )
            _collector = collector
            _stage = ShadowOcrStage(
                OCR_RELEASE,
                conn_factory,
                engines,
                collector=collector,
                logger=log,
            )
            log.info(
                "[OCR_V121] live common-input stage ready: "
                "ENGRAVED_V121 + PADDLE_RAW + PADDLE_ENHANCED "
                f"(mode={OCR_RELEASE.release_mode})."
            )
        except Exception as exc:
            _disabled_reason = f"build error: {type(exc).__name__}: {exc}"


def _input_hash(crop) -> str:
    array = np.ascontiguousarray(np.asarray(crop))
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes())
    return digest.hexdigest()


def _run_input(session_id, crop, capture_generation, trusted_vin) -> dict:
    global _completed, _failed
    _build()
    if _stage is None:
        _failed += 1
        return {"ok": False, "error": _disabled_reason or "stage unavailable"}
    try:
        result = _stage.process(
            session_id=session_id,
            capture_generation=capture_generation,
            normalized_line=np.asarray(crop),
            frame=np.asarray(crop),
            trusted_vin=trusted_vin,
            legacy_text="",
            legacy_conf=0.0,
            # Evidence is valid even after capture release. Final legacy
            # semantics are written separately by record_legacy_shadow_final.
            is_owned=None,
            write_final=False,
        )
        _completed += 1
        return result
    except Exception:
        _failed += 1
        raise


def dispatch_shadow_ocr_input(
    *,
    session_id,
    crop,
    capture_generation=0,
    trusted_vin: str = "",
):
    """Schedule the common-input stage without blocking capture or legacy OCR."""
    if session_id is None or crop is None:
        return None
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "AI_CAM_TEST_REAL_SHADOW"
    ):
        return None
    signature = _input_hash(crop)
    key = (str(session_id), signature)
    with _lock:
        if key in _submitted:
            return None
        _submitted.add(key)
    return _executor.submit(
        _run_input,
        session_id,
        np.asarray(crop).copy(),
        capture_generation,
        trusted_vin,
    )


def record_legacy_shadow_final(
    *,
    session_id,
    legacy_vin: str,
    legacy_conf: float,
) -> None:
    """Write only the legacy SHADOW final; never invoke or echo an OCR engine."""
    if session_id is None:
        return
    try:
        from ..database import db as _db

        conn = _db._connect()
        try:
            odb.migrate(conn)
            odb.set_final_ocr(
                conn,
                session_id=str(session_id),
                text=legacy_vin or "",
                engine=ROLE_PADDLE_RAW if legacy_vin else "",
                confidence=float(legacy_conf or 0.0),
                decision_reason=SHADOW_ONLY,
                model_version="1.2.1",
                disagreement=False,
            )
        finally:
            conn.close()
    except Exception:
        # Additive SHADOW telemetry must not break production finalization.
        return


def dispatch_shadow_ocr(
    *,
    session_id,
    crop,
    legacy_vin,
    legacy_conf,
    pipeline=None,
    trusted_vin: str = "",
) -> None:
    """Backward-compatible name: record final only, with no Paddle echo engine."""
    record_legacy_shadow_final(
        session_id=session_id,
        legacy_vin=legacy_vin,
        legacy_conf=legacy_conf,
    )


def shadow_status() -> dict:
    try:
        from .engines.paddle_profiles import paddle_inference_counters

        counters = paddle_inference_counters()
    except Exception:
        counters = {}
    return {
        "built": _built,
        "ready": _stage is not None,
        "disabled_reason": _disabled_reason,
        "submitted": len(_submitted),
        "completed": _completed,
        "failed": _failed,
        "paddle_inference_calls": counters,
        "engines": (
            sorted(_stage.orch.engines)
            if _stage is not None
            else []
        ),
    }
