# AI_CAM — Production Recovery Implementation Report

**Branch:** `fix/production-ocr-recovery` · **Baseline:** `c3902ea` (master) · **Final:** `b92324f`

---

## 1. FINAL DECISION

> # PARTIALLY REMEDIATED → READY FOR CONTROLLED HIL TEST (session/capture scope only)
>
> The **session/capture lifecycle incident is fixed, tested and measured**. The
> **OCR accuracy/gate-calibration workstream (Phase 5) was NOT implemented** — it
> requires a human-verified ground-truth corpus that does not exist yet (see §6).
>
> HIL has not been run. **No PRODUCTION GO.**

---

## 2. Root cause fixed

**Capture ownership was conflated with session lifetime.**

`_handle_trigger` already stopped processing and paused the camera at plate exit —
capture was genuinely finished — but `_active_session_id` stayed set, and the queued
trigger was started only at the *end* of `_finalize_session()`. With
`hold_until_deadline=true` + `require_rfid=true`, `_maybe_complete_session()` refuses
to close early, so finalization waited for the 30 s HARD_DEADLINE.

Result: a vehicle whose plate left the frame at ~5 s blocked the next capture for ~25 s.
The next vehicle's capture window was gone before its session ever started.

**Now:** capture ownership is a first-class concept (`capture_owner_id`), released at
plate exit / D2223, immediately handing the camera to the next queued trigger — while
the outgoing session keeps draining its own OCR/RFID results and finalizes on its own
schedule.

---

## 3. Measured results

| Metric | Before | After | Target |
|---|---|---|---|
| **Plate exit → next capture start** | **20–22 s** | **P50 11.07 ms / P95 12.61 ms / max 15.98 ms** | ≤ 500 ms ✅ |
| **Idle trigger → capture start** | — | **P50 7.76 ms / P95 8.85 ms / max 9.58 ms** | ≤ 500 ms ✅ |

(n = 200 cycles each, simulator, `SIMULATED`. Real HIL figures still required.)

**Stress:** 500 production cycles (normal / overlapping D2222 / short takt / late OCR /
camera-no-frame / no-detection), invariants all hold, **3/3 identical runs**.

**Suite:** **281 passed** on **3 consecutive identical runs**. Production DB md5
`a733ba…` unchanged throughout.

---

## 4. Commits

| Commit | Content |
|---|---|
| `ac653cd` | `test:` reproduce queued capture delay and session overlap (5 failing tests) |
| `75606a4` | `fix:` decouple capture ownership from session finalization |
| `7fe19ab` | `fix:` reject stale RFID events across session boundaries |
| `0ead75f` | `feat:` per-session camera vision and OCR stage telemetry |
| `b92324f` | `test:` full production-cycle stress + ownership invariants |

### What each change does

**Capture decoupling** — new `_capture_owner_id` / `capture_owner_id`;
`_release_capture()` frees the camera and starts the next queued trigger *without*
closing the session; `plc_on()` queues on capture ownership rather than session
liveness; `_finalize_session()` keeps a fallback release for sessions that never
reached plate exit. A session that hands over capture moves `ACTIVE → WAITING_RESULTS`;
`EXIT_SEEN`/`GRACE_WAIT` keep their own semantics.
`_validate_result_session` accepts results for a draining session so the decoupling
does not silently drop them — ownership still enforced by `session_id`, terminal
sessions still reject.

**MISSED_CAPTURE_WINDOW** — new terminal state + `session.max_capture_delay_ms`
(default 4000, **config-driven, not hardcoded**). A trigger that waited too long would
open the camera on the *wrong* vehicle; it is now closed explicitly with a DB row.
A wrong VIN is more dangerous than NO_READ.

**Stale RFID rejection** — an RFID event is accepted only if the reader *first saw* the
tag inside that session's read window (`rfid.stale_event_tolerance_ms`, default 1500,
0 disables). Records `rfid_reader_event_at`, `rfid_read_started_at`,
`rfid_rejection_reason=STALE_RFID_EVENT`. It deliberately does **not** dedup on EPC
value — the same EPC may legitimately recur; what protects traceability is requiring
*fresh evidence per session*.

**Pre-OCR telemetry** — 13 per-session counters + 8 timestamps, attributed to the
**capture owner** so every frame is billed to the session that owned the camera.
`_classify_failure_stage()` returns one explicit stage:
`NO_PAYLOAD / DECODE_FAILED / NO_DETECTION / NO_CROP / CROP_REJECTED /
OCR_NOT_SUBMITTED / OCR_NOT_STARTED / OCR_TIMEOUT / OCR_AMBIGUOUS / OCR_NO_READ /
MISSED_CAPTURE_WINDOW / DATABASE_FAILURE`.
This makes the distinction the incident hinged on **measurable**: *"OCR ran but did not
read"* vs *"OCR never ran"*. Crops available with zero OCR jobs submitted is logged
CRITICAL as `CROP_AVAILABLE_BUT_OCR_NOT_SUBMITTED`. One `[SESSION SUMMARY]` line per
session — no per-frame spam.

**Operator health** (`status()["health"]`) — `camera_connected`,
`camera_payload_flowing`, `last_frame_age_ms`, `yolo_ready`, `last_detection_age_ms`,
`ocr_ready`, `ocr_queue_depth`, `ocr_last_success_at`, `active_capture_session`,
`open_result_sessions`, `trigger_queue_depth`, `dropped_trigger_count`,
`last_failure_stage`.

