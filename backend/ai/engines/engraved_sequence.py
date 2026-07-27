"""
engraved_sequence.py — integration SLOT for a future trained engraved OCR model.

THIS IS A DISABLED PLACEHOLDER, NOT A WORKING ENGINE. No model artifact exists in
this repository yet. Do NOT implement inference, fabricate predictions, or hardcode
results here until a real trained model + weights + charset spec are delivered by the
dataset/model team (see docs/v1.2.1/NEXT_OCR_INTEGRATION_PLAN.md).

Until then, recognize() always returns a structured ENGINE_NOT_CONFIGURED result —
it never returns a plausible identifier.
"""
from __future__ import annotations

import time
from typing import Optional

from .. import ocr_contract as C


class EngravedSequenceRecognizer(C.OCRRecognizer):
    engine_id = "engraved_sequence"

    def __init__(self, engine_id: str = "engraved_sequence",
                 model_path: Optional[str] = None, device: str = "auto",
                 charset_id: str = C.ALPHANUMERIC_36) -> None:
        self.engine_id = engine_id
        self.model_path = model_path
        self.device = device
        self.charset_id = charset_id
        self._ready = False

    def initialize(self) -> None:
        # A real implementation would attempt to load ``model_path`` here and only
        # then set ``_ready = True``. With no artifact, the engine never becomes
        # ready — this is deliberate.
        self._ready = False

    def recognize(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionResult:
        detail = (f"engine not configured: no model artifact at "
                  f"{self.model_path or '<unset model_path>'}")
        return C.make_result(
            request,
            engine_id=self.engine_id,
            engine_version="n/a",
            engine_status=C.ENGINE_NOT_CONFIGURED,
            charset_id=self.charset_id,
            raw_sequence="",
            normalized_sequence="",
            sequence_confidence=0.0,
            warnings=[detail],
            failure_type=C.ENGINE_NOT_CONFIGURED,
        )

    def health(self) -> C.EngineHealth:
        return C.EngineHealth(
            engine_id=self.engine_id,
            ready=False,
            status="not_configured",
            detail=f"model_path={self.model_path!r} not present or unset",
            last_error=None,
            last_check_at=time.time(),
        )

    def metadata(self) -> C.EngineMetadata:
        return C.EngineMetadata(
            engine_id=self.engine_id,
            engine_version="n/a",
            engine_model_id=None,
            engine_model_version=None,
            charset_id=self.charset_id,
            supports_per_char_confidence=True,
            supports_alternatives=True,
            max_batch_size=1,
            device=self.device,
        )

    def close(self) -> None:
        self._ready = False
