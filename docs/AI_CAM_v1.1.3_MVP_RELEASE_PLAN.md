# AI_CAM v1.1.3 — MVP Release Plan

**Holat:** integratsiyalangan kod (MVP hardening yadrosi + production-hardening OCR modullari)
**Yakuniy qaror:** `READY FOR CONTROLLED HIL TEST` — real liniya sinovisiz `PRODUCTION GO` berilmaydi.
**Branch:** `production-hardening` · **Version:** `v1.1.3`

---

## 1. Ushbu relizda nima bor

### MVP hardening yadrosi (182 test, tasdiqlangan)
- **D2222/D2223 dual-signal** grace-based session close (exit → GRACE_WAIT → deterministik finalize).
- **Session ownership** — har OCR/RFID natijasi `session_id`/`frame_id` bilan; late-result rejection.
- **Real OCR process-worker** — PaddleOCR alohida processda; soft/hard timeout; terminate+respawn.
- **DB traceability** — `session_id UNIQUE` + d2222/d2223 timestamp + latency ustunlar; DATABASE_FAILED guard.
- **Security** — production default-credential blocker, rate-limit, secret masking, path-traversal himoya, cache-bust.
- **Stress/chaos** — Tier 1-8 (burst 850, soak 1000, PLC/kamera/RFID/DB chaos), §16 invariantlar.
- **UI** — kompakt/responsive live video, current vs last-completed session, /api/metrics.

### Integratsiya qilingan OCR R&D (production-hardening)
- `vin_fusion.py`, `pos5_verifier.py`, `vin_slot_recognizer.py`, `ocr_variants.py` (VIN aniqlik modullari — **hozircha standalone**, hardened pipeline'ga wire qilinmagan).
- Training/benchmark tools, OCR migration/pipeline docs, animatsiyalar.

---

## 2. Reliz OLDIDAN majburiy ishlar (integration follow-up)

| # | Ish | Nega | Egasi |
|---|---|---|---|
| I1 | OCR modullarini (vin_fusion va h.k.) hardened `ocr_worker.compute_result` ga wire qilish | Hozir standalone — VIN aniqlik yaxshilanishi ishlatilmayapti | Dev |
| I2 | Sizning testlaringizni (test_vin_fusion, test_ocr_regression, test_production_fixes, test_ocr_process_sim, test_log_scenarios) yangi API'ga moslashtirish | Hozir import/API xatosi (merge divergensiyasi) | Dev |
| I3 | `docs/MERGE_REVIEW_production_hardening_triggering.md` ni ko'rib chiqib, kerakli "triggering" o'zgarishlarni hardened kodga qayta qo'llash | d1d6d5c4 shared-fayl o'zgarishlari auto-merge qilinmadi | Dev |
| I4 | To'liq 182 + integratsiya testlari yashil bo'lishini tasdiqlash | Reliz sifat darvozasi | Dev |

---

## 3. Reliz OLDIDAN config faollashtirish (nazorat ostida)

Standart holatda yangi xatti-harakat **O'CHIQ** (backward-compat). HIL uchun:

| Config | Standart | HIL uchun | Shart |
|---|---|---|---|
| `auth.username/password` | admin/admin | **real parol** | Aks holda production startup RAD etadi |
| DB sxema | eski | migration | `tools/migrate_session_id.py` — avval BACKUP nusxada |
| `plc.exit_address` | null | D2223 (tasdiqlangach) | Controls muhandisi tasdiqlashi SHART |
| `session.exit_signal_enabled` | false | true (D2223 bilan) | Grace mashinasini yoqadi |
| `ocr.process_worker_enabled` | false | true | Real OCR process-worker |
| `plc.write_enabled` | false | **false saqlanadi** | HIL-1 (DONE/ACK) tasdiqlanmaguncha |

---

## 4. Bosqichli HIL reja (`docs/HIL_PRODUCTION_VALIDATION_PLAN.md` — 16 test)

**Bosqich A (eng xavfsiz):** joriy single-signal (D521) + thread OCR — tasdiqlanmagan hardware farazsiz. Kamera/RFID/OCR-latency o'lchash.

**Bosqich B:** D2223 tasdiqlangach — dual-signal grace + process-worker (HIL-1..5, HIL-12). Grace qiymati real D2222→D2223 intervaldan kalibrlanadi.

---

## 5. Go / No-Go darvozalari

```
[ ] I1-I4 integration follow-up bajarildi
[ ] 182 + integratsiya testlari yashil
[ ] auth real parolga o'zgartirildi
[ ] DB migration backup nusxada sinaldi
[ ] Bosqich A HIL o'tdi (kamera/RFID/OCR real)
[ ] D2223 registri controls muhandisi bilan tasdiqlandi
[ ] Bosqich B HIL o'tdi (grace/handshake real)
```
Barchasi ✅ bo'lmaguncha: **NO-GO** production uchun. Hozirgi holat: **READY FOR CONTROLLED HIL TEST**.

---

## 6. Rollback

- Kod: `git checkout <oldingi-tag/commit>` (reliz commit bekor qilinadi).
- DB: migration BACKUP nusxadan tiklanadi (`data/ai_cam.db.bak-*`).
- Config: yangi kalitlarni standart (false/null) ga qaytarish → eski single-signal xatti-harakat.
- Xavfsizlik zaxirasi: `scratchpad/SAFETY_BACKUP_*` (bu sessiya) — to'liq source snapshot.

---

## 7. Repository holati

```
Branch:  production-hardening
Version: v1.1.3
Yadro:   MVP hardening (182 test)
Qo'shildi: production-hardening OCR modullari (standalone)
Korrupsiya: TUZATILDI (ref d91ddf0 -> sog'lom; dangling commit d1d6d5c4 saqlangan)
Production hardware: ULANMAGAN · Production DB: O'ZGARTIRILMAGAN
```
