# Kamera Decode — Log Tahlili va Algoritm Tuzatishlari

**Log:** `message_(6bc035).txt` (8457 satr, 2026-06-26 10:26–13:13)
**Branch:** `production-hardening`

---

## 1. Logdan aniqlangan (root cause)

| Belgi | Qiymat | Xulosa |
|-------|--------|--------|
| `decode_bmp: tasvir dekodlanmadi` | **1574 marta** | Hech bir frame tasvirga aylanmadi |
| Muvaffaqiyatli VIN/decode | **0 marta** | Pipeline OCR ga umuman crop yubormadi |
| OpenCV xatosi (1) | `m_rle_code_ ... BMP_BITFIELDS` (882×) | OpenCV "BM" topdi, lekin header buzuq |
| OpenCV xatosi (2) | `size > 0` assertion (810×) | width/height ≤ 0 — noto'g'ri offset |
| BLOB framing xatolari | **0** (Desync/length/timeout) | Tarmoq/framing SOZ — payload butun keladi |
| RFID | `EPC=1137` aniqlandi | RFID ishlayapti |
| PLC/session | faol (100 satr) | Trigger/sessiya ishlayapti |
| **Kamera geometriyasi** | `mDIGetEffImgSize` → **800 × 440** | Effektiv rasm o'lchami |
| Payload o'lchami | **30KB – 408KB (juda o'zgaruvchan)** | Fiksirlangan BMP emas — yo JPEG, yo o'zgaruvchan header |

**Xulosa:** muammo tarmoq yoki framing EMAS. Eski `decode_bmp` payload'ни qat'iy
`payload[19:]` deb hisoblardi va o'sha joyda haqiqiy tasvir yo'q edi (yoki "BM"
tasodifan tushib, OpenCV buzuq header'da yiqilardi). Haqiqiy tasvir **o'zgaruvchan
offsetда** va formati 19-baytga bog'lab bo'lmaydi.

---

## 2. Hal qilinganmi?

Bu log **eski decode** bilan yozilgan (restartdan keyin ham `camera_client.py:246`
eski kod). Ya'ni log muammoni **tasdiqlaydi**, lekin yangi tuzatishdan oldin
olingan. Yangi `decode_payload` aynan shu failure-mode'ni nishonga oladi.

---

## 3. Algoritmga kiritilgan o'zgarishlar (takrorlanishning oldini olish)

`backend/camera/camera_client.py` — `decode_payload(payload, hint_wh)`:

1. **Multi-magic qidiruv** (offsetga bog'lanmaydi): BMP(`BM`, header
   validatsiyasi bilan — tasodifiy "BM" rad etiladi), JPEG(`FF D8 FF`), PNG.
2. **BMP header validatsiyasi** (`_find_bmp_offset`): pixel-offset, DIB header
   o'lchami, fayl o'lchami ishonchli bo'lsagina qabul — OpenCV'ni buzuq header'ga
   bermaydi (882× `m_rle_code_` xatosini bartaraf etadi).
3. **8bpp raw BMP fallback** (OpenCV BMP'ni rad etsa).
4. **SICK RAW grayscale fallback** (YANGI, logdan): container topilmasa, kamera
   e'lon qilgan **800×440** (mDIGetEffImgSize) bo'yicha xom baytlar reshape
   qilinadi. Geometriya handshake'дан avtomatik olinadi (`img_width/img_height`).
   Payload kerakli o'lchamdan kichik bo'lsa reshape QILINMAYDI (JPEG'ни buzmaslik).
5. **Diagnostika**: format/shape o'zgarganda bir marta log; decode bo'lmasa
   128-bayt hex/ascii dump.

`backend/pipeline.py` — `_capture_loop`:

6. **Ground-truth dump**: 3-ketma-ket decode fail'da BITTA xom payload
   `logs/failed_payload_*.bin` ga saqlanadi → offline tahlil uchun. Bu kamera
   ASLIDA nima yuborayotganini aniqlab, muammo qaytalanmasligini ta'minlaydi.
7. Fail streak hisoblanadi; 5/10-faildan keyin aniq xabar; `/health` va
   `/api/status` da `decode_format`, `decode_fail_total`.

---

## 4. Tekshiruv (sintetik payloadlar bilan, haqiqiy decode mantig'i)

`decode_payload` quyidagilarni to'g'ri dekod qildi (6/6 PASS):
- BMP (19B sub-header bilan / XML orqasida / xom)
- JPEG (tasodifiy "BM" oldida bo'lsa ham JPEG to'g'ri tanlandi)
- PNG
- **RAW 800×440 grayscale** (SICK header bilan) → RAW8, shape (440, 800)
- Juda kichik junk → reshape QILINMADI (noto'g'ri tasvir yo'q), hexdump chiqdi

---

## 5. Keyingi qadam (real kamerada tasdiqlash)

1. Yangi kodni Ubuntu serverda ishga tushiring.
2. Live preview frame chiqishini va logда `Kamera dekod: format=… shape=(440, 800)`
   ko'rinishini tekshiring.
3. Agar hali ham fail bo'lsa: `logs/failed_payload_*.bin` avtomatik saqlanadi —
   uni tahlil qiling:
   ```
   python tools/inspect_camera_payload.py logs/failed_payload_XXXX.bin --wh 800x440
   ```
   yoki to'liq benchmark:
   ```
   python tools/benchmark_camera.py --ip <kamera_ip> --frames 100 --dump 3
   ```
   Bu kamera formatini (BMP/JPEG/RAW) aniq ko'rsatadi — keyin algoritm shунга
   moslanadi (lekin 4 fallback allaqachon barcha ehtimoliy formatlarni qoplaydi).
