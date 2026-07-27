# AI_CAM — Security, Observability & Operational Safety

Bu hujjat Remediation Agent 3 (Security, Observability, Configuration, Operational
Safety) tomonidan kiritilgan o'zgarishlarni, ularning sabablarini va operator
uchun zarur amaliy qadamlarni tavsiflaydi.

## 1. Production-mode ta'rifi va default-credential blocker

**Ta'rif** (`backend/config.py::is_production_mode()`):

```python
def is_production_mode() -> bool:
    return PLC.mode != "simulator" or RFID.mode != "simulator"
```

Tizim **production** hisoblanadi, agar PLC yoki RFID uchun haqiqiy uskuna
rejimi tanlansa (`plc.mode: "melsec"` yoki `rfid.mode: "r700"`). Ikkalasi ham
`"simulator"` bo'lsagina dev/demo muhit hisoblanadi; release defaultlari
qo'shimcha xavfsizlik uchun ikkala servisni ham `enabled: false` qiladi.

**Nima uchun bloker (faqat warning emas):** real PLC/RFID bilan ishlaydigan
liniya — bu ishlab chiqarish liniyasi. Standart `admin/admin` bilan tarmoq
ichidan (yoki noto'g'ri segmentatsiya bo'lsa tashqaridan ham) kirish trivial.
Bu darajadagi xavf uchun faqat log yozish yetarli emas.

**Xulq-atvor** (`backend/auth.py::enforce_startup_security_policy()`,
`backend/server.py` startup hodisasida chaqiriladi):

| AUTH.enabled | production_mode | placeholder/default | Natija |
|---|---|---|---|
| False | — | — | Tekshirilmaydi (auth butunlay o'chirilgan — alohida finding, pastga qarang) |
| True | False (simulator) | Ha | Ishga tushadi, faqat WARNING log |
| True | True | Ha | **RuntimeError — server ISHGA TUSHMAYDI** |
| True | True/False | Yo'q | Ishga tushadi |

**MUHIM — operator uchun amaliy ta'sir:** release `config/settings.yaml`
haqiqiy endpointlarni yoqmaydi: PLC/RFID o'chirilgan simulator holatida,
RFID credentials bo'sh, camera/auth parollari `CHANGE_ME` placeholder.
Simulator holatida diagnostika va hardware-free smoke ishlaydi. Haqiqiy
PLC/RFID rejimini yoqishdan oldin secretlarni YAML'ga commit qilish o'rniga
deployment muhiti orqali bering:

```powershell
$env:AI_CAM_CAMERA_PASSWORD = "<secret-manager-value>"
$env:AI_CAM_RFID_USERNAME = "<secret-manager-value>"
$env:AI_CAM_RFID_PASSWORD = "<secret-manager-value>"
$env:AI_CAM_AUTH_USERNAME = "<operator-login>"
$env:AI_CAM_AUTH_PASSWORD = "<strong-random-password>"
```

Placeholder auth credential bilan haqiqiy uskuna rejimida server ishga
tushmaydi. Bu qasddan qilingan — audit talabiga ko'ra "warning emas, blocker".

## 2. Login rate-limit + lockout

`backend/auth.py`: har bir client IP uchun ketma-ket xato urinishlar hisoblanadi
(`AUTH.max_failed_attempts`, standart 5). Shu miqdordan oshsa, IP
`AUTH.lockout_seconds` (standart 300s) davomida bloklanadi — login endpoint
`429 Too Many Requests` + `Retry-After` header qaytaradi. Muvaffaqiyatli login
hisoblagichni nolga tushiradi.

## 3. Rol modeli (admin / operator)

`AuthConfig.operator_username` / `operator_password` (standart bo'sh — faqat
admin mavjud) ixtiyoriy ikkinchi hisobni belgilaydi. Log tozalash kabi
yuqori-ta'sirli amallar **faqat admin** rolига ruxsat etiladi. `AUTH.enabled=False`
holatida (auth butunlay o'chirilgan) barcha so'rovlar "admin" sifatida
ko'riladi — bu mavjud, hujjatlashtirilgan "ochiq tizim" xatti-harakati
(pastga qarang, 8-band).

## 4. Sessiya cookie va CSRF

* Sessiya cookie (`ai_cam_session`): `HttpOnly`, `SameSite=Lax`,
  `Secure=AUTH.cookie_secure` (standart `False` — LAN'da odatda TLS yo'q;
  TLS terminatsiya qiluvchi reverse-proxy ortida `true` qiling).
  TTL: `AUTH.session_ttl_sec`.
* CSRF cookie (`ai_cam_csrf`): **HttpOnly EMAS** (JS `document.cookie` orqali
  o'qiydi) — double-submit pattern. Holat o'zgartiruvchi so'rovlar
  (`POST /api/settings`, `DELETE /api/logs/clear`, `POST /api/retention/run`)
  `X-CSRF-Token` header'ini kutadi va sessiyaga bog'langan CSRF qiymati bilan
  timing-safe solishtiradi (`auth.validate_csrf`).
* Logout (`/logout`) sessiyani serverda DARHOL bekor qiladi (`auth.destroy`)
  va ikkala cookie'ni ham o'chiradi.

## 5. `/api/settings` — secret masking

`GET /api/settings` endi HECH QACHON plaintext secret qaytarmaydi — quyidagi
maydonlar `"********"` bilan almashtiriladi (`backend/server.py`,
`_mask_settings_dict` / `_mask_raw_yaml`):

* `camera.password`
* `rfid.username`, `rfid.password`
* `auth.password`
* generic: nomi `password/passwd/pwd/secret/token/api_key/apikey` bo'lgan
  har qanday maydon (defense-in-depth).

**Xom YAML** (`raw` maydon, Raw YAML tab uchun) ham xuddi shu qoidalar bilan
maskalanadi — izohlar va formatlash saqlanadi, faqat qiymat almashtiriladi.

**Saqlashda buzilmaslik kafolati:** `POST /api/settings` qabul qilgan
ma'lumotni saqlashdan OLDIN diskdagi joriy qiymatlar bilan solishtiradi — agar
biror secret maydon `"********"` (mask sentinel) bo'lsa, u FOYDALANUVCHI
o'zgartirmagan deb hisoblanadi va diskdagi ASL qiymat bilan almashtiriladi
(`_unmask_settings_dict` / `_unmask_raw_yaml`). Yangi (mask bo'lmagan) qiymat
yuborilsa — u saqlanadi. Bu logikaning to'g'riligi
`tests/test_settings_secret_masking.py` da sinaladi.

`settings_store.py` bu agentning tahrirlash doirasiga kirmaydi (boshqa agent
fayli) — shuning uchun masking/unmasking butunlay `backend/server.py`
darajasida amalga oshirilgan.

## 6. Log sanitization — markazlashgan sanitizer

`backend/logger.py::_SecretSanitizer` — logger darajasidagi `Filter`, barcha
handlerlardan OLDIN ishlaydi. Ikki qatlam:

1. **Kalit-qiymat naqshlari**: `password=`, `token:`, `api_key=`,
   `Authorization: <scheme> <token>`, `Cookie: <value>` — qiymat `***` bilan
   almashtiriladi.
2. **Dinamik aniq-qiymat almashtirish**: joriy `CAMERA.password`,
   `RFID.password`, `AUTH.password` (config'dan reload'ga chidamli o'qiladi)
   — bu qiymatlar log matnining QAYERIDA bo'lishidan qat'i nazar (masalan
   `sMN CheckPassword 3 A89A6E74` kabi tag'siz format) `***` bilan
   almashtiriladi.

Bu **camera_client.py, rfid/*, auth.py, server.py kabi barcha chaqiruvchi
modullarni tuzatishga hojat qoldirmaydi** — arxitekturaviy jihatdan to'g'ri,
markazlashgan yechim (audit talabiga muvofiq).

### Mavjud loglardagi allaqachon yozilgan secret — operator tozalash ko'rsatmasi

`logs/ai_cam.log` faylida (audit topilmasi: qator ~2422 atrofida va
boshqa joylarda) kamera CoLa paroli (`A89A6E74`) ochiq matn holida allaqachon
yozib qo'yilgan — bu **yangi sanitizer BILAN HAM tuzatilmaydi**, chunki u
FAQAT yangi log yozuvlariga ta'sir qiladi. Bu agent, ko'rsatmaga muvofiq,
mavjud log fayllarni O'CHIRMADI (ular audit dalili). Operator quyidagi
choralarni ko'rishi kerak:

1. **Kamera parolini almashtiring** (`config/settings.yaml` -> `camera.password`)
   — eski qiymat allaqachon log fayllarida ochiq turibdi, shuning uchun
   "compromised" deb hisoblanishi kerak.
2. Eski log fayllarni (`logs/ai_cam.log`, rotatsiya backuplari
   `logs/*.log.1`, `.2`, ...) audit siyosatingizga muvofiq **xavfsiz** joyga
   arxivlang (kirish cheklangan, shifrlangan saqlash) yoki muddati o'tgach
   (retention siyosatiga ko'ra, pastga qarang) rejalashtirilgan tarzda
   o'chiring/rotatsiya qiling.
3. Kelajakda yangi yoziladigan barcha loglar avtomatik sanitizatsiya
   qilinadi (`_SecretSanitizer`) — qo'shimcha amal talab qilinmaydi.

### MUHIM — bu sessiyada yuz bergan hodisa (operator/lead e'tiboriga)

Ushbu remediation ishi davomida `tests/test_log_clear_authz.py` dagi (keyinchalik
tuzatilgan) bir test **HAQIQIY** `DELETE /api/logs/clear` endpoint orqali
`clear_files()` ni chaqirib yubordi va natijada real
`logs/system.log`, `logs/plc.log`, `logs/rfid.log`, `logs/ocr.log`,
`logs/errors.log`, `logs/camera.log` fayllari TRUNCATE bo'ldi (soniya:
2026-07-16 14:42:15, worktree local vaqti). `logs/ai_cam.log` (audit
topilmasidagi kamera-parol dalili saqlangan asosiy tarixiy fayl) BUZILMADI.
Test darhol `clear_files()` ni mock qilib tuzatildi (endi hech qanday test
haqiqiy log fayllariga tegmaydi). Loyihaning qat'iy cheklovlariga ko'ra bu
agent `git checkout`/`git reset` orqali fayllarni tiklay OLMAYDI — agar
git tarixidan tiklash kerak bo'lsa (`git checkout -- logs/system.log ...`),
bu qaror **lead**ga tegishli.

## 7. Path traversal — `/crops/{name}`

`backend/server.py::get_crop()` endi:

1. Fayl nomini qat'iy regex bilan tekshiradi (`^[A-Za-z0-9_.-]+$`) — `..`,
   `/`, `\` mutlaqo rad etiladi (Windows uslubidagi `..\` ham).
2. Kengaytma allowlist: faqat `.jpg`, `.jpeg`, `.png`.
3. Yakuniy resolved yo'lni `CROPS_DIR` ichida ekanligini `Path.resolve()` +
   `relative_to()` bilan tasdiqlaydi (himoya qatlami — hatto regex
   o'tkazib yuborgan taqdirda ham).

Sinovlar: `tests/test_crop_path_security.py` — `../`, Windows `..\`,
absolyut yo'l, ruxsat etilmagan kengaytma, mavjud/mavjud bo'lmagan fayl.

## 8. `/api/logs/clear` — audit-wipe

Endi: (a) faqat `role == "admin"` (yoki `AUTH.enabled=False` — mavjud "ochiq
tizim" xatti-harakati, pastga qarang), (b) CSRF tokeni majburiy, (c) tozalashdan
KEYIN audit yozuvi (`[API] AUDIT: System logs cleared by user=... role=...
ip=...`) — bu yozuv aynan shu tozalashdan KEYIN yoziladi, shunda u yangi
(bo'sh) log faylining birinchi yozuvi bo'lib qoladi va yo'qolmaydi.

## 9. `AUTH.enabled=False` — hujjatlashtirilgan cheklov (TUZATILMAGAN)

Agar operator autentifikatsiyani butunlay o'chirsa (`auth.enabled: false`),
barcha `/api/*` endpointlar (shu jumladan `/api/settings`, `/api/logs/clear`,
`/api/retention/run`) HECH QANDAY login talab qilmasdan ochiq bo'lib qoladi —
bu audit tomonidan qayd etilgan, lekin ushbu remediation doirasida TO'LIQ
qayta arxitektura qilinmagan (masalan API-key-asosli "auth-less" muhit uchun
alohida himoya qatlami). Sabab: bu katta arxitekturaviy o'zgarish (barcha
endpointlar uchun muqobil avtorizatsiya modeli talab qiladi) va joriy topshiriq
doirasidan tashqari. Operatorlarga tavsiya: `auth.enabled=false` faqat to'liq
izolyatsiya qilingan (tarmoqsiz) dev muhitda ishlatilsin.

## 10. `/health` — ommaviy liveness

`GET /health` autentifikatsiyasiz ochiq (`_PUBLIC_PATHS`), lekin FAQAT
`{"status": "ok", "service": "ai_cam", "time": <epoch>}` qaytaradi — hech
qanday IP, parol, holat (camera/PLC/RFID) ma'lumoti YO'Q. Barcha boshqa
`/api/status`, `/api/plc/status`, `/api/rfid/status` va h.k. avvalgidek
auth middleware ortida qoladi.

## 11. Observability

`GET /api/metrics` — quyidagi minimal metrikalarni ko'rsatishga harakat
qiladi: `active_session, active_session_id, pending_trigger_count,
dropped_trigger_count, session_mismatch_count, late_ocr_result_count,
late_rfid_result_count, duplicate_finalize_attempt_count,
camera_last_frame_age, camera_reconnect_count, plc_ack_timeout_count,
database_write_failure_count`.

Bu agent FAQAT ko'rsatish/endpoint qatlamiga javobgar — asosiy metrikalarning
ko'pi `backend/pipeline.py::status()` ga Agent 1/2 tomonidan parallel
qo'shilmoqda. `_extract_metrics()` DEFENSIVE: maydon topilmasa `None`
qaytaradi (hech qachon crash qilmaydi), va ba'zilarini (`active_session`,
`active_session_id`, `pending_trigger_count`, `camera_last_frame_age`)
ALLAQACHON mavjud `pipeline.status()` maydonlaridan (`session.active`,
`session.id`, `last_frame_ts`) hisoblab chiqaradi.

`backend/logger.py` strukturaviy kontekstni qo'llab-quvvatlaydi:
`log.info(msg, extra={"session_id": ..., "trigger_sequence": ...,
"component": ..., "event": ...})` — mavjud (kontekstsiz) chaqiruvlar
o'zgarishsiz ishlayveradi (shovqin qo'shilmaydi); yangi/kelajakdagi chaqiruvlar
bu maydonlarni to'ldirsa, ular log qatorining oxirida (`| session=.. seq=..
comp=.. event=..`) va `ui_handler` JSON yozuvida (`/api/logs/*` orqali)
ko'rinadi.

## 12. Kamera hodisalari `camera.log` ga tushmasligi (P3)

`backend/pipeline.py` da ko'p kamera-bog'liq xabarlar `[TAG]` prefiksisiz
keladi (masalan "Kamera uzildi", "Frame timeout..."), shuning uchun avvalgi
`_MODULE_SOURCE` (aniq modul nomi -> manba) xaritasi ularni "SYSTEM" ga
tushirib yuborardi. `backend/logger.py::_MODULE_KEYWORD_SOURCE` — `pipeline`
moduli uchun ikkinchi darajali (TAG bo'lmaganda) kalit-so'z asosidagi
tasniflash qo'shildi (kamera/kadr/stream -> CAMERA, ocr/yolo/crop/trigger ->
OCR, va h.k.). Natijada tegishli kamera xabarlari endi `logs/camera.log`
ga ham tushadi.

## 13. Retention (crops / dataset / logs)

`RetentionConfig` (`backend/config.py`, `config/settings.yaml` -> `retention:`)
— **standart xavfsiz**: `enabled: false`, `dry_run: true`. Hech narsa avtomatik
o'chirilmaydi.

* `GET /api/retention/preview` — dry-run hisobot (HAQIQIY `data/crops` va
  `dataset/collected_raw` ni skanerlaydi, lekin HECH NARSA o'chirmaydi).
* `POST /api/retention/run` — haqiqiy ishga tushirish. Faqat: (a)
  `RETENTION.enabled=true` bo'lsa, (b) `role=="admin"`, (c) CSRF tokeni bilan.
  `RETENTION.dry_run` hali ham `true` bo'lsa — baribir faqat hisobot beradi.

Xavfsizlik qatlamlari (`backend/server.py::_dir_retention_scan` /
`apply_retention`):
* Faqat berilgan `root` ICHIDAGI fayllar ko'rib chiqiladi (`resolve()` +
  `relative_to()` bilan ikki marta tasdiqlanadi — skanerlashda VA
  o'chirishda).
* `active_grace_minutes` (standart 15) dan yosh fayllarga HECH QACHON
  tegilmaydi (faol foydalanishda bo'lishi mumkin).
* Yosh (`max_age_days`) VA jami hajm (`max_total_mb`) chegaralari — eng
  eski fayllar birinchi navbatda nomzod bo'ladi.
* Har bir ishga tushirish `log.info("[RETENTION] ...")` bilan qayd etiladi.

**Sinovlar** (`tests/test_retention.py`) FAQAT `tmp_path` ichidagi soxta
rootlarda ishlaydi — haqiqiy `data/crops` yoki `dataset/collected_raw` ga
HECH QACHON tegilmagan (faqat `GET /api/retention/preview` orqali READ-ONLY
skaner haqiqiy papkalarga ruxsat etiladi, bu ham hech narsani o'chirmaydi).

## 14. Operator UI — stale VIN muammosi (dashboard.js / dashboard.html)

Avval: `dashboard.js` global `ocr.last_vin` ni "Last VIN" pannelida
ko'rsatardi va buni JORIY holat sifatida talqin qilish mumkin edi — sessiya
TIMEOUT bo'lsa ham eski VIN qiymati o'zgarmasdan qolardi (`pipeline.py:317-319`),
va `dashboard.js` `session.*` maydonlarini UMUMAN o'qimasdi.

Endi `dashboard.js`:
* `/api/status` javobidagi `session.*` (mavjud kontrakt: `active`, `id`,
  `remaining_sec`, `vin_done`, `rfid_done` — `pipeline.py:649-673`) dan
  **"Joriy sessiya"** kartasini to'ldiradi: sessiya ID, holat (Idle/Waiting/
  Completing/mavjud bo'lsa `session.state`), qolgan vaqt, VIN/EPC bayrog'i.
* Global `ocr.last_vin` / `ocr.last_conf` endi ALOHIDA, aniq belgilangan
  **"Last completed session"** pannelida ko'rsatiladi — u hech qachon "joriy"
  deb da'vo qilinmaydi.
* Kamera "stale" holati (`camera_last_frame_age`) va kutilayotgan trigger
  soni (`pending_trigger_count`, mavjud bo'lsa) DEFENSIVE ko'rsatiladi —
  maydon yo'q bo'lsa UI buzilmaydi ("—" ko'rsatiladi).

Bu o'zgarish `backend/pipeline.py` ga TEGMAGAN HOLDA amalga oshirilgan —
faqat mavjud `session.*` kontraktidan foydalanish orqali.
