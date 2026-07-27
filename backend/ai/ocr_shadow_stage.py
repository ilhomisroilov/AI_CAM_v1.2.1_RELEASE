"""
ocr_shadow_stage.py — session-owned v1.2.1 OCR integration stage.

Single entry point the production pipeline calls once the session-owned normalized line
is ready. Runs the three-engine orchestrator, persists per-engine evidence rows + the ONE
final session-owned OCR decision, and dispatches the non-blocking collector. In SHADOW
(default) the existing legacy decision remains the production final; the new engines are
evidence only and CANNOT alter the VIN/RFID binding. Fully error-isolated: any failure
here never breaks the production session.
"""
from __future__ import annotations
import time
from typing import Callable, Dict, Optional
import numpy as np
from . import ocr_contract as C
from .ocr_orchestrator import (OcrOrchestrator, ROLE_ENGRAVED, ROLE_PADDLE_RAW,
                               ROLE_PADDLE_ENHANCED, SHADOW_ONLY, UNKNOWN_CHAR)
from ..database import ocr_v121_db as odb


class ShadowOcrStage:
    def __init__(self, cfg, conn_factory: Callable, engines: Dict[str, C.OCRRecognizer],
                 collector=None, logger=None) -> None:
        self.cfg = cfg
        self.conn_factory = conn_factory      # () -> sqlite3.Connection
        self.orch = OcrOrchestrator(
            engines, timeout_ms=getattr(cfg, "engine_timeout_ms", 4000),
            release_mode=getattr(cfg, "release_mode", "SHADOW"),
            weak_classes=getattr(cfg, "weak_classes", "5,6,7,A,B,D,H"),
            weak_enabled=getattr(cfg, "weak_classes_enabled", False), logger=logger)
        self.collector = collector
        self._log = logger
        self.errors = 0
        self.last_latency_ms: Dict[str, float] = {}
        self.timeout_count = 0
        self.engine_error_count = 0

    def process(self, *, session_id, capture_generation, normalized_line: np.ndarray,
                frame=None, trusted_vin: str = "", legacy_text: str = "", legacy_conf: float = 0.0,
                is_owned: Optional[Callable[[object], bool]] = None,
                write_final: bool = True) -> dict:
        try:
            return self._process(session_id, capture_generation, normalized_line, frame,
                                 trusted_vin, legacy_text, legacy_conf, is_owned,
                                 write_final)
        except Exception as exc:               # SHADOW must never break the session
            self.errors += 1
            if self._log: self._log.error(f"[OCR_V121] shadow stage error (isolated): {exc}")
            return {"ok": False, "error": str(exc), "decision_reason": SHADOW_ONLY}

    def _process(self, session_id, capture_generation, normalized_line, frame,
                 trusted_vin, legacy_text, legacy_conf, is_owned, write_final):
        req = C.OCRRecognitionRequest(
            session_id=session_id, capture_generation=capture_generation, job_id=session_id,
            crops=[C.CropRef(crop_index=0, image=normalized_line)], submitted_at=time.time(),
            model_hint=None)
        result = self.orch.run(req, legacy_result_text=legacy_text, legacy_conf=legacy_conf)
        self.last_latency_ms = dict(result.per_engine_latency_ms)
        self.timeout_count += len(result.timeouts)
        self.engine_error_count += len(result.failures)

        # session-ownership re-check before ANY write
        if is_owned is not None and not is_owned(session_id):
            if self._log: self._log.warning(f"[OCR_V121] session {session_id} no longer owns capture; "
                                            "storing evidence only, no final write")
        conn = self.conn_factory()
        try:
            # store 3 engine evidence rows (raw Paddle preserved unchanged)
            if getattr(self.cfg, "store_raw_results", True):
                for role, res in result.engine_results.items():
                    per_char = [{"position": cp.position, "char": cp.char, "confidence": cp.confidence,
                                 "alternatives": cp.alternatives} for cp in (res.char_predictions or [])]
                    odb.store_engine_result(
                        conn, session_id=str(session_id), engine_name=role,
                        engine_role=("primary_candidate" if role == ROLE_ENGRAVED else "evidence"),
                        raw_text=res.raw_sequence, gated_text=res.normalized_sequence,
                        raw_payload={
                            "engine_payload": res.raw_payload,
                            "warnings": res.warnings,
                            "failure_type": res.failure_type,
                        },
                        per_character=per_char,
                        confidence=[cp.confidence for cp in (res.char_predictions or [])],
                        boxes=(res.boxes or [
                            {"crop_index": e.crop_index, "variant": e.variant_name}
                            for e in res.crop_evidence
                        ]),
                        preprocessing_profile=("raw" if role == ROLE_PADDLE_RAW else
                                               "enhanced" if role == ROLE_PADDLE_ENHANCED else "adaptive"),
                        model_name=res.engine_model_id or role, model_version=res.engine_model_version or "",
                        latency_ms=result.per_engine_latency_ms.get(role, 0.0),
                        status=res.engine_status, error_code=(res.failure_type or ""))
                for role in result.timeouts:
                    odb.store_engine_result(conn, session_id=str(session_id), engine_name=role,
                                            status="OCR_ENGINE_TIMEOUT", error_code="TIMEOUT",
                                            latency_ms=self.orch.timeout_s*1000)
                for role, err in result.failures.items():
                    odb.store_engine_result(conn, session_id=str(session_id), engine_name=role,
                                            status="ERROR", error_code=err[:120])
            # ONE final session-owned OCR decision (SHADOW: legacy stays final)
            if write_final and (is_owned is None or is_owned(session_id)):
                odb.set_final_ocr(conn, session_id=str(session_id),
                                  text=result.final_text, engine=result.final_engine,
                                  confidence=result.final_confidence,
                                  decision_reason=result.decision_reason,
                                  model_version="1.2.1", disagreement=result.disagreement)
        finally:
            try: conn.close()
            except Exception: pass

        # dispatch collector (non-blocking)
        if self.collector is not None and getattr(self.cfg, "collection_enabled", True):
            self.collector.submit(self._collector_job(session_id, trusted_vin, normalized_line,
                                                      frame, result))
        return {"ok": True, "decision_reason": result.decision_reason,
                "final_text": result.final_text, "final_engine": result.final_engine,
                "disagreement": result.disagreement,
                "engines": list(result.engine_results.keys()),
                "timeouts": result.timeouts, "failures": list(result.failures)}

    def _collector_job(self, session_id, trusted_vin, normalized_line, frame, result) -> dict:
        eng = result.engine_results.get(ROLE_ENGRAVED)
        praw = result.engine_results.get(ROLE_PADDLE_RAW)
        penh = result.engine_results.get(ROLE_PADDLE_ENHANCED)
        per_char = []
        raw_seq = eng.raw_sequence if eng else ""
        gated = eng.normalized_sequence if eng else ""
        preds = (eng.char_predictions or []) if eng else []
        for cp in preds:
            reasons = []
            gch = gated[cp.position] if cp.position < len(gated) else UNKNOWN_CHAR
            conf = cp.confidence
            top2 = cp.alternatives[1][1] if len(cp.alternatives) > 1 else 0.0
            if (not cp.char) or gch == UNKNOWN_CHAR:
                reasons.append("UNKNOWN" if getattr(self.cfg, "collection_on_unknown", True) else "")
            if conf < 0.85 and getattr(self.cfg, "collection_on_low_confidence", True):
                reasons.append("LOW_CONFIDENCE")
            if (conf - top2) < 0.10:
                reasons.append("LOW_MARGIN")
            if cp.char in set(getattr(self.cfg, "weak_classes", "").replace(" ", "").split(",")):
                reasons.append("WEAK_CLASS")
            per_char.append({"position": cp.position, "char": cp.char, "conf": conf,
                             "reasons": [r for r in reasons if r]})
        if result.disagreement and getattr(self.cfg, "collection_on_disagreement", True):
            for pc in per_char: pc["reasons"] = list(set(pc["reasons"]) | {"ENGINE_DISAGREEMENT"})
        def seq(r): return (r.normalized_sequence or r.raw_sequence) if r else ""
        return {"session_id": str(session_id),
                "expected_vin": trusted_vin, "trusted_label": bool(trusted_vin),
                "normalized_line": normalized_line, "frame": frame, "per_char": per_char,
                "paddle_raw": list(seq(praw)), "paddle_enhanced": list(seq(penh))}


