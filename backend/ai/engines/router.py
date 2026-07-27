"""
router.py — orchestrates primary / shadow / fallback OCR engines (v1.2.1).

Mode semantics (docs/v1.2.1/OCR_ENGINE_CONTRACT.md §7):
  * primary  — its result IS the production decision.
  * shadow   — invoked for audit/training only; can NEVER change the production
               decision (best-effort, off the critical path, exceptions swallowed).
  * fallback — invoked ONLY when the primary result carries a failure status in
               FALLBACK_TRIGGER_STATUSES; its result becomes production only if it
               is itself OK with a non-empty sequence.
  * disabled — engine is simply not passed to the router (never invoked).

All modes preserve session_id / capture_generation / job_id, because every engine
builds its result via ocr_contract.make_result, which echoes the request identifiers.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from .. import ocr_contract as C


class EngineRouter:
    def __init__(self, primary: C.OCRRecognizer,
                 shadow: Optional[C.OCRRecognizer] = None,
                 fallback: Optional[C.OCRRecognizer] = None,
                 logger=None) -> None:
        if primary is None:
            raise ValueError("EngineRouter requires a primary engine")
        self.primary = primary
        self.shadow = shadow
        self.fallback = fallback
        self._log = logger
        self.last_shadow_result: Optional[C.OCRRecognitionResult] = None
        self.last_fallback_invoked: bool = False

    @staticmethod
    def _with_mode(request: C.OCRRecognitionRequest, mode: str) -> C.OCRRecognitionRequest:
        return dataclasses.replace(request, recognition_mode=mode)

    def recognize(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionResult:
        self.last_shadow_result = None
        self.last_fallback_invoked = False

        result = self.primary.recognize(self._with_mode(request, "primary"))

        # Shadow — never affects the production decision.
        if self.shadow is not None:
            try:
                self.last_shadow_result = self.shadow.recognize(
                    self._with_mode(request, "shadow"))
                self._audit_shadow(result, self.last_shadow_result)
            except Exception as exc:   # shadow must never break production
                if self._log is not None:
                    self._log.warning(f"[OCR_ROUTER] shadow engine error (ignored): {exc}")

        # Fallback — only under an explicitly enumerated failure condition.
        if self.fallback is not None and result.engine_status in C.FALLBACK_TRIGGER_STATUSES:
            self.last_fallback_invoked = True
            try:
                fb = self.fallback.recognize(self._with_mode(request, "fallback"))
                if fb.engine_status == C.OK and fb.normalized_sequence:
                    if self._log is not None:
                        self._log.info(
                            f"[OCR_ROUTER] fallback {self.fallback.engine_id} accepted "
                            f"after primary status={result.engine_status}.")
                    return fb
            except Exception as exc:
                if self._log is not None:
                    self._log.error(f"[OCR_ROUTER] fallback engine error: {exc}")

        return result

    def _audit_shadow(self, production: C.OCRRecognitionResult,
                      shadow: C.OCRRecognitionResult) -> None:
        if self._log is None:
            return
        agree = production.normalized_sequence == shadow.normalized_sequence
        self._log.info(
            f"[OCR_ROUTER] SHADOW {shadow.engine_id}: shadow="
            f"{shadow.normalized_sequence or '-'} prod="
            f"{production.normalized_sequence or '-'} agree={int(agree)} "
            f"(production decision unchanged).")

    def health(self) -> list:
        out = []
        for engine in (self.primary, self.shadow, self.fallback):
            if engine is None:
                continue
            try:
                out.append(engine.health())
            except Exception:
                out.append(C.EngineHealth(
                    engine_id=getattr(engine, "engine_id", "unknown"),
                    ready=False, status="not_ready", detail="health() raised"))
        return out
