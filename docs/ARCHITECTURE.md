# AI_CAM Architecture (v1.3.0)

Industrial engraved-VIN OCR for Station 509. One PLC trigger (**D2222**) starts one
body-cycle that produces exactly **one** final database record.

## System

```mermaid
flowchart LR
  PLC[PLC Mitsubishi Q\nD2222 @ 10.123.40.99:5003] -->|debounced rising edge| SVC[PLC service\n+ edge audit]
  SVC --> PIPE[Pipeline\nbody-cycle state machine]
  CAM[SICK camera\n10.123.86.42 :2111/:2113] --> PIPE
  RFID[Impinj R700\n10.123.18.3:80] --> PIPE
  PIPE --> YOLO[YOLO best.pt\ncuda:0]
  YOLO --> OCR[3-engine OCR\nENGRAVED_V121 / PADDLE_RAW / PADDLE_ENHANCED]
  OCR --> DB[(runtime/data/ai_cam.db)]
  PIPE --> DB
  PIPE --> COLL[dataset collector\nruntime/engraved_ocr_collection]
  DB --> API[FastAPI + dashboard/history]
  SVC --> AUD[runtime/audit/*.csv/jsonl]
```

## Body-cycle state machine (D2222 only; no D2223)

```mermaid
stateDiagram-v2
  [*] --> IDLE
  IDLE --> BODY_ACTIVE: stable D2222 rising edge (debounced)
  BODY_ACTIVE --> BODY_ACTIVE: new trigger -> DUPLICATE_TRIGGER_IGNORED
  BODY_ACTIVE --> OCR_ACTIVE: YOLO>=0.90, 2 stable frames
  OCR_ACTIVE --> VIN_LOCKED: VIN accepted (retries cancelled)
  OCR_ACTIVE --> FINALIZING: 30s deadline
  VIN_LOCKED --> FINALIZING
  FINALIZING --> FINALIZED: exactly ONE DB record
  FINALIZED --> IDLE: >= minimum_body_interval (90s)
  IDLE --> IDLE: early rising edge -> SUSPICIOUS_EARLY_TRIGGER
```

- Real bodies are ≥90 s apart. A trigger during an active cycle, or sooner than
  `minimum_body_interval_sec`, is suppressed + audited (no queue in production).
- RFID runs 30 s in parallel, independent of OCR; a missing tag is `NO_TAG`, not a
  false `TIMEOUT`. Camera plate-exit frees camera/YOLO while RFID continues.
- `finalize(session_id)` is idempotent — N parallel calls → 1 record.

## Path & runtime
Everything derives from `PROJECT_ROOT = Path(__file__)...`; models in `models/`,
writable state in `runtime/{data,logs,crops,temp,backups,engraved_ocr_collection,audit}`.
No `/opt`, `/etc`, `/var`, systemd, or service-user dependency. One command:
`python run.py`.

## Release workflow

```mermaid
flowchart LR
  code[code + tests] --> rc[tag v1.3.0-rc1\nbranch release/v1.3.0-reliability]
  rc --> push[push origin + gh prerelease]
  push --> obs[24h production observation\non Ubuntu GPU host]
  obs -->|PASS| final[tag v1.3.0 + final release]
  obs -->|NOT COMPLETED / FAIL| rc
```

See `PLC_AND_CONFIG.md`, `OCR_FUSION.md`, `DATASET_CHARACTER_ALIGNMENT.md`,
`FAILURE_EVIDENCE.md` (below), and `PRODUCTION_OBSERVATION.md`.
