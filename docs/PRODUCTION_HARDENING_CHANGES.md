# AI_CAM — Production Hardening: Bajarilgan O'zgarishlar

**Branch:** `production-hardening`
**Asos:** `audit/AI_CAM_AUDIT_REPORT.md` (P1–P17) + `audit/AI_CAM_PERFORMANCE_AND_RELEASE_PLAN.md` (PF1–PF8)

Bu hujjat audit hisobotidagi muammolarga kiritilgan kod o'zgarishlarini va ularning
tekshiruv holatini sarhisob qiladi.

---

## 1. Critical / High correctness fixlari (kod bilan hal qilindi)

| # | Muammo | Yechim | Fayllar | Tekshiruv |
|---|--------|--------|---------|-----------|
| **P1** | PLC butun WORD ni `==1` bilan solishtiradi (flap) | `signal_kind`= `bit`/`mask`/`value`; word ichidan bit ajratish | `config.py`, `settings.yaml`, `plc_service.py` | ✅ regress |
| **P5** | Poll davridan qisqa puls o'tkazib yuboriladi | Simulyator rising-edge **latch**; poll 500→100ms | `plc_simulator.py`, `config.py` | ✅ regress |
| **P6** | Auto-off + level-high = re-trigger bo'roni | `_armed` latch + **DONE handshake**; soxta-0 sintez olib tashlandi | `plc_service.py`, `plc_melsec.py`, `config.py` | ✅ regress |
| **P2** | Duplicate VIN yangi sessiyani jim bloklaydi | Dedup **session-aware** (faqat bir sessiya ichida) | `ocr_worker.py` | ✅ regress |
| **P3** | `stop()` ish vaqtida → sentinel keyingi workerни o'ldiradi | **Uzluksiz worker**: `stop()`=pauza, `shutdown()`=to'liq; sentinel yo'q | `ocr_worker.py`, `pipeline.py`, `server.py` | ✅ regress |
| **P4** | Finalize'dan keyin kech natija → qo'sh DB yozuvi + PLC puls | Kech/eski sessiya natijasi qat'iy rad; DB `UNIQUE(session_id)` + `INSERT OR IGNORE` | `pipeline.py`, `db.py` | ✅ regress |
| **P8** | `min_interval` throttle dequeue qilingan ishni tashlaydi | Throttle endi **kutadi (sleep)**, job yo'qolmaydi | `ocr_worker.py` | ✅ regress |
| **P7** | RFID R700 preset 400 (rfMode) | `rfMode` barcha antennaga **yagona qiymat** (allaqachon bor, tasdiqlandi) | `rfid_reader.py` | kod ko'rik |

## 2. Performance (PF) fixlari

| # | Muammo | Yechim | Fayllar |
|---|--------|--------|---------|
| **PF3/PF5** | Sinxron bitta loop + frame-drain yo'q (latency) | **Capture/inference ajratildi**: capture doim eng yangi kadr (drop-old), inference o'z tezligida | `pipeline.py` |
| **PF1/PF2** | YOLO+OCR CPU da | `requirements.txt` da GPU yo'riqnoma + `/health` da device ko'rsatkichi | `requirements.txt`, `server.py` |

## 3. Muhit / observability / xavfsizlik

| # | Muammo | Yechim | Fayllar |
|---|--------|--------|---------|
| **P11** | Config validatsiya yo'q | Tip-coercion + diapazon/enum tekshiruvi (startupда log) | `config.py` |
| **P12** | `requirements.txt` UTF-16 + mavjud bo'lmagan torch pin | UTF-8 ga aylantirildi; `torch==2.4.1`/`torchvision==0.19.1` (haqiqiy) | `requirements.txt` |
| **P14** | Confusion xaritasi to'liq emas | Z↔2, D↔0, 5↔6, 2↔7, 8↔0, B↔3... qo'shildi (18/18 test saqlandi) | `vin_postprocess.py` |
| **P15** | `admin/admin` zaif | Startupда aniq ogohlantirish | `config.py` |
| **P16** | Crop papkasi cheksiz o'sadi | Retention: `CROP_RETENTION_MAX` (5000), eng eskini o'chirish | `config.py`, `pipeline.py` |
| **P17** | `@app.on_event` eskirgan, graceful shutdown yo'q | **lifespan** + `/health` endpoint + graceful drain | `server.py` |
| **P9** | Tashlangan trigger metrikasi yo'q | `dropped_triggers` hisoblagichi `/api/status` da | `pipeline.py` |

---

## 4. Tekshiruv (verification)

`tests/test_production_fixes.py` — yangi regress to'plami. To'liq muhitda (numpy/opencv):

```
python tests/test_production_fixes.py     # P1,P5,P6,P2,P3,P8,P4 — hammasi PASS
python tests/test_vin_rules.py            # 18/18 (P14 yangi confusion xaritasi bilan)
```

Tekshirilgan (haqiqiy kod mantig'iga qarshi bajarildi): **P1, P2, P3, P4, P5, P6, P8** regress
testlari PASS; VIN qoidalari **18/18**; P11 tip-coercion PASS.

---

## 5. Qolgan ish — DEPLOYMENT (apparatura/GPU talab qiladi, kod emas)

Bularni ishlab chiqarish serverida bajaring (audit §12 checklist):

- [ ] **PLC D521 semantikasi**: aniq ARRIVE bitini PLC muhandisi bilan tasdiqlang →
      `settings.yaml: plc.signal_bit`. Handshake uchun `plc.done_address` ni o'rnating.
- [ ] **PLC port** (`5003`/`5004`) va tarmoq yo'lini tasdiqlang.
- [ ] **GPU**: `paddlepaddle-gpu` + CUDA `torch` o'rnating (requirements.txt izohi);
      `/health` da `yolo.device=cuda`, `ocr.gpu=true` ekanini tekshiring.
- [ ] **`admin` parolini** `settings.yaml` da o'zgartiring.
- [ ] **HW-in-loop**: 100+ mashina o'tkazib, DB hisobi == liniya hisobi; missed=0, duplicate=0.
- [ ] `/health` yashil (200) ekanini monitoringga ulang.
