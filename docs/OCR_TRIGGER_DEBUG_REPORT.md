# OCR / Trigger Debug Hisoboti

Sana: 2026-06-25  
Scope: kodga tegmasdan statik tahlil, mavjud testlar va berilgan yangi logni tekshirish.  
Qo'shimcha log: `C:/Users/II4028/AppData/Local/Packages/38833FF26BA1D.UnigramPreview_g9c9v27vpyspw/LocalState/0/documents/message_(52c060).txt`

## Qisqa xulosa

YOLO detect ishlayapti: logda `device=auto -> CUDA ... cuda:0` va model `device=cuda:0` bilan yuklangan.

Asosiy muammo OCR cropga yuborilgandan keyingi umumiy sessiya boshqaruvida:

1. VIN topilgan bo'lsa ham, RFID yoqilgan va RFID o'qilmasa sessiya `SUCCESS` bo'lmaydi. Natijada tizim 90 sekundlik eski sessiyada qoladi.
2. OCR trigger `submit_frames()` qilingan zahoti `_plate_locked=True` bo'ladi. OCR xato/rad javob bersa, lock faqat plastinka kadrdan ketganda ochiladi. Plastinka joyida tursa, shu sessiyada qayta OCR urinishi bloklanadi.
3. OCR joblarida `session_id` yo'q va OCR navbati stop/startda tozalanmaydi. Eski crop yangi sessiyaga kech callback berishi mumkin.
4. PaddleOCR GPU holati noto'g'ri ko'rsatilgan: app logi `gpu=True` desa ham, PaddleOCR ichki DEBUG logida `use_gpu=False` turibdi.
5. Logda `model=None` bo'lgan noto'g'ri VIN qabul qilingan: `HSTFC814ELJ040318`. Bu model-aware validatsiya bypass/generic fallback xavfini ko'rsatadi.

## Qo'shimcha logdan dalillar

Log statistikasi:

- `PLC signal -> 1`: 2 marta
- `TRIGGER (fusion)`: 1 marta
- `VIN aniqlandi`: 1 marta
- `[VIN] DETECTED`: 1 marta
- `SUCCESS`: 0 marta
- `TIMEOUT`: 8 marta
- `NO_READ`: 7 marta
- `RFID oqim xatosi`: 92 marta

Muhim vaqt ketma-ketligi:

- `16:46:20` - Session #1 boshlandi.
- `16:47:50` - RFID 22 urinishdan keyin `NO_READ`; sessiya `TIMEOUT`; DB: `VIN=NO_READ`, `RFID_EPC=NO_TAG`.
- `16:47:55` - Session #2 boshlandi.
- `16:49:02` - OCR trigger bo'ldi: `top-2 crop OCR ga yuborildi`.
- `16:49:02` - VIN topildi: `HSTFC814ELJ040318`, `model=None`, `score=0.81`.
- `16:49:25` - Shunga qaramay Session #2 `TIMEOUT`; DB: `VIN=HSTFC814ELJ040318`, `RFID_EPC=NO_TAG`, `STATUS=TIMEOUT`.

Bu aynan "VIN/OCR ishladi, lekin umumiy trigger eski siklda qoldi" simptomiga mos.

## Topilgan muammolar

### 1. VIN topilgandan keyin ham sessiya RFID sabab yopilmaydi

Kod joyi:

- `backend/pipeline.py:303-306` - `rfid_required = bool(RFID.enabled)` va `complete = vin_ok and (rfid_ok or not rfid_required)`.
- `backend/pipeline.py:324-391` - finalize faqat `SUCCESS`, `TIMEOUT` yoki `PLC=0` orqali yakunlanadi.

Natija:

- RFID reader ulanmagan bo'lsa ham VIN retry davom etmaydi, sessiya esa 90 sekundgacha faol qoladi.
- Shu vaqt ichida yangi PLC trigger e'tiborsiz qolishi mumkin, chunki `_session_active=True`.
- Logda VIN 16:49:02 da topilgan, lekin sessiya 16:49:25 da timeout bilan yopilgan.

