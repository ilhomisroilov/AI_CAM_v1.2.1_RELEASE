"""
paddle_legacy.py — legacy PaddleOCR engine wrapped as an OCRRecognizer.

This adapter preserves today's OCR behaviour byte-for-byte: it drives the SAME
building blocks the production OCRWorker uses (process-isolated PaddleOCR pool,
named preprocessing variants, position-level fusion) and maps their output onto
the engine-neutral OCRRecognitionResult. It does NOT change the production hot
path (OCRWorker._process_job) — it is an additive seam so the pipeline can talk
to any engine through one interface.

Two recognition paths:
  * in-process deterministic path (tests / compatibility) via ``attach_test_engine``
    — no PaddleOCR process pool is started (network-free, deterministic);
  * production path via the real OCRProcessPool cascade.

Charset note: PaddleOCR's own output vocabulary is the full generic alphabet, so
this adapter reports charset ``alphanumeric_36``. Whether I/O/Q are *allowed* is a
Layer-3 validation-profile decision made downstream, not here.
"""
from __future__ import annotations

import time
from typing import List, Optional

from .. import ocr_contract as C


def _detect_paddle_version() -> str:
    try:
        from importlib.metadata import version
        return version("paddleocr")
    except Exception:
        return "unknown"


class PaddleOCRLegacyAdapter(C.OCRRecognizer):
    engine_id = "paddleocr_legacy"

    def __init__(self, worker=None, engine_model_id: str = "paddleocr_ppocr",
                 charset_id: str = C.ALPHANUMERIC_36) -> None:
        self._worker = worker
        self._engine_model_id = engine_model_id
        self._charset_id = charset_id
        self._version = _detect_paddle_version()
        self._test_engine = None
        self._test_engine_attached = False

    # -- engine construction -------------------------------------------------
    def _ensure_worker(self):
        if self._worker is None:
            from ..ocr_worker import OCRWorker
            # No-op callbacks: the adapter returns a value, it does not deliver
            # results into the pipeline. Session ownership stays with the caller.
            self._worker = OCRWorker(on_result=lambda *a, **k: None,
                                     on_fail=lambda *a, **k: None)
        return self._worker

    def attach_test_engine(self, engine) -> None:
        """TEST/COMPAT SEAM: inject an in-process engine exposing ``read(img)``.

        No PaddleOCR pool is started. Passing ``None`` simulates an unavailable
        engine (the adapter then returns an ENGINE_UNAVAILABLE result). When a real
        engine is attached, single-read acceptance is enabled so the deterministic
        path can accept one crop (matches tests/test_ocr_contract.py)."""
        self._test_engine_attached = True
        self._test_engine = engine
        w = self._ensure_worker()
        w._engine = engine
        if engine is not None:
            prod = w._fuse_params
            w._fuse_params = lambda: {**prod(), "accept_min_crops": 1,
                                      "accept_single_read": True}

    # -- OCRRecognizer -------------------------------------------------------
    def initialize(self) -> None:
        w = self._ensure_worker()
        if not self._test_engine_attached:
            w.preload()

    def recognize(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionResult:
        t0 = time.perf_counter()
        crops = [c.image for c in request.crops if c.image is not None]

        # Deliberately-unavailable in-process engine.
        if self._test_engine_attached and self._test_engine is None:
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=C.ENGINE_UNAVAILABLE, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id,
                warnings=["in-process engine unavailable"],
                latency_ms=(time.perf_counter() - t0) * 1000.0)

        w = self._ensure_worker()
        if not crops:
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=C.EMPTY, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id, warnings=["no crops"],
                latency_ms=(time.perf_counter() - t0) * 1000.0)

        if w._engine is not None:
            return self._recognize_in_process(request, crops, t0)
        return self._recognize_pool(request, crops, t0)

    # -- in-process deterministic path --------------------------------------
    def _recognize_in_process(self, request, crops, t0) -> C.OCRRecognitionResult:
        from ..ocr_worker import OCRJob
        w = self._worker
        job = OCRJob(frames=crops, session_id=request.session_id,
                     frame_id=request.job_id,
                     capture_timestamp=request.submitted_at or time.time(),
                     model_hint=request.model_hint)
        ocr_result = w.compute_result(job)
        dt = (time.perf_counter() - t0) * 1000.0
        if ocr_result is None:
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=C.EMPTY, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id, latency_ms=dt,
                warnings=["no accepted sequence"])
        # Ownership identifiers come from the request, not from the OCRResult, so
        # they are guaranteed to round-trip even if a future engine drops them.
        return C.make_result(
            request, engine_id=self.engine_id, engine_version=self._version,
            engine_status=C.OK, charset_id=self._charset_id,
            engine_model_id=self._engine_model_id,
            raw_sequence=getattr(ocr_result, "raw_vin", "") or ocr_result.vin,
            normalized_sequence=ocr_result.vin,
            sequence_confidence=float(ocr_result.confidence),
            latency_ms=dt)

    # -- production process-pool path ---------------------------------------
    def _recognize_pool(self, request, crops, t0) -> C.OCRRecognitionResult:
        from ...config import OCR
        w = self._worker
        if not w._ensure_pool():
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=C.ENGINE_UNAVAILABLE, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id,
                warnings=["OCR process pool unavailable"],
                latency_ms=(time.perf_counter() - t0) * 1000.0)
        tasks, crop_quality = w._build_tasks(crops)
        det_primary = bool(getattr(OCR, "paddle_det", False))
        fr, reads, telemetry, _early = w._run_variant_cascade(
            tasks, crop_quality, det=det_primary)
        dt = (time.perf_counter() - t0) * 1000.0
        return self._map_fusion(request, fr, reads, telemetry, w, dt)

    def _map_fusion(self, request, fr, reads, telemetry, w, dt) -> C.OCRRecognitionResult:
        raw = w._plurality_raw(reads) if reads else ""
        crop_evidence = [
            C.CropEvidenceRef(
                crop_index=getattr(r, "crop_index", 0),
                variant_name=getattr(r, "variant_name", ""),
                evidence_group=getattr(r, "evidence_group", 0) or 0,
                ocr_conf=float(getattr(r, "ocr_conf", 0.0)),
                raw_text=getattr(r, "raw_text", ""))
            for r in (reads or [])
        ]
        # Failure taxonomy takes precedence over an empty fusion when no reads.
        if not reads and telemetry is not None:
            if telemetry.engine_unavailable > 0:
                status = C.ENGINE_UNAVAILABLE
            elif telemetry.timeouts > 0:
                status = C.OCR_ENGINE_TIMEOUT
            elif telemetry.crashes > 0:
                status = C.OCR_ENGINE_CRASH
            elif telemetry.errors > 0:
                status = C.ERROR
            else:
                status = C.EMPTY
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=status, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id, raw_sequence=raw,
                crop_evidence=crop_evidence, latency_ms=dt,
                raw_payload=telemetry.raw_payloads,
                boxes=telemetry.boxes,
                warnings=[telemetry.summary()])

        if fr.status == "ACCEPT":
            char_preds = self._char_predictions(fr)
            return C.make_result(
                request, engine_id=self.engine_id, engine_version=self._version,
                engine_status=C.OK, charset_id=self._charset_id,
                engine_model_id=self._engine_model_id, raw_sequence=raw,
                normalized_sequence=fr.validated_vin,
                sequence_confidence=float(fr.final_score),
                char_predictions=char_preds, crop_evidence=crop_evidence,
                raw_payload=telemetry.raw_payloads,
                boxes=telemetry.boxes,
                latency_ms=dt)

        # NO_READ / OCR_AMBIGUOUS: not accepted, but raw evidence preserved.
        return C.make_result(
            request, engine_id=self.engine_id, engine_version=self._version,
            engine_status=C.EMPTY, charset_id=self._charset_id,
            engine_model_id=self._engine_model_id, raw_sequence=raw,
            crop_evidence=crop_evidence, latency_ms=dt,
            raw_payload=telemetry.raw_payloads,
            boxes=telemetry.boxes,
            warnings=[f"status={fr.status}"])

    @staticmethod
    def _char_predictions(fr) -> Optional[List[C.OCRCharacterPrediction]]:
        decisions = getattr(fr, "decisions", None)
        if not decisions:
            return None
        preds: List[C.OCRCharacterPrediction] = []
        for i, d in enumerate(decisions):
            ch = getattr(d, "chosen_char", "")
            margin = float(getattr(d, "margin", 0.0) or 0.0)
            preds.append(C.OCRCharacterPrediction(
                position=i, char=ch,
                confidence=max(0.0, min(1.0, margin)),
                alternatives=[]))
        return preds

    def health(self) -> C.EngineHealth:
        w = self._worker
        if self._test_engine_attached:
            ready = self._test_engine is not None
            return C.EngineHealth(
                engine_id=self.engine_id, ready=ready,
                status="ready" if ready else "engine_unavailable",
                detail="in-process test engine", last_check_at=time.time())
        status = "not_ready"
        last_err = None
        if w is not None:
            raw_status = getattr(w, "_engine_status", "not_ready")
            last_err = getattr(w, "_last_engine_error", "") or None
            status = {
                "ready": "ready",
                "engine_unavailable": "engine_unavailable",
            }.get(raw_status, "not_ready")
        return C.EngineHealth(
            engine_id=self.engine_id, ready=(status == "ready"), status=status,
            detail="paddleocr process pool", last_error=last_err,
            last_check_at=time.time())

    def metadata(self) -> C.EngineMetadata:
        return C.EngineMetadata(
            engine_id=self.engine_id, engine_version=self._version,
            engine_model_id=self._engine_model_id, engine_model_version="unknown",
            charset_id=self._charset_id, supports_per_char_confidence=True,
            supports_alternatives=False, max_batch_size=4, device="cuda")

    def close(self) -> None:
        if self._worker is not None and not self._test_engine_attached:
            try:
                self._worker.shutdown()
            except Exception:
                pass
