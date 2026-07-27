# OCR v1.1.3 production validation

> Historical record. Production-derived crops and their VIN manifest are not
> distributed with the portable v1.2.1 release.

## Qabul siyosati

`v1.1.3` VINni faqat 17 belgili va model strukturasi bilan mos bo'lgani uchun
qabul qilmaydi. Qabul qilishda quyidagi mustaqil dalillar ishlatiladi:

- raw va CLAHE strict to'liq VINlari o'zaro zid bo'lsa `OCR_AMBIGUOUS`;
- barcha o'zgaruvchan pozitsiyalar va 12–17 serial qismida margin tekshiriladi;
- `0 -> D`, `3/8 -> B` kabi faqat strukturadan kelgan pos5 tuzatishlari exact
  rasm dalilisiz rad etiladi;
- past pos5 margin rescue faqat ikki mustaqil crop yoki raw+CLAHE exact
  kelishuvi bor va boshqa qonuniy to'liq VIN nomzodi yo'q bo'lsa ishlaydi;
- perceptual jihatdan takror frame'lar mustaqil crop deb ikki marta sanalmaydi.

BL7M formatida tarixiy `NSTHD41ABTJ...` va joriy `NSTHD41ABUJ...` ikkalasi
qo'llanadi. QY pos5 bitta `C` qiymatiga hardcode qilinmagan: tarixiy A/B/C va
boshqa tasdiqlangan komplektatsiya kodlari saqlangan.

## 2026-07-22 regressiya to'plami

Qo'lda tekshirilgan 14 crop manifesti ishlatilgan. Ishlab chiqarish
identifikatorlarini release ichida tarqatmaslik uchun manifest va croplar
portable v1.2.1 repozitoriysidan chiqarilgan.

Muhim safety regressiyalari:

- haqiqiy `NSTFC814ETJ042178/042184/042189`, strukturadan hosil bo'lgan noto'g'ri
  `NSTFD...` qabul qilinmasligi kerak;
- haqiqiy `NSTFC814ETJ042186` uchun raw `...2188`, CLAHE `...2186` bergan. Yangi
  strict-conflict gate buni `OCR_AMBIGUOUS` qilib, false acceptni to'xtatdi;
- `NSTHD41ABUJ000768` real Paddle bilan exact `ACCEPT` bo'ldi.

Bounded real-Paddle A/B tekshiruvida yuqoridagi xavfli 6/8 namuna eski gate bilan
`ACCEPT ...2188` bo'lgan, yangi gate bilan `OCR_AMBIGUOUS` va `false_accept=0`
bo'ldi. Bitta saqlangan `...2167` crop offline `NO_READ`; live hodisadagi boshqa
framelar saqlanmagani uchun bu yakka rasm to'liq multi-frame replay emas.

2026-07-22 dagi barcha 14 saqlangan crop'ning CPU replay'i yana bitta safety
regressiyani topdi: haqiqiy `NSTHD41ABUJ000764` uchun Paddle bitta yuqori
confidence o'qishda strukturaga qonuniy, ammo noto'g'ri `NSTHD41ABVJ000764`
bergan. Shu sabab productionda `accept_single_read=false` va
`accept_min_crops=2` qilindi. Qayta replay natijasi:

- exact va qabul qilingan: `7/14` (`50.0%`);
- `OCR_AMBIGUOUS`: `5/14` (`35.7%`);
- `NO_READ`: `2/14` (`14.3%`);
- false accept: `0/14`.

Bu yakka saqlangan crop replay'i bo'lib, live hodisadagi top-k frame fusion
yield'ini o'lchamaydi. Natija safety gate ishlayotganini ko'rsatadi, lekin 99%
exact-accept mezoniga hali yetmaganini ham ochiq ko'rsatadi. CSV dalil:
`data/ocr_eval_20260722.csv`.

Tarixiy qayta ishga tushirish (faqat alohida ruxsat berilgan dataset bilan):

```powershell
.\.venv\Scripts\python.exe tools\evaluate_ocr.py `
  --crops <authorized-crops-directory> `
  --csv <authorized-manifest.csv> `
  --trace-reads `
  --out data\ocr_eval_20260722.csv
```

CI-safe safety regressiyasi:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_vin_rules.py tests\test_vin_fusion.py `
  tests\test_ocr_regression.py tests\test_ocr_contract.py `
  tests\test_ocr_process_worker.py -q
```

## 99% uchun qolgan validatsiya

Kod 99%ga tayyorlash uchun safety gate va auditni beradi, lekin 14 crop 99%
aniqlikni statistik isbotlamaydi. Release sign-off uchun kamida 300 ketma-ket,
qo'lda tekshirilgan production hodisasini (QY A/B/C va BL7M, kunduz/tun, glare,
harakat) original top-k framelari bilan baholash kerak. Mezoni:

- false accept: 0;
- exact accept: kamida 99%;
- qolganlari `OCR_AMBIGUOUS/NO_READ`, noto'g'ri VIN emas;
- p95 OCR latency local CPU va Ubuntu NVIDIA uchun session budjetidan kichik;
- barcha no-trigger sessiyalarda `frames_seen`, `max_yolo_conf`,
  `best_crop_quality`, `ocr_triggered` audit qiymatlari mavjud.

Pilot `vin_slot_recognizer` validatsiyadan o'tmagani sabab productionda o'chirilgan.
U faqat alohida labelled testda false-accept=0 ko'rsatgach shadow, keyin assist
rejimiga o'tkazilishi mumkin.
