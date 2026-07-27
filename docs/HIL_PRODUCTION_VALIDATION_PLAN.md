# AI_CAM — Hardware-in-the-Loop (HIL) Production Validation Plan

**Maqsad:** kod darajasida yopilgan (`CODE REMEDIATED`) barcha xavflarni real
ishlab chiqarish liniyasida (SICK kamera, Impinj R700 RFID, Mitsubishi MELSEC
PLC, real konveyer) tasdiqlash. Bu testlar bajarilmaguncha tizim **`PRODUCTION
GO`** holatiga o'tmaydi — eng yuqori avtomatik holat: **`READY FOR CONTROLLED
HIL TEST`**.

> ⚠️ **XAVFSIZLIK:** Bu testlar real qurilma va real liniyaga ta'sir qiladi.
> Har bir testni controls muhandisi va operator nazorati ostida, liniya
> to'xtatilgan yoki nazorat qilinadigan rejimda bajaring. `plc.write_enabled`
> ni faqat DONE/ACK registri controls muhandisi tomonidan tasdiqlangandan keyin
> `true` qiling.

---

## 0. Old-shart: konfiguratsiya tasdiqlash (HIL bloklovchi)

Quyidagilar repo'da **TASDIQLANMAGAN** (audit topilmasi) — HIL'da controls
muhandisi bilan aniqlanishi SHART:

| Parametr | Hozirgi holat | HIL'da aniqlanishi kerak |
|---|---|---|
| `plc.signal_address` (D2222/START) | `D521` (word) | Real START registri va turi (bit/word/mask) |
| `plc.exit_address` (D2223/EXIT) | `null` | EXIT sensori MAVJUDMI? Manzili? |
| `plc.done_address` (ACK) | `null` | Real DONE/ACK registri (agar bor bo'lsa) |
| `plc.busy_address` | `null` | Real BUSY registri (agar bor bo'lsa) |
| `session.post_exit_grace_ms` | `2500` (MVP taxmin) | D2222→D2223 interval + OCR/RFID qoldiq-latency P99 dan hisoblansin |
| `session.max_session_duration_sec` | `30` | Real takt vaqti asosida |
| OCR latency P50/P95/P99 | fake (harness) | Real PaddleOCR (GPU/CPU) bilan o'lchansin |

Agar EXIT sensori (D2223) **mavjud bo'lmasa**, `exit_signal_enabled=false`
qoldirilib, tizim single-signal + `max_session_duration` failsafe rejimida
ishlaydi (kod buni qo'llab-quvvatlaydi).

---

## Test formati

Har bir HIL testi quyidagi tuzilishga ega:

- **Purpose** — nima tasdiqlanadi
- **Safety preconditions** — xavfsizlik shartlari
- **PLC/HW prerequisites** — kerakli qurilma holati
- **Personnel** — kim ishtirok etadi
- **Steps** — bajarish qadamlari
- **Expected** — kutilgan natija
- **Abort** — to'xtatish sharti
- **Evidence** — yig'iladigan dalil
- **Rollback** — orqaga qaytarish
- **Pass/Fail** — o'tish mezoni

---

## HIL-1 — PLC START trigger va qisqa impuls (P0-3, P2)

- **Purpose:** Real D521/START rising-edge bitta sessiya ochishini; qisqa impuls
  (poll oralig'idan qisqa) yo'qolmasligini (ladder latch/seal-in shart).
- **Safety:** Liniya bo'sh; test kuzovi qo'lda kiritiladi.
- **PLC prereq:** `write_enabled=false`; poll_interval_ms ma'lum.
- **Personnel:** Controls muhandisi + operator.
- **Steps:** (1) Test kuzovini START zonasiga kiriting. (2) Loglarda `[SESSION #..]
  Boshlandi` ni kuzating. (3) Juda qisqa impuls (agar ladder qo'llab-quvvatlasa)
  yuboring va sessiya ochilishini tekshiring.
- **Expected:** Har START = aynan bitta sessiya; qisqa impuls yo'qolmaydi.
- **Abort:** Kutilmagan ko'p sessiya yoki trigger storm.
- **Evidence:** `logs/plc.log`, `logs/ai_cam.log` (session_id bilan), DB yozuvlari.
- **Rollback:** Signalni OFF; sessiyani watchdog yopadi.
- **Pass/Fail:** PASS = 1 START → 1 sessiya → 1 DB yozuvi.

## HIL-2 — D2222→D2223 interval va grace kalibratsiyasi (grace CALIBRATION)

- **Purpose:** Real D2222→D2223 intervali va OCR/RFID qoldiq-latency asosida
  `post_exit_grace_ms` ni kalibrlash.
- **PLC prereq:** EXIT sensori (D2223) mavjud va manzili tasdiqlangan.
- **Steps:** (1) 30-50 real kuzovni normal tezlikda o'tkazing. (2) Har kuzov
  uchun D2222 va D2223 timestamp'larini (`/api/metrics`: `d2222_at`, `d2223_at`)
  yozib oling. (3) D2223 dan keyin kelgan OCR/RFID natijalar sonini va ularning
  kechikishini o'lchang.
- **Expected:** `post_exit_grace = max(1000ms, P99(D2223 keyingi OCR)×1.3,
  P99(D2223 keyingi RFID)×1.3)`.
- **Evidence:** interval jadvali, percentil hisobi.
- **Pass/Fail:** PASS = grace qiymati kalibrlangan va settings.yaml ga yozilgan;
  grace ichida hech qanday to'g'ri natija yo'qolmaydi.

## HIL-3 — Signal ON holatda qolishi → trigger storm YO'Q (P0-3)

- **Purpose:** START signal fizik ravishda ON bo'lib qolsa (sensor yopishib
  qolsa) takroriy sessiya ochilmasligini.
- **Steps:** (1) START signalni ~10s ON ushlab turing. (2) Sessiya sonini kuzating.
- **Expected:** Bitta sessiya; ACK yoki real OFF gacha yangi trigger yo'q.
- **Pass/Fail:** PASS = 10s ON davomida ≤1 sessiya.

## HIL-4 — Real DONE/ACK handshake (write_enabled) (P0-3)

- **Purpose:** `write_enabled=true` da real DONE registriga yozuv va handshake.
- **Safety:** ⚠️ FAQAT controls muhandisi DONE registrini tasdiqlagandan keyin.
- **Steps:** (1) `write_enabled=true`, `done_address=<tasdiqlangan>`. (2) Kuzov
  o'tkazing. (3) PLC ladder ACK ni qabul qilishini tekshiring.
- **Abort:** ⚠️ Noto'g'ri registrga yozuv xavfi → darhol `write_enabled=false`.
- **Pass/Fail:** PASS = ACK to'g'ri registrga yoziladi, ladder tasdiqlaydi,
  qayta-trigger yo'q.

## HIL-5 — PLC disconnect / reconnect (P1)

- **Purpose:** PLC tarmog'i uzilib-ulanганда reconnect va sessiya yaxlitligini.
- **Steps:** (1) PLC tarmoq kabelini uzing. (2) `/api/status` da PLC holatini
  kuzating. (3) Qayta ulang.
- **Expected:** Backoff bilan reconnect; ulanmagan davrda trigger yo'qolmaydi
  yoki aniq loglanadi.
- **Pass/Fail:** PASS = avtomatik reconnect, jim ma'lumot yo'qolishi yo'q.

## HIL-6 — Kamera trigger latency va real frame (P0-4)

- **Purpose:** Real SICK kamera capture latency va frame session-ownership.
- **Steps:** (1) Kuzov o'tkazing. (2) `/api/metrics`: `camera_last_frame_age`,
  frame_id/session_id bog'lanishini kuzating.
- **Pass/Fail:** PASS = frame joriy sessiyaga bog'lanadi, stale frame keyingi
  kuzovga o'tmaydi.

## HIL-7 — Kamera uzilishi → watchdog reconnect (P0-4)

- **Purpose:** Kamera kabeli uzilganda watchdog stale/dead stream'ni aniqlab
  reconnect qilishini; tizim "ishlayapti" deb yolg'on ko'rsatmasligini.
- **Steps:** (1) Kamera kabelini uzing. (2) `/api/status` `camera.state` ni
  kuzating (STALE/RECONNECTING/FAILED). (3) Qayta ulang.
- **Expected:** State STALE→RECONNECTING→CONNECTED; `camera_connected` faqat
  fresh frame kelganda `true`.
- **Pass/Fail:** PASS = dead stream aniqlanadi, avtomatik reconnect, health real
  holatni ko'rsatadi.

## HIL-8 — RFID SSE disconnect / reconnect (P1)

- **Purpose:** R700 SSE oqimi uzilganda auto-reconnect.
- **Steps:** (1) R700 tarmog'ini uzing. (2) Reconnect'ni kuzating.
- **Pass/Fail:** PASS = SSE avtomatik qayta ulanadi.

## HIL-9 — Antenna cross-read va eski teg (P0)

- **Purpose:** RFID antennasi oldingi/keyingi kuzov tegini o'qimasligini; eski
  teg RF maydonida qolса yangi sessiyaga yozilmasligini.
- **Steps:** (1) Ketma-ket 2 kuzovni bir xil EPC bilan / turli EPC bilan
  o'tkazing. (2) Har DB yozuvida EPC↔session_id bog'lanishini tekshiring.
- **Expected:** Har EPC o'z kuzovining session_id iga; cross-read yo'q.
- **Pass/Fail:** PASS = 0 noto'g'ri EPC↔kuzov bog'lanishi.

## HIL-10 — Bir nechta teg / RSSI tanlash (P2)

- **Purpose:** Bir vaqtda bir nechta teg ko'rinsa eng kuchli RSSI (to'g'ri zona)
  tanlanishini.
- **Pass/Fail:** PASS = to'g'ri (eng yaqin) teg tanlanadi.

## HIL-11 — Real OCR latency (P50/P95/P99) va GPU/CPU (Performance)

- **Purpose:** Real PaddleOCR latency'ni o'lchash (harness fake emas).
- **Steps:** (1) 100+ real kuzov o'tkazing. (2) OCR latency percentillarini
  (`/api/status` ocr stats) yig'ing. (3) GPU/CPU rejimini tekshiring
  (`torch.cuda.is_available()`).
- **Expected:** P99 OCR latency < grace/takt budjeti.
- **Pass/Fail:** PASS = P99 latency takt vaqtiga mos; queue backlog yo'q.

## HIL-12 — Real OCR hang → hard-timeout → worker restart (P0)

- **Software holati:** ✅ **Real process-worker software integration VERIFIED** —
  HAQIQIY PaddleOCR alohida child processda yuklanadi va inference bajaradi
  (`tests/test_ocr_real_integration.py`: worker_pid != parent, session/frame
  ownership, real VIN). Supervisor terminate+respawn mexanizmi fake worker bilan
  to'liq test qilingan (`tests/test_ocr_process_worker.py`: hang/crash/soft/hard).
  **HARDWARE/GPU hang xatti-harakati (real OOM/GPU-xato) HIL PENDING.**
- **Purpose:** Real PaddleOCR GPU/resurs xatosi bilan osilganda hard-timeout bilan
  terminate+restart qilishini; keyingi kuzov ishlashini real qurilmada tasdiqlash.
- **Prereq:** `ocr.process_worker_enabled=true` (software tayyor).
- **Steps:** (1) Real GPU OOM yoki resurs bloklash bilan sun'iy hang keltiring.
  (2) `/api/metrics`: `ocr_hard_timeout_total`, `ocr_worker_restart_total`.
- **Expected:** FastAPI/kamera tirik; osilgan kuzov hard-timeout bilan yopiladi;
  worker restart; keyingi kuzov muvaffaqiyatli.
- **Pass/Fail:** PASS = tizim to'xtamaydi, worker qayta ishga tushadi.

## HIL-13 — Server restart mid-session (P1)

- **Purpose:** Sessiya davomida server qayta ishga tushsa, PENDING sessiya
  restart-recovery bilan FAILED sifatida yopilishini (yo'qolmasligini).
- **Steps:** (1) Kuzov davomida serverни qayta ishga tushiring. (2) DB'da o'sha
  session PENDING→FAILED bo'lishini tekshiring.
- **Pass/Fail:** PASS = tugallanmagan sessiya ANIQ yakuniy holat oladi (jim
  yo'qolmaydi).

## HIL-14 — DB locked real yuk ostida (P2)

- **Purpose:** Katta yuk / export paytida DB locked bo'lsa natija yo'qolmasligi
  va jim SUCCESS ko'rsatilmasligi (DATABASE_FAILED guard).
- **Pass/Fail:** PASS = DB xatosi aniq status bilan; jim SUCCESS yo'q.

## HIL-15 — Real konveyer maksimal tezligi / takt (Throughput)

- **Purpose:** Real takt vaqtida (eng qisqa interval) tizim invariantni saqlab
  ishlashini; overlap/queue/backlog ostida ma'lumot yo'qolmasligini.
- **Steps:** (1) Liniyani real maksimal tezlikda ishga tushiring. (2) N kuzov
  o'tkazing. (3) `accepted_D2222 == finalized_DB_records` invariantini tekshiring.
- **Pass/Fail:** PASS = invariant saqlanadi; dropped_trigger_count = 0.

## HIL-16 — Multi-body korrelatsiya (P0 — asosiy maqsad)

- **Purpose:** Ketma-ket real kuzovlarда VIN↔RFID aynan o'z kuzoviga
  bog'lanishini (traceability yadrosi).
- **Steps:** (1) 20+ ketma-ket kuzov (ma'lum VIN/EPC bilan). (2) Har DB yozuvida
  VIN↔EPC↔session_id to'g'riligini tekshiring.
- **Pass/Fail:** PASS = 0 noto'g'ri VIN↔RFID juftlashuvi; 0 session-mixing.

---

## HIL yakuniy qaror mezoni

Barcha HIL testlari PASS bo'lgandagina:

```
PRODUCTION GO
```

Aks holda:

```
CONDITIONAL PRODUCTION GO   (ba'zi HIL PASS, qolganlari monitoring ostida)
NO-GO                       (kritik HIL FAIL)
```

Bu hujjat bajarilmaguncha tizim holati: **`READY FOR CONTROLLED HIL TEST`**.
