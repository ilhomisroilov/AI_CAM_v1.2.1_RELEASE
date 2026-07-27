"""
ocr_orchestrator.py — session-owned three-engine OCR orchestration (v1.2.1).

Runs ENGRAVED_V121, PADDLE_RAW, PADDLE_ENHANCED concurrently against the same
session-owned input, with per-engine timeout + error isolation (one engine's failure
or timeout never cancels the others). Produces evidence for all engines and ONE final
decision candidate per the configured release mode. Never mutates one engine's result
with another's, and never invents a character.
"""
from __future__ import annotations
import concurrent.futures as cf
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from . import ocr_contract as C

# decision reasons
ENGRAVED_ACCEPTED = "ENGRAVED_ACCEPTED"
ENGRAVED_LOW_CONFIDENCE = "ENGRAVED_LOW_CONFIDENCE"
ENGRAVED_UNKNOWN_CHAR = "ENGRAVED_UNKNOWN_CHAR"
ENGRAVED_WEAK_CLASS_BLOCKED = "ENGRAVED_WEAK_CLASS_BLOCKED"
ENGRAVED_SEGMENTATION_REJECT = "ENGRAVED_SEGMENTATION_REJECT"
PADDLE_CONSENSUS_FALLBACK = "PADDLE_CONSENSUS_FALLBACK"
PADDLE_RAW_FALLBACK = "PADDLE_RAW_FALLBACK"
PADDLE_ENHANCED_FALLBACK = "PADDLE_ENHANCED_FALLBACK"
ENGINES_DISAGREE = "ENGINES_DISAGREE"
ENGINE_TIMEOUT = "ENGINE_TIMEOUT"
NO_SAFE_RESULT = "NO_SAFE_RESULT"
SHADOW_ONLY = "SHADOW_ONLY"

UNKNOWN_CHAR = "?"
ROLE_ENGRAVED = "ENGRAVED_V121"
ROLE_PADDLE_RAW = "PADDLE_RAW"
ROLE_PADDLE_ENHANCED = "PADDLE_ENHANCED"


@dataclass
class OrchestrationResult:
    session_id: object
    capture_generation: object
    engine_results: Dict[str, C.OCRRecognitionResult] = field(default_factory=dict)
    failures: Dict[str, str] = field(default_factory=dict)
    timeouts: List[str] = field(default_factory=list)
    per_engine_latency_ms: Dict[str, float] = field(default_factory=dict)
    total_latency_ms: float = 0.0
    disagreement: bool = False
    final_text: str = ""
    final_engine: str = ""
    final_confidence: float = 0.0
    decision_reason: str = NO_SAFE_RESULT


