# AI_CAM — Final Finding Closure Matrix

Auditda aniqlangan 28 birlashtirilgan finding (P0-P3) ning yakuniy holati.
Statuslar: `CLOSED` (hardware talab qilmaydi + avtomatik test bilan isbotlangan) ·
`CODE REMEDIATED — HIL VERIFICATION PENDING` (kod tuzatilgan, real qurilma tasdig'i
kerak) · `PARTIALLY CLOSED` · `NOT FIXED` · `REGRESSION INTRODUCED` · `NOT APPLICABLE`.

Baseline: `628a804` · Final: (freeze HEAD) · 182 test PASS / 0 FAIL.

## P0 — Critical

| # | Finding | Code change | Test evidence | Status | HIL |
|---|---|---|---|---|---|
| P0-1 | Faol sessiya paytida 2-PLC trigger jim yo'qoladi | trigger FIFO queue + overflow alarm | test_session_queue, test_stress_invariants (silent_drop=0) | **CLOSED** | — |
| P0-2 | Async OCR/RFID natijasi session-ID'siz | session ownership + `_validate_result_session` | test_session_ownership, test_late_results, test_stress_invariants (mismatch=0) | **CLOSED** | — |
| P0-3 | Real PLC'ga yozuv/DONE handshake yo'q | handshake state-machine + `write_bit` (write_enabled gate) | test_plc_handshake (11) | **CODE REMEDIATED — HIL PENDING** | HIL-1,3,4 |
| P0-4 | Kamera stream o'limi sezilmaydi | CameraState watchdog + bounded reconnect; `connected` faqat fresh-frame | test_camera_watchdog, test_camera_reconnect, test_camera_chaos | **CODE REMEDIATED — HIL PENDING** | HIL-6,7 |

## P1 — High

| # | Finding | Code change | Test evidence | Status | HIL |
|---|---|---|---|---|---|
| P1-1 | DB'da session_id / UNIQUE yo'q | schema + `UNIQUE(session_id)` + traceability ustunlar | test_database_migration, test_db_schema_traceability | **CLOSED** | — |
| P1-2 | Server restart faol sessiyani yo'qotadi | PENDING insert + `recover_incomplete_sessions` → FAILED | test_session_idempotency | **CLOSED** | — |
| P1-3 | Global VIN dedup yangi kuzovni yo'qotadi | dedup `(session_id, vin)` scoped | test_ocr_contract | **CLOSED** | — |
| P1-4 | RFID busy-lock jim yo'qoladi | caller `False` tekshiradi → `failure_reason=RFID_BUSY` | test_session_ownership | **CLOSED** | — |
| P1-5 | Operator UI stale VIN | current-session vs last-completed ajratildi | test_operator_session_status, test_observability_grace | **CLOSED** | — |
| P1-6 | admin/admin + brute-force yo'q | production startup blocker + rate-limit/lockout | test_auth_security | **CLOSED** | — |
| P1-7 | /api/settings plaintext credentials | server-side masking (mask/unmask round-trip) | test_settings_secret_masking | **CLOSED** | — |
| P1-8 | Watchdog tashqi try/except yo'q | `_session_watch` try/except → FAILED finalize | test_session_grace/idempotency | **CLOSED** | — |

## P2 — Medium

| # | Finding | Code change | Test evidence | Status | HIL |
|---|---|---|---|---|---|
| P2-1 | Qisqa PLC impuls poll-aliasing bilan yo'qoladi | latch/seal-in kontrakt + `pulse()` simulyator | test_plc_pulse_latching | **CODE REMEDIATED — HIL PENDING** | HIL-1 |
| P2-2 | MJPEG + YOLO/OCR bitta thread | frame ownership + OCR process-worker (capture/inference ajratildi) | test_frame_ownership, test_ocr_real_integration | **CLOSED** | — |
| P2-3 | /crops path traversal (Windows `\`) | filename allowlist + resolve/relative_to | test_crop_path_security | **CLOSED** | — |
| P2-4 | Kamera paroli logda ochiq | markazlashgan `_SecretSanitizer` filter | test_auth_security (sanitization) | **CLOSED** | — |
| P2-5 | /api/logs/clear audit-wipe | role + CSRF + audit entry | test_log_clear_authz | **CLOSED** | — |
| P2-6 | DB locked → jim SUCCESS/PLC done | DATABASE_FAILED guard (jim SUCCESS yo'q) | test_db_chaos | **CLOSED** | HIL-14 (real yuk) |
| P2-7 | Retention yo'q (disk to'lishi) | configurable retention (dry-run, root-scoped) | test_retention | **CLOSED** | — |
| P2-8 | RFID decimal-validate asimmetriyasi | tanlash + ekstraktsiya izchil | test_rfid_chaos | **CLOSED** | — |
| P2-9 | Bir kadrda 2+ plastinka bitta OCR | ROI/eng-yuqori-conf; single-conveyor dizayn | test_rfid_chaos (RSSI), N/A | **NOT APPLICABLE** (single-body dizayn) | HIL-16 |

## P3 — Low

| # | Finding | Status |
|---|---|---|
| P3-1 | PLC hujjati (B0901) real D521 bilan mos emas | **CLOSED** (bit/mask-aware detection, slice 8) |
| P3-2 | camera.log bo'sh | **CLOSED** (keyword classification) |
| P3-3 | 18 crop-rasm yetishmaydi | **NOT APPLICABLE** (tarixiy ma'lumot; forward-fix orphan yo'q) |
| P3-4 | 121 NULL status | **NOT APPLICABLE** (migratsiya-oldi tarixiy; yangi yozuvlar status bilan) |
| P3-5 | /health yo'q | **CLOSED** (public /health, secret'siz) |
| P3-6 | CUDA=False vs use_gpu=true | **CLOSED** (CPU fallback; real GPU → HIL-11) |
| P3-7 | pytest/test fayllar yo'q + eskirgan komment | **CLOSED** (182 test, pytest, docs) |

## Xulosa

| Status | Soni |
|---|---|
| CLOSED | 22 |
| CODE REMEDIATED — HIL PENDING | 3 (P0-3, P0-4, P2-1) |
| NOT APPLICABLE | 3 (P2-9, P3-3, P3-4) |
| NOT FIXED / REGRESSION | 0 |

**Muhim:** hech qanday hardware-bog'liq finding noto'g'ri `CLOSED` deb belgilanmagan.
P0-3, P0-4, P2-1 real PLC/kamera/ladder tasdig'i kerak → `HIL PENDING`. P2-6 kod
darajasida CLOSED, lekin real yuk ostida DB-lock HIL-14 da qo'shimcha tekshiriladi.
