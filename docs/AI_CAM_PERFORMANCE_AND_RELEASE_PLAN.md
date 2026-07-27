# AI_CAM — Performance (FPS) Tahlili va Production Release Rejasi

**Sana:** 2026-06-25  
**Asos:** `AI_CAM_AUDIT_REPORT.md` (P1-P17) + kamera/frame oqimi (`camera_client.py`, `pipeline._stream_loop`, `detector.py`, `dataset_collector.py`) qo'shimcha tahlili.  
**Eslatma:** Kod hali O'ZGARTIRILMADI. Bu — savolingizga javob + FPS tuzatish rejasi.

---

## 0. Qisqa javob (savollaringizga)

**1. "Shu MD dagi muammolarni yechsak, hammasi hal bo'ladimi?"**  
Yo'q — yetarli emas, lekin zarur. `AI_CAM_AUDIT_REPORT.md` dagi P1-P17 **to'g'rilik/ishonchlilik** (correctness) muammolari: trigger, sessiya, dedup, idempotentlik, RFID. Ular tuzatilsa, tizim **to'g'ri ishlaydi**, lekin **tez** ishlamaydi. Siz aytgan FPS 2→4 (maks 8) — bu **alohida performance workstream** (quyida PF1-PF8). Ikkalasi ham kerak.

