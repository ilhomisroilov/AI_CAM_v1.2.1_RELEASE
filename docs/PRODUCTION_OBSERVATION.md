# Production Observation (v1.2.1 stabilization)

24-hour observation of the **real** running system (no mock). Aggregates the
artifacts AI_CAM already writes into a fixed report set.

## Run
```bash
python tools/observe_production.py --duration-hours 24   # attach to a running app
python tools/observe_production.py --duration-hours 24 --launch   # also start run.py
python run.py --observe-hours 24                          # integrated with the server
python tools/observe_production.py --report-only          # generate now from current artifacts
```

## Inputs
- `runtime/audit/plc_edges_*.csv` — rising/falling edges, decisions, intervals.
- `runtime/audit/suppressed_triggers_*.jsonl` — duplicate/early suppressions.
- `runtime/data/ai_cam.db` — `vin_records` (records, statuses, duplicate VINs,
  missing evidence) and `ocr_engine_results` (latency, U/V & F/E conflicts).
- `runtime/engraved_ocr_collection/**/metadata.json` — dataset samples,
  training-eligible, equal-split counts.

## Outputs (`reports/`)
`PRODUCTION_24H_AUDIT.md`, `PRODUCTION_24H_METRICS.json`,
`PRODUCTION_24H_SESSIONS.csv`, `PRODUCTION_24H_PLC_EDGES.csv`,
`PRODUCTION_24H_OCR_CONFLICTS.csv`.

## Honesty gate
If the window does not fully elapse, the audit is written as
**`PRODUCTION_24H_AUDIT: NOT COMPLETED`**. Do **not** declare final `v1.2.1 Production Stabilization` until a
real 24h run on the Ubuntu GPU host completes with duplicate records = 0, queued
triggers = 0, missing evidence (decoded>0) = 0, and equal-split training samples = 0.
