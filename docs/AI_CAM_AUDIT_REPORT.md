# AI_CAM — To'liq Audit va Failure-Mode Hisoboti

**Sana:** 2026-06-25  
**Branch:** `master` (origin bilan sinxron, lekin commit qilinmagan ish-daraxti o'zgarishlari bor)  
**Metod:** Kod noldan o'qildi, git diff/log tahlil qilindi, loglar tekshirildi va **haqiqiy kod bilan 7 ta failure-mode simulyatsiyasi ishga tushirildi** (`audit/stress_failuremode.py`).  
**Muhim:** Hech qanday kod o'zgartirilmadi. Bu — diagnostika + yechim rejasi.

---

## 1. Executive Summary

YOLO detekt qismi soz: model `best.pt` yuklanadi, `device=auto` GPU/CPU ni to'g'ri tanlaydi, OCR engine va VIN-aware post-processing (QY/BL7M) ishlaydi (18/18 unit test o'tdi). Oxirgi commitdan keyingi diff aslida oldingi `OCR_TRIGGER_DEBUG_REPORT.md` dagi 5 ta muammoni **to'g'rilagan** (session_id korrelyatsiyasi, `on_fail` retry, `require_known_model`, RFID grace, real-device log). Bu to'g'rilashlar yaxshi.

Ammo noldan audit **yangi va saqlanib qolgan jiddiy nuqsonlarni** ochib berdi. Eng muhimlari (hammasi simulyatsiyada tasdiqlangan):

1. **PLC trigger butun WORD ni `==1` bilan solishtiradi.** Real logда `D521 = 8720` (word). Trigger faqat register aniq `1` bo'lsagina ishlaydi; har qanday word o'zgarishi START/STOP flap qiladi. Bitmask yo'q. → **Critical**.
2. **Duplicate VIN oynasi (90s) yangi sessiyani jimgina bloklaydi.** Bir xil VIN qayta kelsa, OCR `on_result` ham, `on_fail` ham chaqirmaydi → sessiya 90s TIMEOUT gacha osiladi, `NO_READ` yoziladi. duplicate_window (90s) == session timeout (90s). → **Critical**.
3. **`stop()` ish bajarilayotgan paytda chaqirilsa, navbatda "sentinel" qoladi va keyingi worker darhol o'ladi.** OCR `running=True` ko'rinadi, lekin hech narsa qayta ishlamaydi. → **Critical**.
4. **Sessiya yopilgandan keyin kech kelgan OCR natijasi qo'shimcha (legacy) DB yozuvi yaratadi va PLC ni qayta puls qiladi.** Idempotentlik buziladi. → **High**.
5. **PLC poll davridan (500ms) qisqa trigger pulslari butunlay o'tkazib yuboriladi.** → **High**.
6. **Auto-off + level-high signal = re-trigger bo'roni** (kamera connect/disconnect flapping). → **High**.
7. RFID R700 preset `400 Bad Request` (rfMode), PLC `5003` portга ulanish timeout, requirements.txt UTF-16/torch pin — operatsion/muhit muammolari.

**Productionga tayyorlik xulosasi:** Hozircha **TAYYOR EMAS**. PLC trigger semantikasi va duplicate/sentinel/late-result muammolari ishlab chiqarish liniyasida mashinalarни yo'qotishga, soxta TIMEOUT/NO_READ va takroriy yozuvlarga olib keladi.

---

## 2. Hozirgi Runtime Oqim Diagrammasi

```
                         ┌──────────────────────────────────────────────┐
                         │  server.py (FastAPI, startup)                 │
                         │  warmup_models() → YOLO+PaddleOCR xotirada    │
                         │  plc_service.start()  rfid_service.start()    │
                         └───────────────┬──────────────────────────────┘
                                         │
        PLC poll thread (500ms) ─────────┤  read_signal()  [D521 = WORD!]
        plc_service._loop                │
            value != last? ──────────────┤
              value == trigger_on_value(1) ? ── YES ─► pipeline.plc_on()
                                               └─ NO ─► pipeline.plc_off()
                                         │
                      ┌──────────────────▼───────────────────────────────┐
                      │ plc_on(): SESSIYA #N boshlanadi (session_lock)    │
                      │  • _session_active=True, deadline=now+90s         │
                      │  • connect_camera() + start_processing()          │
                      │      └─ ocr.start()  (OCR worker thread)          │
                      │  • rfid_service.trigger_read(...) [parallel thread]│
                      │  • watchdog thread (deadline yoki done gacha)     │
                      └──────────────────┬───────────────────────────────┘
                                         │
   camera stream thread (_stream_loop)   │       rfid-read thread
   ┌─────────────────────────────────┐   │   ┌──────────────────────────┐
   │ read_stream_frame → decode_bmp  │   │   │ _read_until_deadline      │
   │ YOLO detect (if processing)     │   │   │ (6s+retry) → on_rfid_result│
   │ _handle_trigger (fusion/gating) │   │   └───────────┬──────────────┘
   │   conf≥0.85, confirm≥2, quality │   │               │
   │   → ocr.submit_frames(top,sid)  │   │               ▼
   └───────────────┬─────────────────┘   │   _session_rfid = {...}
                   │                      │   _maybe_complete_session()
                   ▼                      │
   OCR worker thread (_loop)             │
   ┌─────────────────────────────────┐   │
   │ queue.get → _process_job        │   │
   │  preprocess→PaddleOCR→fuse      │   │
   │  evaluate_auto (QY/BL7M)        │   │
   │  require_known_model gate       │   │
   │  ANTI-DUPLICATE (90s) ◄── jim drop│  │
   │  → on_result(vin,...,sid)       │   │
   │  → on_fail(sid,raw) (retry)     │   │
   └───────────────┬─────────────────┘   │
                   ▼                      │
   _on_ocr_result: _session_vin={...} ───┘
   _maybe_complete_session()
       VIN + (RFID yoki !require_rfid) → SUCCESS → _finalize_session
       VIN bor, RFID kerak, yo'q → grace(15s) → VIN_OK_RFID_TIMEOUT
       deadline → watchdog → TIMEOUT
                   │
                   ▼
   _finalize_session (idempotent):
     stop_processing() → ocr.stop() + collector.stop()
     db.insert_record(status)   [HAR DOIM 1 marta]
     _session_active=False
     on_vin_done() → plc_service.notify_vin_done() (_force_off / sim=0)
```

**Diagrammadagi zaif nuqtalar** (raqamlar §4 dagi muammolarga mos): `[D521=WORD]` → P1; `ANTI-DUPLICATE jim drop` → P2; `ocr.stop()` → P3; finalize'dan keyin kech `_on_ocr_result` → P4; PLC poll → P5/P6; `notify_vin_done` + level-high → P6/P7.

---

## 3. Topilgan Xatoliklar Jadvali

| # | Nomi | Severity | Fayl:satr | Simulyatsiya |
|---|------|----------|-----------|--------------|
| P1 | PLC butun WORD ni `==trigger_on_value(1)` bilan solishtiradi (bitmask yo'q) | **Critical** | `plc_melsec.py:96-99`, `plc_service.py:148-162`, `config.py:198-199` | T5 ✅ |
| P2 | Duplicate VIN (90s) yangi sessiyani jim bloklaydi (na on_result, na on_fail) | **Critical** | `ocr_worker.py:626-628, 640-648` | T1 ✅ |
| P3 | `stop()` ish vaqtida → qolgan sentinel keyingi workerни o'ldiradi | **Critical** | `ocr_worker.py:303-316, 418-427` | T2 ✅ |
| P4 | Finalize'dan keyin kech OCR natijasi → qo'shimcha legacy DB yozuvi + PLC puls | **High** | `pipeline.py:638-644, 656-673` | T4 ✅ |
| P5 | PLC poll davridan qisqa trigger pulsi o'tkazib yuboriladi | **High** | `plc_service.py:120-152`, `config.py:200` | T6 ✅ |
| P6 | Auto-off + level-high signal = re-trigger / camera flapping | **High** | `plc_service.py:97-105, 142-150`; `pipeline.py:422-426` | analitik + T5 |
| P7 | RFID R700 preset `400 Bad Request` (rfMode antennalararo bir xil emas) | **High** | `rfid_reader.py`/`r700_client.py` (preset), log dalili | log ✅ |
| P8 | `OCR.min_interval_sec>0` dequeue qilingan ishni yo'qotadi (requeue yo'q, fail yo'q) | **Medium** | `ocr_worker.py:429-434` | T3 ✅ |
| P9 | Faol sessiya davomida yangi PLC trigger butunlay tashlanadi (liniya tezligi cheklovi) | **Medium/High** | `pipeline.py:239-243` | analitik |
| P10 | PaddleOCR CPU da ishlayapti (paddlepaddle CPU build), GPU emas | **Medium** | `requirements.txt:17`, log `gpu=False` | log ✅ |
| P11 | `config` validatsiyasi yo'q — YAML xato tip jimgina qabul qilinadi | **Medium** | `config.py:300-307` | analitik |
| P12 | `requirements.txt` UTF-16 kodlangan + `torch==2.12.0` pin (pip parse/mavjudlik xavfi) | **Medium** | `requirements.txt` | fayl ✅ |
| P13 | PLC connect porti `5003` (docstring 5004), maydonda doimiy timeout | **Medium** | `config.py:196`, `plc_melsec.py:6`, errors.log | log ✅ |
| P14 | Confusion xaritasi to'liq emas (Z↔2, D↔0, 6↔G bor, lekin ko'p juftlar yo'q) → tuzatib bo'lmaydigan misread → TIMEOUT | **Medium** | `vin_postprocess.py:30-46` | analitik |
| P15 | Default `admin/admin`, sessiyalar xotirada, auth zaif | **Low/Medium** | `config.py:177-183` | analitik |
| P16 | Crops papkasi cheksiz o'sadi (rotation/cleanup yo'q) | **Low** | `pipeline.py:646-651` | analitik |
| P17 | FastAPI `@app.on_event` eskirgan (lifespan emas); watchdog/rfid threadlari join qilinmaydi | **Low** | `server.py:79-103` | analitik |

✅ = `audit/stress_failuremode.py` da reproduce qilingan.

---

## 4. Har Bir Muammo — To'liq Tahlil

### P1 — PLC trigger butun WORD ni `1` bilan solishtiradi *(Critical)*

- **Simptom:** Haqiqiy PLC bilan sessiya umuman boshlanmaydi yoki tasodifiy START/STOP flap qiladi. Logда: `PLC signal -> 8720: ishlov berish TO'XTAYDI (idle)`.
- **Root cause:** `D` prefiks = word qurilma (`plc_melsec.py:_is_word_device`). `read_signal()` butun word qiymatini qaytaradi (`8720`). `plc_service._handle_transition` esa `value == PLC.trigger_on_value` (=1) deb solishtiradi. ARRIVE signali odatda word ichidagi **bitta bit** (mas. bit0), butun word emas. Bitmask/bit-tanlash yo'q.
- **Dalil:** `plc_melsec.py:96-99` (word read), `plc_service.py:148-162` (edge+compare), log `PLC signal -> 8720`.
- **Reproduce:** T5 — `seq=[8720,8721,8720,1,8720]`, `trigger_on_value=1` → `on_start=1`, `on_stop=4`. Word o'zgarishlari flap yaratadi, trigger faqat aniq `1` da.
- **Production xavfi:** Mashina keladi, lekin sessiya boshlanmaydi → VIN umuman o'qilmaydi. Yoki word liniya holatiga qarab o'zgarsa, kamera doimiy connect/disconnect.
- **Latency/throughput:** Trigger ishonchsiz → 0% yoki tasodifiy ishlov.
- **Yechim:** Word register uchun **bit-tanlash** qo'shish: configга `signal_bit` (mas. `D521.0`) yoki `trigger_mask` qo'shib, `(value & mask) == expected` mantiqini ishlatish. Yoki PLC dasturchisi bilan D521 ning aniq semantikasini aniqlash (qaysi bit ARRIVE). Word qiymatini `trigger_on_value` bilan solishtirishni faqat aniq integer-rejimda qoldirish.
- **Test:** `signal_bit=0`, word `0x0001` → start; `0x0000` → stop; `0x2210 (8720)` bit0=0 → stop; flapping bo'lmasligini tasdiqlash.

### P2 — Duplicate VIN yangi sessiyani jim bloklaydi *(Critical)*

- **Simptom:** Bir xil VIN li mashina (yoki bir mashina qayta skanerlanса) → sessiya 90s osilib, `VIN=NO_READ, STATUS=TIMEOUT` yoziladi. Operatorга mashina "o'qilmadi" ko'rinadi.
- **Root cause:** `_process_job` da `_is_duplicate(vin)` True bo'lsa, funksiya `on_result` HAM, `on_fail` HAM chaqirmasdan `return` qiladi (`ocr_worker.py:626-628`). Pipeline esa VIN ham, fail xabarini ham olmaydi → `_session_vin=None` qoladi → watchdog TIMEOUT. Bundan tashqari `OCR.duplicate_window_sec=90` == `SESSION.timeout_sec=90` — ya'ni oyna butun sessiyani qoplaydi.
- **Dalil:** `ocr_worker.py:626-628` (return), `:640-648` (`_is_duplicate`).
- **Reproduce:** T1 — ikki marta bir xil VIN: `on_result=1, on_fail=0`. Ikkinchi sessiya hech narsa olmadi.
- **Production xavfi:** Ketma-ket bir xil VIN (re-work, qayta o'tish, yoki haqiqiy dublikat) → soxta TIMEOUT, mashina yo'qoladi.
- **Yechim:** Duplicate aniqlash **session-aware** bo'lishi kerak. Bitta PLC sessiyasi ichida bir xil VIN qabul qilinishi kerak (bu — yangi mashina hodisasi). Anti-duplicate faqat **bir sessiya ichidagi** takror submit'larни bloklasin, sessiyalararo emas. Yoki duplicate bo'lsa ham sessiyaga VIN ni yetkazib, faqat DB-darajasida idempotentlikni `session_id` bilan ta'minlash.
- **Test:** Sessiya #1 da VIN X → SUCCESS; sessiya #2 da yana VIN X → SUCCESS (bloklanmasin); bir sessiya ichida ikkinchi marta submit X → e'tiborsiz.

### P3 — `stop()` ish vaqtida → qolgan sentinel keyingi workerни o'ldiradi *(Critical)*

- **Simptom:** Sessiya yopilgach (`stop_processing`→`ocr.stop()`) keyingi sessiyada OCR `running=True` ko'rsatadi, lekin croplarni umuman qayta ishlamaydi — VIN hech qachon chiqmaydi.
- **Root cause:** `stop()` navbatni drain qiladi, so'ng `None` sentinel qo'yadi, so'ng `join(timeout=2.0)`. Agar worker thread uzun OCR ishini bajarayotgan bo'lsa (CPU da 1-3s), join timeout bilan tugaydi; thread ish tugagach `while self._running` (=False) ni ko'rib **sentinel ni o'qimasdan** chiqadi. Sentinel navbatda qoladi. Keyingi `start()` yangi thread yaratadi; u birinchi `get()` da o'sha eski `None` ni oladi → `break` → darhol o'ladi.
- **Dalil:** `ocr_worker.py:303-316` (stop), `:418-427` (`_loop` sentinel break).
- **Reproduce:** T2 — stop() ishlayotgan jobда; restartdан keyin `thread_alive=False, processed=False, running=True, queue=1`. Buzilish aniq.
- **Production xavfi:** Birinchi sekin OCR sessiyasidan keyin OCR "o'lik" qoladi; barcha keyingi mashinalar VIN siz TIMEOUT bo'ladi. Bu logдagi 22:55:35 dagi start/stop bo'roniга mos potentsial holat.
- **Latency:** O'lik worker → 100% VIN yo'qotish keyingi sessiyalarda.
- **Yechim:** Sentinel mexanizmini almashtirish: (a) `_loop` da `get()` dan keyin `_running` ni qayta tekshirish va sentinel'larni "consume but continue" qilmaslik; (b) sentinel o'rniga `threading.Event` ishlatish; (c) yoki har `start()` da navbatni drain qilib, eski sentinel'ni tozalash; (d) thread referensiyasini to'g'ri boshqarish (eski thread tugaganini kafolatlash yoki generation-id bilan). Eng toza: worker ni **uzluksiz** qoldirish (sessiyalararo to'xtatmaslik), `set_enabled(False)` bilan boshqarish.
- **Test:** Sekin job (1s) → stop() → start() → yangi job 0.8s ichida qayta ishlanishi kerak (`processed=True`).

### P4 — Finalize'dan keyin kech OCR natijasi → qo'shimcha legacy DB yozuvi *(High)*

- **Simptom:** Bitta mashina uchun ikki DB qatori; ulardan biri `status=OK` (legacy yo'l) qo'shimcha; PLC ikki marta puls oladi.
- **Root cause:** Sessiya `_finalize_session` da `_session_active=False` bo'ladi. Keyin kech kelgan `_on_ocr_result(... session_id=joriy)`: stale-guard `self._session_active and not finalized` shartiga tayanadi — ikkalasi ham False bo'lгani uchun guard **ishlamaydi**. So'ng `session_active=False` → `_legacy_write_record()` chaqiriladi → ikkinchi DB yozuvi + `on_vin_done()` qayta puls.
- **Dalil:** `pipeline.py:638-644` (stale guard faqat active sessiya uchun), `:656-673` (legacy fallback).
- **Reproduce:** T4 — finalize qilingan #5 sessiyasiga kech natija: `DB inserts=1, PLC pulses=1`. Control (yangi #6 aktiv, eski #5 natijasi): `inserts=0` — guard faqat shu holatda ishlaydi.
- **Production xavfi:** Idempotentlik buzilishi: duplicate yozuvlar, noto'g'ri PLC pulslari (P6 re-trigger bilan birga kuchayadi).
- **Yechim:** `_on_ocr_result` da: agar `session_id` berilgan VA u allaqachon **finalize bo'lgan** sessiyaga tegishli bo'lsa (`session_id <= self._session_id` va sessiya aktiv emas, yoki `session_id != current`), natijani **butunlay e'tiborsiz** qoldirish. Legacy yozuv faqat `session_id is None` (haqiqiy qo'lda rejim) bo'lganda ishlasin.
- **Test:** finalize #5 → kech natija(sid=5) → 0 insert; qo'lda rejim (sid=None) → 1 insert.

### P5 — Qisqa trigger pulsi poll davridan o'tkazib yuboriladi *(High)*

- **Simptom:** Tez liniyada mashina kelib o'tadi, lekin PLC pulsi 500ms dan qisqa bo'lsa, tizim umuman sezmaydi.
- **Root cause:** Polling 500ms (`poll_interval_ms`). Level-edge faqat poll momentidagi qiymatni ko'radi. 500ms dan qisqa puls ikki poll orasiga tushib qoladi.
- **Dalil:** `plc_service.py:120-152`, `config.py:200`.
- **Reproduce:** T6 — 50ms puls, 500ms poll → `on_start=0`.
- **Production xavfi:** Mashinalar jimgina yo'qoladi (DB da umuman qator yo'q).
- **Yechim:** (a) Poll intervalни kamaytirish (mas. 50-100ms) — lekin tarmoq yuki oshadi; (b) PLC tomonda ARRIVE bitni **latch** qilish (mashina o'tguncha 1 da turadi) va tizim VIN o'qilgach qu 0 ga tushiradi (handshake); (c) PLC dan rising-edge counterни o'qib, hech bir o'tishni o'tkazib yubormaslik. Eng ishonchli: latch + handshake.
- **Test:** 50ms puls + latch → start ishlaydi; handshake bilan 0 ga tushadi.

### P6 — Auto-off + level-high signal = re-trigger bo'roni *(High)*

- **Simptom:** Bitta mashina uchun kamera ketma-ket connect/disconnect, ko'p sessiya, ko'p DB qatori.
- **Root cause:** VIN o'qilgach `notify_vin_done()` haqiqiy PLC da `_force_off=True` qo'yadi → keyingi poll qiymatni mahalliy 0 deb qabul qiladi → `_last_signal=0`. Lekin fizik D521 hali ham trigger qiymatida (mashina hali sensorда). Keyingi poll fizik qiymatni o'qiydi (`1`/yuqori) → `1 != 0` → ko'tarilish qirrasi → `on_start` → **yangi sessiya**. `full_stop_on_zero=True` bo'lgani uchun oradа disconnect ham bo'ladi → flapping.
- **Dalil:** `plc_service.py:97-105` (`notify_vin_done`/`_force_off`), `:142-150` (poll qiymatni qayta o'qiydi), `pipeline.py:422-426` (`on_vin_done`).
- **Reproduce:** T5 word-flap bilan bog'liq; to'liq HW-in-loop kerak, lekin mantiq aniq.
- **Production xavfi:** Resurs sarfi, takroriy yozuv, operator chalkashligi, kamera ulanish beqarorligi.
- **Yechim:** Handshake protokoli: tizim VIN/sessiya tugagach PLC ga "DONE" bitini yozsin; PLC ARRIVE ni 0 ga tushirsin; tizim ARRIVE 0 ga tushganini **kutib**, keyin keyingi rising-edge ni qabul qilsin (`_armed` bayrog'i: sessiyadan keyin signal fizik 0 ga tushmaguncha qayta arm bo'lmasin). `_force_off` lokal nayrang o'rniga real edge-tracking.
- **Test:** Signal 1 da turibdi, VIN o'qildi → bitta sessiya; signal 0 ga tushmaguncha yangi sessiya ochilmasligi.

### P7 — RFID R700 preset `400 Bad Request` *(High)*

- **Simptom:** RFID konfiguratsiya rad etiladi, "transient" rejimga tushadi; NO_TAG/NO_READ ko'p (oldingi logда 92 RFID oqim xatosi).
- **Root cause:** Preset `rfMode` antennalararo bir xil emas: `{"invalidPropertyId":"#/antennaConfigs/1/rfMode","detail":"Must be the same for all antennas"}`. Antenna konfiguratsiyasi har port uchun har xil rfMode yuboradi.
- **Dalil:** `ai_cam.log` 13:37:01 R700 initialize 400; preset qurilishi `r700_client.py`/`rfid_reader.py`.
- **Production xavfi:** RFID ishonchsiz → `require_rfid=true` bilan ko'p sessiya `VIN_OK_RFID_TIMEOUT`.
- **Yechim:** Preset JSON da barcha `antennaConfigs[].rfMode` ни bir xil qiymatga keltirish (yoki global rfMode ishlatish). R700 firmware versiyasiga mos sxemani tekshirish.
- **Test:** Preset POST → 200; inventory start → teglar keladi.

### P8 — `min_interval_sec>0` dequeue qilingan ishni yo'qotadi *(Medium, latent)*

- **Simptom:** Hozir `min_interval_sec=0` — faol emas. Yoqilsa, throttle vaqtida dequeue qilingan job `continue` bilan **tashlanadi** (requeue yo'q), `on_fail` ham chaqirilmaydi.
- **Root cause:** `ocr_worker.py:429-434` — job allaqachon `get()` qilingan, throttle `continue` qiladi → yo'qoladi.
- **Reproduce:** T3 — `on_result=0, on_fail=0` (job yo'qoldi).
- **Yechim:** Throttle'дан oldin job ni dequeue qilmaslik (peek), yoki throttle vaqtida `time.sleep(qolgan)` qilib jobни saqlash. `min_interval_sec` ni hozircha 0 da qoldirish.

### P9 — Faol sessiya yangi triggerни tashlaydi *(Medium/High)*

- **Simptom:** Liniya tez bo'lsa, oldingi sessiya 90s (yoki RFID grace) tugamaguncha keyingi mashina e'tiborsiz qoladi.
- **Root cause:** `plc_on` da `if self._session_active: return` (`pipeline.py:239-243`). Bu holat himoyasi uchun to'g'ri, lekin VIN-only SUCCESS tez bo'lsa muammo kam; RFID kerak bo'lsa 90s blok.
- **Yechim:** Liniya takt vaqtini o'lchab `timeout_sec`/`rfid_grace_sec` ни moslash; yoki sessiyalar navbati (queue). Tashlangan triggerlarни metrikaga yozish (observability).

### P10 — PaddleOCR CPU da ishlayapti *(Medium)*

- **Dalil:** Log `OCR.use_gpu=True, lekin CUDA topilmadi — CPU`; `gpu=False`. `requirements.txt` CPU `paddlepaddle` ni o'rnatadi.
- **Xavf:** CPU OCR det+rec + retry (5 urinishgача) = bir hodisa uchun ~1-3s → throughput past, sessiya vaqti yeyiladi.
- **Yechim:** Production GPU serverда `paddlepaddle-gpu` o'rnatish (requirements izohida bor); startup'da real device tekshiruvini healthcheck ga chiqarish (P10 fix allaqachon logда bor).

### P11 — Config validatsiyasi yo'q *(Medium)*

- `_apply_section` YAML kalitlarini tip tekshirмасдан `setattr` qiladi (`config.py:300-307`). `timeout: 5` (int) vs `5.0`, yoki `tx_power_cdbm: "3150"` (str) jimgina o'tadi → runtime'da xato. Schema validatsiya (pydantic yoki qo'lda) qo'shish kerak.

### P12 — requirements.txt UTF-16 + torch pin *(Medium)*

- Fayl **UTF-16** kodlangan (BOM `ÿþ`, har belgi orasида bo'shliq baytlar). `pip install -r` ba'zi muhitларда buni parse qila olmaydi. Bundan tashqari `torch==2.12.0`/`torchvision==0.27.0` pinlari muhit/CUDA bilan mosligini tekshirish kerak. **Tavsiya:** faylni UTF-8 ga aylantirish, pinlarni o'rnatilgan muhitда tekshirish.

### P13 — PLC port 5003 vs 5004 *(Medium)*

- `config.py:196` port `5003`; `plc_melsec.py:6` docstring `5004`. Maydonда doimiy `timed out` (errors.log 15:44-16:05). MC protokol porti odatda PLC параметрида sozlanadi — to'g'ri portni PLC muhandisi bilan tasdiqlash. `0xC05C` xatosi avval bit-read'дан kelgan (keyin word'ga o'tilgan).

### P14 — Confusion xaritasi to'liq emas *(Medium, aniqlik)*

- `vin_postprocess.CONFUSIONS` da `Z↔2`, `D↔0`, `5↔6`, `2↔7`, `8↔0` kabi keng tarqalgan etched-metal misread juftlari yo'q. "Never invent" siyosati tufayli xaritada bo'lmagan misread tuzatilmaydi → VIN rad → retry → ehtimoliy TIMEOUT. Datasetdagi haqiqiy misread'larни tahlil qilib xaritani kengaytirish kerak.

### P15-P17 — Low/Medium

- **P15:** `admin/admin`, sessiyalar `auth.py` xotirasида — restartда login yo'qoladi, zaif parol. Zavod intranet uchun maqbul, lekin parol o'zgartirish majburiy bo'lsin.
- **P16:** `CROPS_DIR` cheksiz o'sadi — disk to'lishi. Rotation/retention siyosati kerak.
- **P17:** `@app.on_event` eskirgan (FastAPI lifespan ga o'tish); watchdog/rfid-read threadlari daemon, shutdown'да join qilinmaydi (graceful drain yo'q).

---

## 5. FMEA Jadvali

| Failure Mode | Cause | Effect | Detection | Mitigation | Priority |
|--------------|-------|--------|-----------|------------|----------|
| PLC trigger ishlamaydi/flap | Word `==1` solishtirish, bitmask yo'q | Sessiya boshlanmaydi yoki flapping | PLC signal log word qiymat ko'rsatadi | Bit-tanlash/mask, PLC semantikasini aniqlash | **Critical** |
| Yangi mashina jim bloklanadi | Dup oynasi = timeout, na result na fail | Soxta TIMEOUT/NO_READ | "DUPLICATE" log, VIN bor lekin TIMEOUT | Session-aware dedup; sessiyaga VIN yetkazish | **Critical** |
| OCR worker o'lik qoladi | stop() ish vaqtida → qolgan sentinel | Keyingi sessiyalar VINsiz | running=True lekin processed=0, queue>0 | Sentinel→Event yoki uzluksiz worker | **Critical** |
| Qo'sh DB yozuvi + qo'sh puls | Finalize'дан keyin kech natija legacy yo'l | Idempotentlik buzilishi | Bir mashinaга 2 qator (OK+SUCCESS) | session_id bilan late-result e'tiborsiz | **High** |
| Mashina jim yo'qoladi | Puls < poll davri | DB da qator yo'q | Liniya hisobi vs DB hisobi farqi | Latch+handshake yoki tez poll | **High** |
| Re-trigger / kamera flap | Auto-off + level-high | Ko'p sessiya, resurs sarfi | Ketma-ket connect/disconnect log | Edge handshake, `_armed` bayroq | **High** |
| RFID o'qimaydi | Preset rfMode 400 | Ko'p VIN_OK_RFID_TIMEOUT | R700 400 log, NO_TAG ko'p | rfMode barcha antenna bir xil | **High** |
| OCR job yo'qoladi | min_interval throttle dequeue+drop | VIN yo'qoladi (yoqilsa) | on_result=0, on_fail=0 | Peek/sleep, 0 da qoldirish | **Medium** |
| Liniya cheklovi | Faol sessiya yangi trigger tashlaydi | Mashina o'tkazib yuboriladi | Tashlangan trigger log yo'q | Takt-aware timeout, metrika | **Medium** |
| Past OCR throughput | Paddle CPU build | Sekin VIN, sessiya yeyiladi | gpu=False log, avg_ms yuqori | paddlepaddle-gpu | **Medium** |
| Config noto'g'ri tip | YAML validatsiya yo'q | Runtime xato | Startup'да yo'q tekshiruv | Schema validatsiya | **Medium** |
| Noto'g'ri VIN o'tib ketadi | Confusion xaritasi to'liq emas | Rad/TIMEOUT yoki xato VIN | raw vs validated farqi | Xaritani kengaytirish (datasetdan) | **Medium** |
| Disk to'ladi | Crop cleanup yo'q | Diskда joy tugaydi | Disk monitoring | Retention/rotation | **Low** |

---

## 6. Stress Test Natijalari

`audit/stress_failuremode.py` — haqiqiy kodga qarshi (PaddleOCR engine FakeEngine bilan almashtirilgan, qolgan hammasi loyiha kodi). Natijalar:

| Test | Stsenariy | Natija |
|------|-----------|--------|
| T1 | Duplicate VIN ketma-ket | **Bug reproduce:** `on_result=1, on_fail=0` — 2-sessiya hech narsa olmaydi |
| T2 | stop() ish vaqtida, keyin restart | **Bug reproduce:** `thread_alive=False, processed=False, running=True, queue=1` |
| T3 | `min_interval_sec=5` da job | **Bug reproduce:** job yo'qoldi (`on_result=0, on_fail=0`) |
| T4 | Finalize'дан keyin kech natija | **Bug reproduce:** `DB inserts=1, PLC pulses=1`; control (yangi sessiya aktiv) → `inserts=0` (guard shu holatda ishlaydi) |
| T5 | PLC word `[8720,8721,8720,1,8720]` | **Bug reproduce:** `on_start=1, on_stop=4` (flap) |
| T6 | 50ms puls, 500ms poll | **Bug reproduce:** `on_start=0` (puls yo'qoldi) |
| T7 | 100× rapid start/stop, keyin sog'lik | **OK:** crash yo'q, keyin OCR sog'lom (`healthy=True`, 1 ta worker thread) — ya'ni bo'ron o'zi xavfsiz; **xavf faqat T2 dagi "ish vaqtида stop"** |

Mavjud unit testlar: `python3 tests/test_vin_rules.py` → **18/18 passed** (VIN qoidalari va post-processing soz).

**Talqin:** T7 ko'rsatdiki, logдagi 22:55:35 dagi 100× start/stop bo'roni o'z-o'zidan halokatli emas (har stop bo'sh threadни join qilib sentinelни iste'mol qiladi). Haqiqiy xavf — **uzun OCR ishi davomida stop** (T2): bu CPU PaddleOCR da (har OCR ~1-3s) ishlab chiqarishда oson yuz beradi.

---

## 7. Root Cause Analysis (asosiy)

Foydalanuvchi simptomi: "YOLO ishlaydi, lekin OCR boshlanishi / crop yuborilishi / trigger-session / eski siklда qolish / kechikish". Asl sabablar **YOLO emas**, balki **trigger↔sessiya↔OCR koordinatsiyasi**:

1. **PLC interfeysi semantikasi (P1, P5, P6):** Word vs bit, poll vs puls, auto-off vs level-high. Bu eng yuqori darajadagi sabab — sessiya umuman noto'g'ri boshlanadi/yopiladi. Liniyaдa "trigger ishlamayapti" yoki "o'zi qayta-qayta ishlayapti" shikoyatlarining asosi shu.
2. **OCR worker hayot-sikli (P3, P8):** Sessiyalararo start/stop nozik; sentinel poyga sharti workerни o'ldirishi mumkin. "OCR boshlanmadi / crop ketdi-yu javob yo'q" simptomi.
3. **Dedup va sessiya korrelyatsiyasi (P2, P4):** Duplicate jim drop + finalize'дан keyin kech natija. "Eski siklда qoladi / chalkashadi" simptomi.
4. **RFID ishonchliligi (P7):** `require_rfid=true` bilan birga RFID preset xatosi ko'p qisman-muvaffaqiyat (VIN_OK_RFID_TIMEOUT) yaratadi.

Diff (oxirgi ish) bu zanjirning bir qismini (session_id, on_fail, grace, require_known_model) to'g'rilagan — bu yo'nalish to'g'ri, lekin **PLC interfeysi va dedup/sentinel/late-result** hali ochiq.

---

## 8. Production Kamchiliklari (Checklist)

| Talab | Holat | Izoh |
|-------|-------|------|
| Deterministik session_id | ⚠️ Qisman | Monotonik inkrement bor, lekin restartda 0 ga qaytadi; kech natija himoyasi to'liq emas (P4) |
| OCR job↔session korrelyatsiya | ⚠️ Qisman | submit/result'да session_id bor, lekin finalize'дан keyin himoya yo'q (P4) |
| Queue cleanup | ❌ | stop()да drain bor, lekin sentinel poygasi (P3) |
| Thread join | ⚠️ | OCR 2s, PLC 5s, stream 8s join; watchdog/rfid-read join yo'q (P17) |
| Retry policy | ✅ | `on_fail` + `ocr_session_max_triggers=12` |
| Timeout policy | ✅ | 90s + 15s grace; lekin liniya taktiga moslanмаган (P9) |
| VIN-only vs VIN+RFID | ✅ | `require_rfid` config bilan ajratilgan |
| Strict VIN validatsiya | ✅ | `require_known_model=true`, QY/BL7M qoidalari, 18/18 test |
| Observability/metrics | ⚠️ | OCR stats + `/api/logs/stats` bor; tashlangan trigger/missed-pulse metrikasi yo'q |
| Structured logs | ⚠️ | Matnli loglar, sessiya/manba teglari bor; JSON structured emas |
| Healthcheck | ❌ | Maxsus `/health` yo'q; GPU/PLC/RFID/camera holati `/api/logs/stats` ichida tarqoq |
| Graceful shutdown | ⚠️ | PLC/RFID stop bor; sessiya/watchdog/rfid-read drain yo'q |
| GPU capability verify | ✅ | Paddle real-device tekshiruvi qo'shilgan (log) |
| Config validation | ❌ | Schema yo'q (P11) |
| Operator-facing status nomlari | ✅ | SUCCESS / VIN_OK_RFID_TIMEOUT / TIMEOUT / NO_READ / NO_TAG |
| DB idempotency | ❌ | Bir sessiya = bir qator kafolatlanmagan (P4 qo'sh yozuv) |
| Duplicate handling | ❌ | Sessiyalararo dedup yangi mashinani bloklaydi (P2) |

---

## 9. Tavsiya Qilingan Algoritmik Yechimlar

1. **PLC trigger qatlamini qayta loyihalash (P1/P5/P6):**
   - Config: `signal_kind: bit|word|mask`, `signal_bit`/`trigger_mask`, `done_address` (handshake).
   - Mantiq: `triggered = (kind==bit ? val : (val & mask) == expected)`. Faqat **rising-edge** START; sessiya tugagach `done_address`ga DONE yozish; ARRIVE fizik 0 ga tushmaguncha `_armed=False` (qayta trigger yo'q).
   - Liniya pulsi qisqa bo'lsa: PLC tomon latch yoki tizim tomon edge-latch.

2. **Session-aware OCR / dedup (P2/P4):**
   - Anti-duplicate faqat **bir session_id ichida** ishlasin (yoki butunlay olib tashlanib, DB-darajada `UNIQUE(session_id)` bilan idempotentlik).
   - `_on_ocr_result`/`_on_ocr_fail`: `session_id != current` YOKI sessiya finalize bo'lgan → **darhol return** (legacy yozuvga tushmaslik).

3. **OCR worker hayot-siklini soddalashtirish (P3):**
   - Worker'ni **uzluksiz** qoldirish (startup'da bir marta start, shutdown'da bir marta stop). Sessiya bosh/oxiri faqat `set_enabled()` + `_drain_queue()` bilan boshqarilsin. Sentinel poygasi yo'qoladi.

4. **Handshake + observability:** Har sessiya uchun struktura: `session_id, trigger_ts, vin, rfid, status, latency_ms, ocr_attempts, dropped_triggers`. Prometheus/`/metrics` yoki kamida structured JSON log.

5. **RFID preset (P7):** `antennaConfigs[].rfMode` bir xil; R700 firmware sxemasiga moslashtirish; xatoда aniq fallback + metrika.

---

## 10. Patch Plan (qaysi faylда nima o'zgaradi)

> Eslatma: kod hali O'ZGARTIRILMAGAN. Quyida — alohida ruxsatдан keyingi reja.

| Fayl | O'zgarish |
|------|-----------|
| `config.py` / `settings.yaml` | PLC ga `signal_kind`, `signal_bit`/`trigger_mask`, `done_address` qo'shish; `poll_interval_ms` ni kamaytirish opsiyasi; config schema validatsiya funksiyasi |
| `plc_melsec.py` | Word'дан bit/mask ajratish; `done_address` ga yozish metodi (`write_done()`) |
| `plc_service.py` | `_handle_transition` → mask/bit mantiqi; rising-edge + `_armed` latch; `notify_vin_done` → handshake (DONE yozish, ARRIVE 0 kutish); short-pulse latch |
| `ocr_worker.py` | Sentinel → `Event` yoki uzluksiz worker; `stop()` ni `set_enabled(False)`+drain ga almashtirish; dedup ni session-aware qilish (yoki olib tashlash); `min_interval` throttle ni peek/sleep ga o'zgartirish |
| `pipeline.py` | `_on_ocr_result`/`_on_ocr_fail` da finalize/eski-session natijasini qat'iy e'tiborsiz qoldirish; OCR worker'ни sessiyalararo to'xtatmaslik; dropped-trigger metrikasi |
| `db.py` | `UNIQUE(session_id)` yoki `INSERT OR IGNORE` bilan idempotentlik; `session_id` ustuni |
| `rfid_reader.py`/`r700_client.py` | Preset `rfMode` ni barcha antenna uchun bir xil; init xato metrikasi |
| `server.py` | `@app.on_event` → lifespan; `/health` endpoint (GPU/PLC/RFID/camera/OCR holati); graceful drain |
| `requirements.txt` | UTF-8 ga aylantirish; GPU muhit uchun `paddlepaddle-gpu`; torch pinlarni tasdiqlash |
| `audit/stress_failuremode.py` | Regress test sifatida saqlash; fix'lardan keyin barcha "BUG REPRODUCED" → "ok" bo'lishi kerak |

---

## 11. Test Plan

1. **Regress (mavjud):** `python3 tests/test_vin_rules.py` → 18/18.
2. **Failure-mode regress:** `python3 audit/stress_failuremode.py` — fix'lardan keyin T1-T6 "BUG REPRODUCED" emas, bal "ok" bo'lishi shart; T7 sog'lom qolishi.
3. **Yangi testlar qo'shish:**
   - PLC bit/mask: word `0x2210` bit0=0 → stop; `0x0001` → start; flapping yo'q.
   - Session-aware dedup: sessiya #1 VIN X SUCCESS; #2 VIN X SUCCESS (bloklanmaydi).
   - Late-result: finalize #5 → kech natija(sid=5) → 0 DB insert.
   - Sentinel/uzluksiz worker: sekin job → "stop" → keyingi job qayta ishlanadi.
   - Short-pulse + latch: 50ms puls → start ishlaydi.
   - Handshake re-trigger: signal level-high → bitta sessiya, 0 ga tushmaguncha qayta yo'q.
   - RFID preset: mock 200 → inventory teglar.
   - Config validatsiya: noto'g'ri tip → startupда aniq xato.
4. **HW-in-loop (maydon):** Haqiqiy PLC/R700/Lector bilan: 100+ mashina o'tkazib, DB hisobi == liniya hisobi; missed/duplicate = 0; o'rtacha latency o'lchash.

---

## 12. Deployment Checklist

- [ ] PLC `D521` ning aniq semantikasini PLC muhandisi bilan tasdiqlash (qaysi bit ARRIVE, latch bormi).
- [ ] PLC port (`5003`/`5004`) va tarmoq yo'lini tasdiqlash (hozir timeout).
- [ ] R700 preset `rfMode` to'g'rilangan, `/api/rfid/read` 200 qaytaradi.
- [ ] GPU serverда `paddlepaddle-gpu` o'rnatilgan; startupда `real_device=gpu` logда.
- [ ] `requirements.txt` UTF-8; toza muhitда `pip install -r` ishlaydi.
- [ ] `admin` parolini o'zgartirish; `session_ttl` mos.
- [ ] `/health` endpoint yashil (PLC/RFID/camera/OCR/GPU).
- [ ] Crop retention/disk monitoring sozlangan.
- [ ] `audit/stress_failuremode.py` da barcha buglar "ok".
- [ ] HW-in-loop 100+ mashina sinovi o'tdi (missed=0, duplicate=0).
- [ ] Structured log + dropped-trigger/missed-pulse metrikasi yoqilgan.

---

## 13. Prioritet Bo'yicha Bajarish Tartibi

1. **P1 — PLC word/bit/mask + edge** (Critical) — boshqa hamma narsa shunga bog'liq.
2. **P3 — OCR worker uzluksiz / sentinel fix** (Critical).
3. **P2 — Session-aware dedup** (Critical).
4. **P4 — Late-result/finalize idempotentlik + DB UNIQUE** (High).
5. **P6 + P5 — PLC handshake + short-pulse latch** (High) — P1 bilan birga.
6. **P7 — RFID preset rfMode** (High).
7. **P10/P12/P13 — GPU build, requirements UTF-8, PLC port** (Medium, muhit).
8. **P8/P9/P11/P14 — throttle, takt-aware timeout, config schema, confusion xarita** (Medium).
9. **P15/P16/P17 — auth, crop retention, lifespan/graceful** (Low/Medium).

---

*Hisobot tugadi. Failure-mode'lar haqiqiy kod bilan reproduce qilingan: `audit/stress_failuremode.py`. Kod o'zgartirish — alohida ruxsatдан keyin.*