**2. "Qachon production release ga tayyor bo'lamiz?"**  
Realistik baho (1 dev, to'liq vaqt):
- **Funksional/ishonchlilik fix (P1-P7 critical/high):** ~2 hafta.
- **Performance fix (PF1-PF5, FPS 25+ gacha):** ~1-1.5 hafta (asosan GPU + arxitektura).
- **Integratsiya + HW-in-loop sinov (haqiqiy PLC/R700/Lector, 100+ mashina):** ~1 hafta.
- **Stabilizatsiya/observability/deployment:** ~0.5-1 hafta.

**Jami: ~4.5-5.5 hafta** to'liq production release uchun. **Pilot/cheklangan ishlab chiqarish** (bitta liniya, nazorat ostida) ~3 haftadan keyin mumkin (P1-P7 + GPU + frame-drain).

---

## 1. FPS Muammosining Root Cause Tahlili

Siz kuzatgan: **FPS odatda 2-4, maksimum 8 gacha chiqadi.** Bu raqamlar kodning aniq xulq-atvoriga mos keladi.

### Frame yo'li (har bir kadr uchun, `pipeline._stream_loop`)

```
read_stream_frame()  → tarmoq + _recv_exactly (BLOB)        ~5-15 ms
decode_bmp()         → cv2.imdecode (JPEG/BMP grayscale)     ~5-15 ms
cvtColor GRAY2BGR    →                                       ~2-5 ms
─────────────────  (agar _processing=True) ─────────────────
detector.detect()    → YOLOv8n .predict (CPU!)              ~110-130 ms   ◄── ASOSIY
draw_boxes()         →                                       ~2 ms
collector.maybe_collect (settings: false)                    ~0 ms
_handle_trigger()    → crop+quality_score (faqat conf≥0.85)  ~1-10 ms
─────────────────────────────────────────────────────────────
cv2.imencode (.jpg, q=80)  → MJPEG buffer                    ~5-10 ms
```

**Hisob:**
- **Faqat live (processing OFF):** ~20-35 ms/kadr → **~30-50 FPS** mumkin.
- **Processing ON, OCR YO'Q:** YOLO CPU ~120 ms hukmron → **~7-8 FPS** ← bu sizning "maksimum 8".
- **Processing ON + OCR ishlayotганда:** PaddleOCR (det+rec, retry+variants) ham **CPU** da, **bir xil protsessda**, bir necha yadroни va Python ish vaqtини egallaydi. YOLO bilan CPU uchun raqobat → YOLO 250-500 ms gacha sekinlashadi → **~2-4 FPS** ← bu sizning "2-4 ga tushib ketishi".

### Asosiy sabablar

| Kod | Nima | FPS ta'siri |
|-----|------|-------------|
| `detector._resolve_device` → log `device=auto -> CPU` | YOLO **CPU** da (GPU yo'q yoki paddle CPU build) | 8 FPS shift (P10) |
| `ocr_worker` PaddleOCR **CPU** build | OCR ham CPU; YOLO bilan yadro raqobati | 8→2-4 tushish |
| `_stream_loop` — **sinxron, bitta loop** | read→YOLO→encode ketma-ket; YOLO sekin bo'lsa hamma narsa kutadi | latency + FPS |
| YOLO **har kadrda** ishlaydi (`_processing` bo'lsa) | live ko'rinish uchun ham to'liq inferens | ortiqcha yuk |
| **Frame-drain yo'q** | 16MB socket buferida kadrlar yig'iladi; eng eski kadr o'qiladi | **latency o'sadi** (eskirgan kadr) |
| `cv2.imencode` har kadrda (q=80) | hech kim ko'rmasa ham MJPEG kodlanadi | kichik, lekin bor |
| `max_ocr_attempts=5` + variants (deskew, ±7°) | bir hodisaga 5 OCR chaqiruvi CPU da | OCR davomida uzoq CPU tutilish |

### Muhim (yashirin) muammo: kechikish (latency), nafaqat FPS

`_stream_loop` kadrlarni **navbat bilan** o'qiydi va eng eskidan boshlaydi. Kamera 25-40 FPS push qiladi, lekin loop 8 FPS qayta ishlaydi → har soniyada ~17-32 kadr 16MB OS buferiga yig'iladi. `read_stream_frame` har doim **eng eski** kadrни qaytaradi → **qayta ishlanayotgan kadr real vaqtdan sekundlar orqada**. Industrial trigger uchun bu xavfli: OCR/crop eskirgan kadrдан olinishi, mashina allaqachon o'tib ketgan bo'lishi mumkin. **"Latest-frame" (eng yangi kadrga sakrash) yo'q.**

---

## 2. "Kameraga buyruq berish" (Camera Command) Arxitektura Muammosi

Hozir tizim **uzluksiz live-push** (`mLIStart 0`) rejimida ishlaydi va YOLO **doimiy oqimni** tahlil qiladi. Bu real-time ko'rinish uchun yaxshi, lekin **PLC-triggerli VIN o'qish** uchun samarasiz:

- Mashina kelganда butun vaqt davomida (90s) YOLO har kadrda ishlaydi → CPU isrofi, FPS tushishi.
- Eng yaxshi kadr — mashina aniq pozitsiyada bo'lganida; uzluksiz oqimда buni topish uchun "confirm/fusion/quality" logikasi qo'shilgan, lekin bu ham CPU yeydi.

**Tavsiya (arxitektura opsiyalari):**
- **A — Hardware/Software trigger single-shot:** PLC=1 da kamerага **bitta** yuqori sifatli kadr buyrug'i (`mTRIGGER`/single image grab) berib, faqat o'sha kadr(lar)ни YOLO+OCR qilish. Uzluksiz YOLO o'rniga trigger-grab → CPU yuki keskin kamayadi, latency aniq.
- **B — Davriy YOLO (stride):** Live ko'rinishni 25 FPS ko'rsatib, YOLO ни har N-kadrда (mas. har 3-kadr) ishlatish. Detect FPS ~ 8/N emas, balki ko'rinish silliq qoladi.
- **C — Detached pipeline:** Kadr o'qish (capture) thread + tahlil (inference) thread ajratilgan; capture har doim **eng yangi** kadrни saqlaydi (eski tashlanadi), inference o'z tezligida ishlaydi → latency past, FPS ko'rinishi yuqori.

Eng yaxshisi: **A + C** kombinatsiyasi (trigger-grab + decoupled latest-frame).

---

## 3. Performance Defektlar Ro'yxati (PF)

| # | Defekt | Severity | Fayl:satr | Yechim yo'nalishi | Kutilgan natija |
|---|--------|----------|-----------|-------------------|-----------------|
| PF1 | YOLO **CPU** da (GPU emas) | **High** | `detector.py:39-62`, log | GPU serverda CUDA torch + `model.to('cuda')` | 8 → 30-60 FPS (faqat YOLO) |
| PF2 | PaddleOCR **CPU** build → YOLO bilan yadro raqobati | **High** | `requirements.txt:17`, `ocr_worker` | `paddlepaddle-gpu`; OCR GPU da | OCR davomida FPS tushmaydi |
| PF3 | **Frame-drain yo'q** — eskirgan kadr o'qiladi (latency o'sadi) | **High** | `pipeline._stream_loop:462-498` | Capture/inference ni ajratish; har doim eng yangi kadr (drop-old) | latency sekundlardan ~1 kadrgача |
| PF4 | YOLO **har kadrда** ishlaydi (live ko'rinish uchun ham) | **Medium** | `pipeline._stream_loop:474-484` | Detection stride (har N-kadr) yoki trigger-grab | CPU yuki ~N marta kamayadi |
| PF5 | Sinxron bitta loop (read→YOLO→encode) | **Medium** | `pipeline._stream_loop` | Capture thread + inference thread (decoupled) | FPS ko'rinishi 25+ barqaror |
| PF6 | OCR `max_ocr_attempts=5` + variants har hodisada | **Medium** | `config.py:136`, `ocr_worker._variants` | Birinchi yuqori-ishonch natijada to'xtash; variantlarni kamaytirish | OCR davomiyligi qisqaradi |
| PF7 | MJPEG har kadrда kodlanadi (tomoshabin bo'lmasa ham) | **Low** | `pipeline._stream_loop:487-493` | Faqat aktiv MJPEG mijoz bo'lsa kodlash; FPS cap | kichik CPU tejash |
| PF8 | `decode_bmp` + `cvtColor` har kadrда (to'liq JPEG decode) | **Low** | `camera_client.decode_bmp`, `_stream_loop:466-471` | YOLO uchun grayscale to'g'ridan ishlatish (BGR shart emas) | kichik tejash |

---

## 4. Performance Fix Rejasi (algoritmik, kodga tegmasdan reja)

**Bosqich P-1 — GPU (eng katta yutuq, eng kam mehnat):**
- GPU serverda: CUDA `torch`/`torchvision`, `paddlepaddle-gpu` o'rnatish; `detection.device=cuda:0`, `ocr.use_gpu=true`.
- Startup `/health` da `yolo_device=cuda`, `paddle_real_device=gpu` ko'rsatish.
- **Kutilgan:** YOLO 8→30-60 FPS; OCR 5-10× tez; OCR davomida FPS tushmaydi.

**Bosqich P-2 — Capture/inference decoupling (latency):**
- Capture thread: faqat `read_stream_frame`+`decode_bmp`, doim **eng yangi** kadrni `latest_frame` ga yozadi (eski tashlanadi).
- Inference thread: `latest_frame` ni oladi (eski kadrlar o'tkazib yuboriladi) → YOLO/trigger.
- MJPEG: `latest_annotated` dan o'qiydi.
- **Kutilgan:** latency ~1 kadr; FPS ko'rinishi to'liq; eskirgan crop yo'qoladi.

**Bosqich P-3 — Trigger-grab opsiyasi (CPU yukini kamaytirish):**
- PLC=1 da: davriy/triggerli kadr olib, faqat detection oynasида YOLO; mashina yo'q paytda YOLO o'chiq (yoki past stride).
- Yoki detection stride (har N-kadr) — live silliq, detect tejamkor.

**Bosqich P-4 — OCR yukini optimallashtirish:**
- Birinchi qabul qilinadigan VIN da to'xtash (allaqachon bor); variant/attempt sonини GPU da kamaytirish shart emas, CPU da kamaytirish.
- OCR ни sessiyalararo to'xtatmaslik (P3 fix bilan birga) — qayta yuklash/sentinel yo'qoladi.

**O'lchov (har bosqichdan keyin):** `/api/logs/stats` → `fps`, `ocr_ms`; yangi metrika: capture FPS vs inference FPS, frame latency (ms), dropped frames.

---

## 5. To'liq Release Yo'l Xaritasi (correctness + performance birga)

| Bosqich | Ish | Muammolar | Taxminiy vaqt |
|---------|-----|-----------|----------------|
| **1. Critical correctness** | PLC word/bit/mask + edge/handshake; OCR worker uzluksiz (sentinel fix); session-aware dedup; late-result idempotentlik | P1, P3, P2, P4, P6(PLC) | **~2 hafta** |
| **2. GPU + performance** | GPU build; capture/inference decouple; frame-drain; detection stride/trigger-grab | PF1-PF5, P10 | **~1-1.5 hafta** (1 bilan qisman parallel) |
| **3. RFID + muhit** | R700 preset rfMode; PLC port/tarmoq; requirements UTF-8; config schema | P7, P11, P12, P13 | **~0.5 hafta** |
| **4. Observability + deploy** | `/health`; structured log; dropped-trigger/missed-pulse/latency metrika; graceful shutdown; DB UNIQUE(session_id) | P4(DB), P9, P17 | **~0.5-1 hafta** |
| **5. HW-in-loop sinov** | Haqiqiy PLC/R700/Lector; 100+ mashina; DB hisobi == liniya hisobi; missed=0, duplicate=0; latency/FPS o'lchov | barchasi | **~1 hafta** |

**Yakuniy: ~4.5-5.5 hafta** to'liq production. **Pilot (nazorat ostida): ~3 hafta** (Bosqich 1 + GPU + frame-drain tugagach).

### Tayyorlik mezonlari (Definition of Done)
- [ ] `tests/test_vin_rules.py` 18/18 + yangi correctness testlar o'tadi.
- [ ] `audit/stress_failuremode.py` da barcha "BUG REPRODUCED" → "ok".
- [ ] GPU: YOLO+OCR `cuda`; FPS ko'rinishi ≥ 20, OCR davomida ham ≥ 15.
- [ ] Frame latency < 150 ms (eng yangi kadr).
- [ ] HW-in-loop: 100+ mashina, missed=0, duplicate=0, har biri DB da bitta to'g'ri qator.
- [ ] `/health` yashil; PLC trigger ishonchli; RFID 200; graceful shutdown.

---

## 6. Prioritet (correctness + performance birlashgan)

1. **P1 PLC trigger** (Critical) + **PF1 GPU** (eng katta FPS yutug'i) — parallel boshlash.
2. **P3 OCR worker uzluksiz** (Critical) — PF2/PF6 OCR optimizatsiyasi bilan bog'liq.
3. **P2 session-aware dedup** (Critical).
4. **PF3 frame-drain + PF5 decouple** (latency, High).
5. **P4 idempotentlik + DB UNIQUE** (High).
6. **P6/P5 PLC handshake + short-pulse latch** (High).
7. **P7 RFID preset** (High).
8. **PF4 detection stride / trigger-grab** (Medium).
9. **P10/P12/P13 muhit; P8/P9/P11/P14; observability** (Medium).
10. **P15/P16/P17 auth, retention, lifespan** (Low/Medium).

---

## 7. Xulosa

- **MD (P1-P17) ni yechish = tizim TO'G'RI ishlaydi**, lekin **tez emas**.
- **FPS 2-4 / maks 8** — asosan **CPU da YOLO+OCR** (GPU yo'q) + **sinxron loop / frame-drain yo'qligi**. Bu alohida performance ish (PF1-PF8).
- Ikkala workstream birga: **~4.5-5.5 hafta** to'liq production, **~3 hafta** nazoratli pilot.
- Eng katta tezkor yutuq: **GPU build** (8→30+ FPS) va **OCR ni GPU ga ko'chirib, FPS tushishini yo'q qilish**.
- Keyingi qadam (ruxsatingiz bilan): shu reja bo'yicha **kodni tuzatishni boshlash** va har fixdан keyin `stress_failuremode.py` + FPS metrika bilan tasdiqlash.

*Kod hali o'zgartirilmadi.*