class OcrOrchestrator:
    def __init__(self, engines: Dict[str, C.OCRRecognizer], timeout_ms: int = 4000,
                 release_mode: str = "SHADOW", weak_classes: str = "5,6,7,A,B,D,H",
                 weak_enabled: bool = False, logger=None) -> None:
        self.engines = engines
        self.timeout_s = max(0.05, timeout_ms / 1000.0)
        self.release_mode = (release_mode or "SHADOW").upper()
        self.weak = set((weak_classes or "").replace(" ", "").split(",")) - {""}
        self.weak_enabled = weak_enabled
        self._log = logger

    def run(self, request: C.OCRRecognitionRequest,
            legacy_result_text: str = "", legacy_conf: float = 0.0) -> OrchestrationResult:
        out = OrchestrationResult(session_id=request.session_id,
                                  capture_generation=request.capture_generation)
        t0 = time.perf_counter()
        ex = cf.ThreadPoolExecutor(max_workers=max(1, len(self.engines)))
        fut = {}
        for role, eng in self.engines.items():
            req = C.OCRRecognitionRequest(
                session_id=request.session_id, capture_generation=request.capture_generation,
                job_id=request.job_id, crops=request.crops, submitted_at=request.submitted_at,
                model_hint=request.model_hint, requested_engine=role,
                recognition_mode="primary" if role == ROLE_ENGRAVED else "shadow",
                charset_profile=request.charset_profile)
            fut[ex.submit(self._run_one, role, eng, req)] = role
        deadline = t0 + self.timeout_s
        # engines run concurrently; gather each with its own remaining-time budget so one
        # slow/hung engine is recorded as a timeout without cancelling the others.
        for f, role in fut.items():
            try:
                res, lat = f.result(timeout=max(0.0, deadline - time.perf_counter()))
                out.engine_results[role] = res
                out.per_engine_latency_ms[role] = lat
            except cf.TimeoutError:
                out.timeouts.append(role)
            except Exception as exc:                # isolation: one engine failing is contained
                out.failures[role] = f"{type(exc).__name__}: {exc}"
        ex.shutdown(wait=False)                     # don't block on a hung engine's thread
        out.total_latency_ms = (time.perf_counter() - t0) * 1000.0
        # session-ownership guard: every stored result must carry this session_id
        for role, r in list(out.engine_results.items()):
            if r.session_id != request.session_id:
                out.failures[role] = "session_id_mismatch"
                out.engine_results.pop(role, None)
        self._decide(out, legacy_result_text, legacy_conf)
        return out

    @staticmethod
    def _run_one(role, eng, req):
        t = time.perf_counter()
        res = eng.recognize(req)
        return res, (time.perf_counter() - t) * 1000.0

    # ---- decision policy ----
    def _decide(self, out: OrchestrationResult, legacy_text: str, legacy_conf: float) -> None:
        eng = out.engine_results.get(ROLE_ENGRAVED)
        praw = out.engine_results.get(ROLE_PADDLE_RAW)
        penh = out.engine_results.get(ROLE_PADDLE_ENHANCED)
        raw_t = (praw.normalized_sequence or praw.raw_sequence) if praw else ""
        enh_t = (penh.normalized_sequence or penh.raw_sequence) if penh else ""
        eng_gated = eng.normalized_sequence if eng else ""
        eng_raw = eng.raw_sequence if eng else ""
        # disagreement across available engines
        seqs = [s for s in (eng_gated, raw_t, enh_t) if s]
        out.disagreement = len(set(seqs)) > 1

        if self.release_mode != "GUARDED_PRIMARY":
            # SHADOW: production decision stays legacy; engraved/enhanced are evidence only
            out.final_text = legacy_text
            out.final_engine = ROLE_PADDLE_RAW if legacy_text else ""
            out.final_confidence = legacy_conf
            out.decision_reason = SHADOW_ONLY
            return

        # GUARDED_PRIMARY
        if ROLE_ENGRAVED in out.timeouts:
            reason = ENGINE_TIMEOUT
        elif eng is None or eng.engine_status == C.ENGINE_UNAVAILABLE:
            reason = ENGRAVED_SEGMENTATION_REJECT if eng and eng.engine_status == C.EMPTY else ENGINE_TIMEOUT
        elif UNKNOWN_CHAR in eng_raw and not eng_gated:
            reason = ENGRAVED_UNKNOWN_CHAR
        elif not eng_gated or UNKNOWN_CHAR in eng_gated:
            reason = ENGRAVED_LOW_CONFIDENCE
        elif (not self.weak_enabled) and any(c in self.weak for c in eng_gated):
            reason = ENGRAVED_WEAK_CLASS_BLOCKED
        else:
            out.final_text = eng_gated; out.final_engine = ROLE_ENGRAVED
            out.final_confidence = eng.sequence_confidence; out.decision_reason = ENGRAVED_ACCEPTED
            return
        # engraved not accepted -> documented Paddle fallback (never invent a hybrid string)
        if raw_t and raw_t == enh_t:
            out.final_text, out.final_engine, out.final_confidence, out.decision_reason = (
                raw_t, ROLE_PADDLE_RAW, (praw.sequence_confidence if praw else 0.0), PADDLE_CONSENSUS_FALLBACK)
        elif raw_t and enh_t and raw_t != enh_t:
            out.final_text, out.final_engine, out.final_confidence, out.decision_reason = (
                legacy_text, ROLE_PADDLE_RAW, legacy_conf, ENGINES_DISAGREE)
        elif raw_t:
            out.final_text, out.final_engine, out.final_confidence, out.decision_reason = (
                raw_t, ROLE_PADDLE_RAW, (praw.sequence_confidence if praw else 0.0), PADDLE_RAW_FALLBACK)
        elif enh_t:
            out.final_text, out.final_engine, out.final_confidence, out.decision_reason = (
                enh_t, ROLE_PADDLE_ENHANCED, (penh.sequence_confidence if penh else 0.0), PADDLE_ENHANCED_FALLBACK)
        else:
            out.final_text, out.final_engine, out.final_confidence, out.decision_reason = (
                "", "", 0.0, NO_SAFE_RESULT)
        # keep the engraved reason as the primary cause note
        out.decision_reason = f"{reason}->{out.decision_reason}"