Tavsiya:

- VIN topilganda kamera/OCR qismini alohida `done` qilish.
- RFID ishlamasa `PARTIAL_SUCCESS` yoki `VIN_OK_RFID_TIMEOUT` kabi status bilan sessiyani tezroq yopish.
- RFID required bo'lishi config orqali aniq ajratilishi kerak: productionda VIN mustaqil muvaffaqiyat deb qabul qilinadimi yoki RFID shartmi.

### 2. OCR trigger lock OCR natijasidan oldin yopiladi

Kod joyi:

- `backend/pipeline.py:543-544` - crop OCR ga yuborilgach `_plate_locked=True`.
- `backend/ai/ocr_worker.py:551-556` - agar OCR kandidati chiqmasa, callback bo'lmaydi va pipeline lockdan xabar topmaydi.
- `backend/pipeline.py:508-515` - lock faqat `len(dets)==0` va absent frame yetganda ochiladi.

Natija:

- OCR error yoki invalid VIN bo'lsa, ayni plastinka kadrda turgan paytda qayta OCR bo'lmaydi.
- Oldingi loglarda `PaddleOCR xatosi` ketma-ket 5 marta chiqib, keyin `yaroqli VIN topilmadi`; bunday holatda lock ochilishi faqat plastinka yo'qolganda bo'ladi.

Tavsiya:

- OCR worker reject/fail callback qaytarsin.
- Fail bo'lsa `_plate_locked=False` yoki retry counter/cooldown bilan qayta trigger ochilsin.
- Har sessiyada `ocr_attempts` limiti bo'lsin, lekin bitta failed OCR butun sessiyani bloklamasin.

### 3. OCR navbati sessiya bilan bog'lanmagan

Kod joyi:

- `backend/ai/ocr_worker.py:328-342` - job faqat `(crops, model)`.
- `backend/pipeline.py:588-601` - OCR callback kelgan paytdagi aktiv sessiyaga VIN yoziladi.
- `backend/ai/ocr_worker.py:283-286` - `stop()` sentinel qo'yadi, lekin thread join qilmaydi va queue tozalanmaydi.

Diagnostika:

- Tez start/stop stress testda source o'zgarmasdan `queue_size=5` qoldi. Bu navbat to'liq tozalanmasligi mumkinligini ko'rsatadi.

Xavf:

- Eski sessiya cropi kech qaytsa, yangi sessiyaga VIN sifatida yozilishi mumkin.
- Bu foydalanuvchi aytgan "eski siklda qolib ketayapti" yoki "o'zi chalkashib qolmoqda" holatini kuchaytiradi.

Tavsiya:

- OCR jobga `session_id` qo'shish.
- Callbackda `session_id` mosligini tekshirish.
- `stop_processing()` paytida OCR queue ni tozalash va worker threadni qisqa timeout bilan join qilish.

### 4. PaddleOCR GPU holati noto'g'ri ko'rinyapti

Log dalili:

- App: `PaddleOCR init OK ... (gpu=True)`.
- PaddleOCR DEBUG: `use_gpu=False`.

Kod joyi:

- `backend/ai/ocr_worker.py:115-136` - app berilgan `gpu` qiymatini log qiladi, PaddleOCR real device holatini tekshirmaydi.
- `requirements.txt:17` - default `paddlepaddle>=2.5,<3.0` CPU build bo'lishi mumkin.
- `requirements.txt:27-28` - GPU uchun alohida `paddlepaddle-gpu` o'rnatish izohi bor.

Natija:

- YOLO GPUda, OCR esa amalda CPUda bo'lishi mumkin.
- Log "gpu=True" deb aldaydi, real OCR tezligi/behaviorini noto'g'ri talqin qilishga olib keladi.

Tavsiya:

