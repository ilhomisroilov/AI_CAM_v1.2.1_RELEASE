# OCR v1.2.1 Architecture

Session-owned normalized OCR input → three engines run **concurrently** → evidence stored →
one final session-owned decision → non-blocking collector.

```
Pipeline._on_ocr_result (session-owned, after legacy result)
  └─ ocr_shadow_hook.dispatch_shadow_ocr   (flag-guarded, fully isolated)
       └─ ShadowOcrStage.process
            ├─ OcrOrchestrator.run  (ThreadPoolExecutor, per-engine timeout + isolation)
            │    ├─ ENGRAVED_V121   (engraved_v121.py: ONNX + adaptive seg + UNKNOWN gate)
            │    ├─ PADDLE_RAW      (paddle_profiles.py; live hook reuses legacy result)
            │    └─ PADDLE_ENHANCED (paddle_profiles.py; contrast profile)
            ├─ ocr_v121_db.store_engine_result  × N  (evidence rows)
            ├─ ocr_v121_db.set_final_ocr        (ONE final; SHADOW = legacy)
            └─ ActiveLearningCollector.submit   (non-blocking cropchar collection)
```

- **Engine contract:** all engines implement `backend.ai.ocr_contract.OCRRecognizer`
  (initialize/recognize/health/metadata/close); results carry session_id/capture_generation/
  job_id end-to-end (ownership never lost).
- **Decision policy** (`ocr_orchestrator._decide`): SHADOW → legacy stays final (SHADOW_ONLY).
  GUARDED_PRIMARY → engraved accepted only if complete + no UNKNOWN + thresholds/margins +
  weak-class policy pass, else documented Paddle fallback (never a hand-built hybrid string).
- **Isolation:** one engine's failure/timeout never cancels the others; collector/DB failures
  never break the session; SHADOW never alters VIN/RFID binding or capture ownership.
- **Charset:** 21 known + REJECT = 22 outputs; G/V not emittable.

See `ocr_orchestrator.py`, `ocr_shadow_stage.py`, `ocr_shadow_hook.py`, `engines/engraved_v121.py`,
`engines/paddle_profiles.py`, `ocr_collector.py`, `database/ocr_v121_db.py`.
