"""
paddle_profiles.py — PADDLE_RAW and PADDLE_ENHANCED as independent OCRRecognizer engines.

Both reuse the real PaddleOCRLegacyAdapter (process-isolated PaddleOCR). They are
operationally independent (separate adapter instances, separate preprocessing) and never
mutate each other's result. PADDLE_RAW preserves the untouched raw plurality text as
raw_sequence; no hardcoded VIN correction is applied to the stored raw evidence.
"""
from __future__ import annotations
import threading
from typing import Optional
import numpy as np
from .. import ocr_contract as C
from .paddle_legacy import PaddleOCRLegacyAdapter

_COUNTER_LOCK = threading.Lock()
_INFERENCE_CALLS = {"PADDLE_RAW": 0, "PADDLE_ENHANCED": 0}


def paddle_inference_counters() -> dict:
    with _COUNTER_LOCK:
        return dict(_INFERENCE_CALLS)


def reset_paddle_inference_counters() -> None:
    with _COUNTER_LOCK:
        for key in _INFERENCE_CALLS:
            _INFERENCE_CALLS[key] = 0


def _enhance(img: np.ndarray) -> np.ndarray:
    """Controlled contrast normalization (the enhanced profile). Identity-preserving."""
    a = np.asarray(img).astype(np.float32)
    if a.ndim == 3:
        a = a.mean(axis=2)
    lo, hi = np.percentile(a, 2), np.percentile(a, 98)
    if hi > lo:
        a = np.clip((a - lo) * 255.0 / (hi - lo), 0, 255)
    return a.astype(np.uint8)


class _PaddleProfile(C.OCRRecognizer):
    engine_id = "PADDLE"
    profile = "raw"

    def __init__(self) -> None:
        self._adapter = PaddleOCRLegacyAdapter(charset_id=C.ALPHANUMERIC_36)

    def initialize(self) -> None:
        self._adapter.initialize()

    def attach_test_engine(self, engine) -> None:      # test/offline seam
        self._adapter.attach_test_engine(engine)

    def _preprocess(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionRequest:
        return request

    def recognize(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionResult:
        with _COUNTER_LOCK:
            _INFERENCE_CALLS[self.engine_id] += 1
        res = self._adapter.recognize(self._preprocess(request))
        # relabel engine + record the preprocessing profile; do NOT alter the text
        res.engine_id = self.engine_id
        res.warnings = list(res.warnings) + [f"profile={self.profile}"]
        return res

    def health(self) -> C.EngineHealth:
        h = self._adapter.health(); h.engine_id = self.engine_id; return h

    def metadata(self) -> C.EngineMetadata:
        m = self._adapter.metadata(); m.engine_id = self.engine_id; return m

    def close(self) -> None:
        self._adapter.close()


class PaddleRawRecognizer(_PaddleProfile):
    engine_id = "PADDLE_RAW"
    profile = "raw"
    # raw: crops passed through unchanged; raw_sequence preserved untouched.


class PaddleEnhancedRecognizer(_PaddleProfile):
    engine_id = "PADDLE_ENHANCED"
    profile = "enhanced_contrast"

    def _preprocess(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionRequest:
        crops = [C.CropRef(crop_index=c.crop_index, image=_enhance(c.image),
                           evidence_group=c.evidence_group, source="enhanced")
                 for c in request.crops if c.image is not None]
        import dataclasses
        return dataclasses.replace(request, crops=crops)
