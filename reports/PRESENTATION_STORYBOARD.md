# AI_CAM v1.2.1 Presentation Storyboard

## Story objective

By the end, technical management and engineering should understand how AI_CAM keeps PLC, vision, OCR, RFID, database, and operator evidence attached to one body-cycle—and which live validation steps remain before production confidence can be declared.

## Visual system

- **Background:** graphite to midnight navy.
- **Primary signal:** electric cyan for camera/vision/data paths.
- **Secondary signal:** clear blue for RFID and software boundaries.
- **Accepted state:** bright green.
- **Trigger/readiness:** amber.
- **Failure/blocked:** restrained coral red.
- **Typography:** large white takeaway titles, compact blue-grey technical labels.
- **Depth:** subtle shadows, layered offsets, isometric hero, luminous connector paths.
- **Evidence discipline:** no invented runtime screenshots; schematics are labeled.

## Slide-by-slide storyboard

### 1. AI_CAM v1.2.1 Production Stabilization

- **Job:** establish product, station context, and the one-trigger/one-result idea.
- **Visible copy:** title, industrial AI VIN reading and traceability subtitle.
- **Visual:** full-bleed semi-3D inspection-cell hero with title in negative space.
- **Source:** `README.md`, `VERSION`; generated hero.

### 2. A single body produces multiple identity signals

- **Job:** make the traceability problem concrete.
- **Visible copy:** PLC arrival, engraved VIN, electronic EPC; one body-cycle owns every result.
- **Visual:** three signals feeding one large session contract.
- **Source:** `README.md`, `backend/pipeline.py`, `backend/database/db.py`.

### 3. Every body follows one deterministic evidence path

- **Job:** explain the complete workflow before technical detail.
- **Visible copy:** D2222 → session → parallel camera/RFID → OCR/binding → finalize once.
- **Visual:** editable, directional flow with parallel lanes.
- **Source:** `backend/pipeline.py`, `backend/rfid/rfid_service.py`.

### 4. The PLC pulse is a boundary—not just a signal

- **Job:** explain why body-cycle control matters.
- **Visible copy:** armed, debounce, active, finalize, closed; duplicate/early suppression.
- **Visual:** horizontal state progression plus a suppressed-trigger callout.
- **Source:** `config/settings.yaml`, PLC/pipeline modules.

### 5. Four hardware layers anchor the system in the cell

- **Job:** show verified hardware capabilities without overclaiming installed variants.
- **Visible copy:** Lector 652, MELSEC-Q, R700, NVIDIA target.
- **Visual:** four layered pseudo-3D blocks.
- **Source:** `reports/HARDWARE_CAPABILITY_REPORT.md` and official manufacturer documents.

### 6. Vision starts by finding the right metal region

- **Job:** explain the camera-to-crop path and the strict detector gate.
- **Visible copy:** frame, YOLO, stable evidence, OCR submit, 0.90 gate.
- **Visual:** abstract VIN-region frame plus numbered processing steps.
- **Source:** camera client, detector, pipeline, typed config default.

### 7. Parallel OCR views expose single-model blind spots

- **Job:** clarify model roles and prevent the audience from assuming Engraved is the live primary.
- **Visible copy:** shared crop; Engraved/RAW/Enhanced evidence; production Paddle authority.
- **Visual:** one central crop with three engine branches.
- **Source:** OCR worker, shadow hook/stage, orchestrator.

### 8. The production VIN is a guarded Paddle fusion result

- **Job:** show how the active decision is made.
- **Visible copy:** read, fuse, apply rules, gate, lock; YAML/hook mismatch.
- **Visual:** five-step flow plus amber reconciliation strip.
- **Source:** `ocr_worker.py`, `vin_fusion.py`, `vin_rules.py`, `ocr_shadow_hook.py`.

### 9. Weak characters expose the next validation gap

- **Job:** focus attention on F/E, U/V, and numeric confusions without overclaiming live guards.
- **Visible copy:** implemented helper condition; tested but not wired to acceptance.
- **Visual:** schematic glyph strip and three evidence rules.
- **Source:** `ocr_disagreement.py`, `AI_MODEL_ANALYSIS.md`.

### 10. Every hard case can improve the next model

- **Job:** explain the review-candidate loop and alignment safeguards.
- **Visible copy:** ambiguous → capture → align → review → train; equal split pending review.
- **Visual:** five-stage learning loop and warning callout.
- **Source:** OCR collector, dataset collector, dataset-alignment documentation.

### 11. Session ownership keeps parallel results attached

- **Job:** explain software architecture.
- **Visible copy:** device services → pipeline → AI/DB/logs → FastAPI.
- **Visual:** central session-owner block with routed connectors.
- **Source:** pipeline, server, device and database modules.

### 12. One session becomes one auditable database row

- **Job:** make idempotency and evidence tables understandable.
- **Visible copy:** pending insert, unique session, same-row finalization, engine/collection tables.
- **Visual:** central database cylinder with related evidence surfaces.
- **Source:** database modules and migration.

### 13. Operators see live evidence and history

- **Job:** show what a production/operator user sees.
- **Visible copy:** dashboard, history, logs, system, settings.
- **Visual:** clearly labeled UI schematic based on actual templates.
- **Source:** frontend templates/JS and FastAPI endpoints.

### 14. Stabilization closes the failure loops

- **Job:** summarize reliability controls.
- **Visible copy:** trigger, capture, detection, OCR, failure, observation.
- **Visual:** six technical rails with a green evidence invariant.
- **Source:** changelog, failure-evidence and observation docs.

### 15. Devices, AI, data, and operations stay separated

- **Job:** orient developers in the repository.
- **Visible copy:** device/AI/database/server/frontend directories around pipeline.
- **Visual:** codebase responsibility map.
- **Source:** source tree and system-discovery report.

### 16. Current strengths are clear; remaining gates are operational

- **Job:** provide a balanced engineering assessment.
- **Visible copy:** six strengths and six open gates.
- **Visual:** two-column green/amber comparison.
- **Source:** architecture, AI, hardware, and release reports.

### 17. Commissioning evidence determines readiness

- **Job:** convert unknowns into an actionable acceptance plan.
- **Visible copy:** PLC, camera, RFID, GPU, 24-hour checkpoints.
- **Visual:** five checkpoints ending in documentation-ready/live-pending status.
- **Source:** HIL plan, hardware checklist, observation documentation.

### 18. One trigger. One body. One traceable result.

- **Job:** resolve the opening and leave a clear next step.
- **Visible copy:** synthesis plus site evidence/OCR authority/24-hour actions.
- **Visual:** large closing statement and a four-node PLC–vision–RFID–record system.
- **Source:** executive synthesis of the package.

## Review checklist

- Every slide advances one claim.
- All external hardware claims have official sources in speaker notes.
- Every slide has a `[Sources]` block.
- No slide claims that the three-engine stage currently owns the session VIN.
- No slide claims that fixed-position rule output is literally never substituted.
- UI and VIN examples are labeled as schematics.
- Installed hardware variants and live validation remain explicitly unresolved.