def ocr_v121_health(cfg, engines: Dict[str, C.OCRRecognizer], conn_factory: Callable,
                    collector=None, stage: Optional[ShadowOcrStage] = None) -> dict:
    eng_health = {}
    for role, e in (engines or {}).items():
        try:
            h = e.health(); m = e.metadata()
            eng_health[role] = {"ready": h.ready, "status": h.status,
                                "model_version": m.engine_model_version, "charset_id": m.charset_id,
                                "last_error": h.last_error}
        except Exception as exc:
            eng_health[role] = {"ready": False, "status": "error", "error": str(exc)}
    mig = {}
    try:
        c = conn_factory(); mig = odb.migration_state(c); c.close()
    except Exception as exc:
        mig = {"error": str(exc)}
    return {
        "release_mode": getattr(cfg, "release_mode", "SHADOW"),
        "final_engine_policy": ("legacy_paddle" if getattr(cfg, "release_mode", "SHADOW") == "SHADOW"
                                else "engraved_guarded"),
        "engines": eng_health,
        "engraved_charset": "0123456789ABCDEFHJNST", "output_dimension": 22,
        "unsupported_classes": ["G", "V"],
        "weak_classes": getattr(cfg, "weak_classes", ""),
        "weak_classes_enabled": getattr(cfg, "weak_classes_enabled", False),
        "collector_enabled": getattr(cfg, "collection_enabled", True),
        "collection_root": getattr(cfg, "collection_root", ""),
        "collector_collected": getattr(collector, "collected", 0) if collector else 0,
        "collector_errors": getattr(collector, "errors", 0) if collector else 0,
        "migration_state": mig,
        "last_engine_latency_ms": getattr(stage, "last_latency_ms", {}) if stage else {},
        "timeout_count": getattr(stage, "timeout_count", 0) if stage else 0,
        "engine_error_count": getattr(stage, "engine_error_count", 0) if stage else 0,
    }