---

## 5. A real bug found by the stress test

`_prune_session_history_locked` protected only `_active_session_id`. After the
decoupling, a session that had handed over capture but was still draining
(`WAITING_RESULTS`/`GRACE_WAIT`) could be evicted from `_sessions` once history exceeded
200 entries — its OCR/RFID result would then be rejected as an *unknown session*,
silently losing the read. **Non-terminal sessions are now never pruned.**

This is exactly the class of defect the decoupling could have introduced, and it was
caught before it shipped.

---

## 6. NOT implemented — stated explicitly

| Scope item | Status | Why |
|---|---|---|
| **Phase 5: OCR incident replay corpus** (`incident_replay/`, `labels.csv`) | **NOT DONE** | Requires human-verified ground truth. A prior audit proved crop filenames are **self-labels** (`ocr_worker.py:1034` writes the pipeline's own *rejected* prediction; `pipeline.py:1679` writes its *accepted* one) — using them is circular. Only 99 human-verified rows exist (`labels_review.csv`). Building this corpus is a **human labelling task**, not a code task. |
| **OCR gate calibration** (ROC / precision-recall, margin thresholds) | **NOT DONE** | Depends on the corpus above. Changing `pos5` / `risky_margin` / `variable_margin` / `serial_margin` / `exact_conflict` thresholds without labelled precision-recall data would be exactly the "lower the threshold and hope" move the brief forbids. |
| **Removing non-contributing OCR variants** | **NOT DONE** | Prior measurement (N=40) showed 2 vs 3 vs 9 variants pick the **same VIN 18/18/18**, so the fan-out looks removable — but it must be validated against the labelled corpus before changing accuracy-affecting behaviour. |
| **Physical image-quality gate** (glare/exposure metrics per crop) | **NOT DONE** | Measurement-only work that belongs with the corpus. Prior audit already indicates **glare accounts for 100 % of NO_READs**. |
| **HIL Stages 1–4** | **NOT DONE** | Requires real hardware. |

**The acceptance gate was deliberately left untouched** — per the brief, a false VIN is
more dangerous than NO_READ, and prior measurement shows v1.1.3's gate already yields
*more correct* VINs than v1.1.2 while admitting 18× fewer wrong ones.

---

## 7. Mandatory numbers

```
BASELINE HEAD:                       c3902ea (master)
FINAL HEAD:                          b92324f (fix/production-ocr-recovery)
COMMITS:                             5
WORKING TREE CLEAN:                  YES
PRODUCTION DB MODIFIED:              NO (md5 a733ba357b65375bf748c67428189cef unchanged)

TRIGGER_TO_CAPTURE_P50:              7.76 ms      [SIMULATED, n=200]
TRIGGER_TO_CAPTURE_P95:              8.85 ms      [SIMULATED]
PLATE_EXIT_TO_NEXT_CAPTURE_P50:      11.07 ms     [SIMULATED, n=200]
PLATE_EXIT_TO_NEXT_CAPTURE_P95:      12.61 ms     [SIMULATED]   (was 20-22 s)

STRESS CYCLES:                       500 (x3 identical runs)
SILENT TRIGGER LOSSES:               0
WRONG SESSION RESULTS:               0
WRONG VIN/RFID BINDS:                0
DUPLICATE DB ROWS:                   0
UNCLASSIFIED FAILURES:               0

TESTS ADDED:                         24  (5 capture-window, 4 RFID staleness,
                                          10 telemetry, 1 stress, +4 covered)
FULL SUITE:                          281 passed x 3 consecutive identical runs
                                     (real_ocr deselected: needs GPU/model load)

REPLAY IMAGES / HUMAN VERIFIED:      NOT COLLECTED (Phase 5 not implemented)
BASELINE/FIXED OCR ACCURACY:         NOT MEASURED in this workstream

HIL STATIC / MOVING / OVERLAP / ENDURANCE CYCLES:  0 / 0 / 0 / 0

PRIMARY ROOT CAUSE FIXED:            capture ownership coupled to session finalization
SECONDARY FIXED:                     stale cross-session RFID binding;
                                     non-terminal session pruning;
                                     missing pre-OCR failure-stage observability
PHYSICAL CAMERA ACTIONS:             glare/lighting remains the leading suspected cause
                                     of OCR NO_READs (prior audit) - unaddressed here
REMAINING RISKS:                     OCR accuracy workstream unvalidated (no corpus);
                                     HIL unrun; max_capture_delay_ms default (4000 ms)
                                     must be tuned to real takt + D2222 sensor distance
FINAL DECISION:                      PARTIALLY REMEDIATED -> READY FOR CONTROLLED HIL TEST
                                     (session/capture scope)
```

---

## 8. Before HIL — required tuning

`session.max_capture_delay_ms` currently defaults to **4000 ms**. This value must be set
from the real production takt and the physical D2222-sensor-to-camera distance. Too low
→ valid vehicles closed as `MISSED_CAPTURE_WINDOW`; too high → the original defect
returns in a milder form. It is config-driven precisely so this is an operator decision,
not a code change.

Suggested HIL order: Stage 1 static plate (20 VINs) → Stage 2 moving body (30 cycles,
record `trigger_to_capture_ms` from `[SESSION SUMMARY]`) → Stage 3 overlapping triggers
(20 cycles, verify zero wrong VIN/RFID binds and that any `MISSED_CAPTURE_WINDOW` is
explicit) → Stage 4 endurance (100 consecutive cycles).