- GPU kompyuterda `paddle.device.is_compiled_with_cuda()` va `paddle.device.get_device()` bilan real holatni tekshirish.
- CUDA build kerak bo'lsa `paddlepaddle-gpu` o'rnatish.
- App logida torch CUDA emas, Paddle CUDA holati ko'rsatilishi kerak.

### 5. `model=None` VIN generic validatsiyadan o'tgan

Log dalili:

- `VIN aniqlandi: HSTFC814ELJ040318 [model=None, score=0.81 ...]`

Bu VIN ishlab chiqarish qoidalariga mos emas: kutilgan prefix `NSTF` yoki `NSTH`, lekin `HSTF` qabul qilingan.

Kod xavfi:

- `backend/ai/ocr_worker.py:477-486` - model prefix topilmasa fallback yo'li bor.
- `backend/ai/ocr_worker.py:466` atrofida legacy generic regex ishlatiladi.

Natija:

- Agar `VIN.enabled` o'chsa yoki model-aware yo'l bypass bo'lsa, oddiy 17 belgili VIN regex noto'g'ri ishlab chiqarish VINlarini ham qabul qiladi.

Tavsiya:

- Production rejimida `model=None` natija DB ga yozilmasin yoki `REVIEW` status bilan tushsin.
- QY/BL7M qoidalari majburiy bo'lsa, generic fallbackni o'chirish kerak.
- `raw_vin` va `validated_vin` alohida ko'rsatilib, model aniqlanmasa OCR qayta urinishi kerak.

### 6. Timeout paytida ham "VIN o'qildi" deb log chiqadi

Kod joyi:

- `backend/pipeline.py:387-391` - `_finalize_session()` oxirida `on_vin_done()` har qanday finalize sababida chaqiriladi.

Log dalili:

- Session #1 da `VIN=NO_READ`, lekin undan keyin `VIN o'qildi -> PLC simulyator signali 0` chiqadi.

Natija:

- Operator uchun noto'g'ri signal: tizim VIN o'qildi deb o'ylashi mumkin.

Tavsiya:

- Log matnini `sessiya yakunlandi -> PLC 0` kabi neytral qilish.
- `on_vin_done` nomini ham `on_session_done` ga ajratish ma'qul.

## Test natijalari

Mavjud VIN qoidalari testlari:

```text
python tests/test_vin_rules.py
18/18 tests passed
```

`pytest` orqali ishga tushirish:

```text
python -m pytest tests/test_vin_rules.py -q
No module named pytest
```

Demak test mantiqi o'tyapti, lekin dev muhitda `pytest` o'rnatilmagan.

## Keyingi tekshiruvlar

1. GPU real OCR holati:

```bash
python -c "import paddle; print(paddle.device.is_compiled_with_cuda(), paddle.device.get_device())"
```

2. RFID vaqtincha o'chirib VIN-only test:

- `rfid.enabled=false`
- PLC trigger bering
- VIN topilishi bilan sessiya darhol yopiladimi, `SUCCESS` bo'ladimi tekshiring.

3. OCR failure retry testi:

- PaddleOCR xato/reject qaytarganda `_plate_locked` qayta ochiladimi tekshirish kerak.
- Hozirgi kodda bu callback yo'q, shuning uchun failed OCR bitta hodisani bloklab qo'yishi mumkin.

4. `model=None` qabul qilinmasligi testi:

- `HSTFC814ELJ040318` kabi VIN productionda rad bo'lishi kerak.
- Bu uchun alohida unit test qo'shish kerak.

## Yakuniy tashxis

Muammo YOLO detectda emas. Asosiy root cause sessiya va OCR-trigger koordinatsiyasida:

- OCR one-shot lock natijadan oldin yopiladi.
- OCR joblar sessiya ID bilan himoyalanmagan.
- Sessiya VIN topilsa ham RFID sabab 90 sekund ushlab turiladi.
- PaddleOCR GPU holati noto'g'ri loglanmoqda.
- Generic VIN fallback ishlab chiqarish qoidalaridan tashqaridagi VINni qabul qilishi mumkin.

