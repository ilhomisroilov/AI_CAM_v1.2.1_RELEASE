"""
============================================================
config.py  —  AI_CAM markaziy sozlamalari
============================================================
Barcha o'zgaruvchan parametrlar shu yerda. Ishlab chiqarishda
faqat shu faylni tahrirlash kifoya (IP, portlar, chegaralar).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- Loyiha papka manzillari (portable, cwd-independent) ---
if getattr(sys, "frozen", False):
    # PyInstaller ONEDIR: writable/configurable release content is beside AI_CAM.exe,
    # not below the private extraction/import directory.
    _DEFAULT_PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    _DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _configured_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser().resolve() if value else default.resolve()


PROJECT_ROOT = _configured_path("AI_CAM_PROJECT_ROOT", _DEFAULT_PROJECT_ROOT)
BASE_DIR = PROJECT_ROOT                                  # backward-compatible alias
MODELS_DIR = PROJECT_ROOT / "models"
# Windows/source default remains repository-local. Ubuntu service deployments set
# AI_CAM_DATA_ROOT=/var/lib/ai-cam so persistent data never enters the checkout.
RUNTIME_DIR = _configured_path("AI_CAM_DATA_ROOT", PROJECT_ROOT / "runtime")
DATA_DIR = RUNTIME_DIR / "data"
CROPS_DIR = RUNTIME_DIR / "crops"                       # kesilgan VIN rasmlari
LOGS_DIR = _configured_path("AI_CAM_LOG_ROOT", RUNTIME_DIR / "logs")
TEMP_DIR = RUNTIME_DIR / "temp"
ENGRAVED_COLLECTION_DIR = RUNTIME_DIR / "engraved_ocr_collection"
BACKUPS_DIR = RUNTIME_DIR / "backups"
DB_PATH = DATA_DIR / "ai_cam.db"
# Avtomatik dataset yig'ish papkasi (Data Loop)
DATASET_DIR = RUNTIME_DIR / "dataset_collected"
# O'qitilgan YOLOv8n model — loyihaga tegishli, models/ ichida (self-contained).
TRAINED_MODEL_PATH = MODELS_DIR / "yolo" / "best.pt"
# Self-contained PaddleOCR model papkalari (foydalanuvchi-profil keshiga bog'liq EMAS).
PADDLE_DET_DIR = MODELS_DIR / "paddle" / "det"
PADDLE_REC_DIR = MODELS_DIR / "paddle" / "rec"
PADDLE_CLS_DIR = MODELS_DIR / "paddle" / "cls"

# Papkalar mavjudligini ta'minlash (runtime yozish uchun)
for _d in (DATA_DIR, CROPS_DIR, LOGS_DIR, TEMP_DIR, ENGRAVED_COLLECTION_DIR,
           BACKUPS_DIR, MODELS_DIR, DATASET_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@dataclass
class CameraConfig:
    """SICK LECTOR652 ulanish sozlamalari (lector652 loyihasidan)."""
    # Eslatma: IP endi hardcode QILINMAYDI. Standart qiymat faqat
    # UI dagi input maydoni uchun (foydalanuvchi /dashboard da o'zgartiradi).
    ip: str = "192.0.2.10"
    # CoLa-A (ASCII) control porti. Lector 65x: 2111=CoLa-A, 2112=CoLa-B
    cola_port: int = 2111
    # BLOB (rasm) streaming porti — foydalanuvchi talabi bo'yicha 2113 ga
    # qat'iy bog'langan (autodetect O'CHIRILGAN).
    blob_port: int = 2113
    # False -> GetBlobClientConfig bilan port almashtirilmaydi, doim 2113.
    blob_autodetect: bool = False
    # CoLa-A CheckPassword (ishlaydigan lector652 koddan). Ba'zi kameralar
    # talab qilmaydi — xato bo'lsa ulanish baribir davom etadi.
    password: str = "CHANGE_ME"

    recv_timeout: float = 3.0          # socket recv timeout (s)
    reconnect_delay: float = 2.0       # uzilganda qayta ulanish kechikishi (s)
    # Trigger pulse: gateon -> kutish -> gateoff
    trigger_pulse_off_delay: float = 0.15   # 150 ms (Wireshark dagidek)
    capture_timeout: float = 5.0       # rasm kelishini kutish (s)
    # Kadr xom (original) holatda qayta ishlanadi — orientatsiya allaqachon to'g'ri.

    # Kamera watchdog/reconnect contracti. Joriy decoupled capture pipeline bu
    # qiymatlarni sog'liq va qayta-ulanish siyosatida ishlatadi; test/HIL ham
    # shu yagona config obyektini boshqaradi.
    stale_after_sec: float = 3.0
    max_consecutive_timeouts: int = 5
    reconnect_initial_delay_sec: float = 1.0
    reconnect_max_delay_sec: float = 30.0
    reconnect_max_attempts: int = 0     # 0 = cheksiz, bounded backoff bilan


@dataclass
class DetectionConfig:
    """YOLOv8n plastinka aniqlash sozlamalari."""
    # O'qitilgan model: runs/detect/dataset-2/weights/best.pt
    model_path: str = str(TRAINED_MODEL_PATH)
    # Past chegara: dataset yig'ish uchun nomzod box'larni ham ko'rsatadi (>0.40).
    conf_threshold: float = 0.40       # YOLO bazaviy aniqlash chegarasi (dataset collect)
    iou_threshold: float = 0.45
    # OCR FAQAT shu ishonchdan yuqorida ishga tushadi — 0.90 (90%)
    ocr_trigger_conf: float = 0.90
    # "auto" -> NVIDIA GPU bo'lsa cuda:0, aks holda cpu. Yoki "cuda:0"/"cpu".
    device: str = "auto"
    crop_padding: int = 8              # ROI atrofiga qo'shimcha piksel

    # --- Single-shot trigger (debounce / state-lock) ---
    # Bitta plastinka kadrda bir necha kadr turishi mumkin — OCR FAQAT BIR MARTA
    # ishga tushadi. Plastinka kadrdan ketib (shu qadar kadr ko'rinmay) qaytsa,
    # yangi avtomobil sifatida qayta trigger bo'ladi.
    ocr_rearm_absent_frames: int = 3   # plastinka shu qadar kadr ko'rinmasa qayta yoqiladi
    ocr_cooldown_sec: float = 2.0      # ketma-ket triggerlar orasidagi minimal vaqt (xavfsizlik)
    # Bitta sessiyada OCR ni qayta ishga tushirish chegarasi (OCR fail bo'lsa qulf
    # ochilib qayta urinadi, lekin cheksiz emas — shu son bilan cheklangan).
    ocr_session_max_triggers: int = 12

    # --- Multi-frame fusion + gating ---
    # OCR faqat plastinka shu qadar YUQORI-ISHONCH (>=0.95) kadrida ko'ringach
    # ishga tushadi (2-frame tasdiqlash). Har kadrda crop sifati baholanib
    # buferga yig'iladi; ENG YAXSHI fusion_k crop OCR ga (ovoz berish) yuboriladi.
    ocr_confirm_frames: int = 2        # OCR dan oldin kerakli yuqori-ishonch kadrlar
    # Plastinka faqat bitta yuqori-ishonch kadrda ko'rinib keyin chiqib ketsa,
    # o'sha best cropni xavfsiz fusion gate'ga yuboramiz (OCRsiz yo'qotmaymiz).
    ocr_submit_on_exit: bool = True
    ocr_crop_margin_frac: float = 0.15  # ROI ni shu nisbatda kengaytirish (belgilar kesilmasin)
    min_crop_quality: float = 0.45     # eng yaxshi crop sifati shundan past bo'lsa — kutamiz
    event_buffer_max: int = 10         # bitta hodisa uchun saqlanadigan eng yaxshi croplar soni
    fusion_k: int = 3                  # OCR ovoz berishga yuboriladigan top croplar soni
    # v1.3.0 STRICT OCR-submit gate: best_q (crop sifati) YOLO ishonchini ALMASHTIRMAYDI.
    # Sessiyaning eng yuqori YOLO ishonchi shu qiymatdan past bo'lsa OCR YUBORILMAYDI.
    ocr_submit_min_yolo_conf: float = 0.90
    stable_bbox_iou: float = 0.70      # ketma-ket stabil detection uchun minimal bbox IoU

    # --- Avtomatik dataset yig'ish (Data Loop) ---
    collect_enabled: bool = True
    collect_conf: float = 0.40         # shu conf dan yuqori aniqlovlar saqlanadi
    collect_min_interval_sec: float = 1.0   # throttle: ~1 rasm/sekund (FPS himoyasi)
    collect_class_id: int = 0          # YOLO yorliq sinfi (0 = vin_plate)


@dataclass
class OCRConfig:
    """PaddleOCR sozlamalari (engraved/etched metal VIN uchun, aniqlik + tezlik)."""
    lang: str = "en"
    # True -> PaddleOCR GPU (NVIDIA/CUDA) bilan ishlaydi. Ubuntu+NVIDIA serverda
    # tavsiya etiladi. CUDA topilmasa avtomatik CPU ga qaytadi (xavfsiz).
    use_gpu: bool = True

    # --- Preprocess (aniqlik uchun) ---
    apply_clahe: bool = True           # yengil CLAHE (kontrast)
    clahe_clip: float = 3.0
    clahe_grid: int = 8
    upscale_height: int = 96           # crop ni shu balandlikka kattalashtirish
                                       # (0 = o'chiq). Kichik etched belgilar uchun muhim.
    sharpen: bool = True               # yengil unsharp mask (chekka aniqligi)

    # --- PaddleOCR parametrlari ---
    # paddle_det -> FALLBACK detektor+recognizer (raw/original croplar uchun).
    #   Standart rejim endi REC-ONLY (paddle_det=False): YOLO bergan tor VIN crop
    #   allaqachon bitta matn qatori — detektor keraksiz ish qiladi VA native crash
    #   xavfini oshiradi. Shu sabab productionда rec-only + preprocessing + consensus.
    #   det=True FAQAT rec-only bo'sh natija bergan raw croplar uchun ishlatiladi.
    paddle_det: bool = True            # PRIMARY: det+rec (eski ishlagan yo'l; crash izolyatsiya bilan ushlanadi)
    det_fallback_enabled: bool = False  # det primary bo'lsa fallback kerak emas
    use_angle_cls: bool = False        # burchak klassifikatori (metall matn tik -> o'chiq, tez)
    drop_score: float = 0.30           # PaddleOCR shu balldan past natijalarni tashlaydi
    rec_batch_num: int = 6             # recognizer batch (tezlik)
    det_limit_side_len: int = 960      # detektor maksimal tomon uzunligi (det rejimida)

    # --- Ishonch chegaralari ---
    min_char_confidence: float = 0.30  # har bir bo'lak (fragment) uchun minimal ishonch
    min_confidence: float = 0.45       # umumiy (o'rtacha) ishonch chegarasi

    # --- Denoise (engraved metal uchun) ---
    bilateral: bool = True             # chekkalarni saqlab shovqinni kamaytirish
    bilateral_d: int = 5
    bilateral_sigma: float = 50.0

    # --- Preprocessing VARIANTLAR (aniqlik uchun OCR task diversitysi) ---
    # Har crop uchun bir nechta NOMLI variant OCR ga yuboriladi va natijalar
    # position-level consensus bilan birlashtiriladi. Variantlar engine ichida
    # HARDCODE QILINMAYDI — ular alohida OCR tasklari (audit uchun variant_name).
    # Mavjud variantlar: raw_resized, clahe_unsharp, blackhat_relief,
    #   tophat_relief, adaptive_binary, inverted_clahe.
    variants_enabled: tuple = ("raw_resized", "clahe_unsharp", "blackhat_relief")
    # Deskew + kichik burilish variantlari (gradus). 0 = deskew qilingan asl.
    variant_rotations: tuple = (0.0, -3.0, 3.0)
    variant_deskew: bool = True        # minAreaRect deskew variantini ham qo'shish

    # --- Retry + fusion (fail handling) ---
    retry_enabled: bool = True         # variant/burilish tasklarini yaratish
    retry_rotations: tuple = (-7.0, 7.0)   # (legacy) — variant_rotations bilan almashtirildi
    # Bitta hodisa uchun jami OCR tasklari chegarasi. Asl croplar + deskew/filter/burilish
    # variantlar HAQIQATAN ishlashi uchun yetarlicha katta bo'lishi kerak (fusion_k=4
    # crop × bir nechta variant). 5 juda kichik edi (faqat asl croplar sig'ardi).
    max_ocr_attempts: int = 10

    # --- PROCESS IZOLYATSIYASI (native SIGSEGV himoyasi) ---
    # PaddleOCR native (C++/MKLDNN/oneDNN) crash Python try/except bilan tutilmaydi.
    # Yechim: OCR inference ALOHIDA PROCESS(lar)da ishlaydi. Worker o'lsa (core dump),
    # asosiy ilova (run.py) TIRIK qoladi va worker AVTOMATIK qayta yaratiladi.
    # parallel_engines = process worker soni (Python thread EMAS). 1 = eng barqaror.
    # 2 -> faqat process-izolyatsiyalangan workerlar (har biri mustaqil crash zonasi).
    parallel_engines: int = 1
    worker_task_timeout_sec: float = 12.0  # bitta variant OCR tasklari uchun timeout (s)
    worker_max_restarts: int = 50      # o'lgan workerlarni qayta yaratish chegarasi (umr bo'yi)
    worker_start_timeout_sec: float = 60.0  # 1-marta PaddleOCR model yuklash uchun budjet (s)
    # CPU OCR: MKLDNN/oneDNN parallel CPU engineda 'could not execute a primitive'
    # native crash sababchisi. PRODUCTIONDA O'CHIQ (false). RuntimeError shu matnni
    # o'z ichiga olsa, worker MKLDNN'siz bir marta qayta urinadi.
    enable_mkldnn: bool = False        # PRODUCTION DEFAULT: barqarorlik uchun O'CHIQ
    cpu_threads: int = 1               # har worker uchun thread (1 = SIGSEGV xavfi minimal)

    # --- Tezlik / lag nazorati ---
    queue_maxsize: int = 50            # OCR navbati hajmi (orqaga bosim oldini olish)
    min_interval_sec: float = 0.0      # ketma-ket OCR yugurishlari orasidagi minimal
                                       # vaqt (0 = o'chiq; navbat allaqachon cheklaydi)

    # Anti-duplicate: bir xil VIN shu oraliqda qayta yozilmaydi
    duplicate_window_sec: float = 90.0


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    mjpeg_fps: int = 25                # live stream maksimal FPS
    jpeg_quality: int = 80             # MJPEG kodlash sifati


@dataclass
class OperationsConfig:
    """24/7 host controls shared by Windows and systemd deployments."""

    timezone: str = "Asia/Tashkent"
    disk_warning_free_gb: float = 10.0
    disk_critical_free_gb: float = 2.0
    crop_retention_max: int = 5000
    collector_retention_days: int = 0   # 0 = never delete review evidence automatically
    backup_retention_days: int = 30
    max_expected_child_processes: int = 8
    health_preflight_timeout_sec: float = 1.0
    file_logging_enabled: bool = True


@dataclass
class VINConfig:
    """Model-aware VIN post-processing (QY / BL7M strukturasi)."""
    enabled: bool = True
    # YOLO 2-sinfli bo'lmaguncha model shu qiymatdan olinadi.
    default_model: str = "QY"
    # YOLO sinf nomi -> model. (vin_plate kabi nomlar default_model ga tushadi.)
    class_to_model: dict = field(default_factory=lambda: {"QY": "QY", "BL7M": "BL7M"})
    # Saralash og'irliklari
    confusion_prior: float = 0.6       # chalkashlik variantining vizual ulushi
    w_visual: float = 1.0
    struct_bonus: float = 0.5          # strukturaga mos belgi uchun bonus (DOIMIY pozitsiyalar)
    struct_penalty: float = 0.5        # mos kelmaganga jazo (NST/raqam uchun og'ir)
    # O'ZGARUVCHAN pozitsiyalar (ayniqsa pos5) uchun KAMAYTIRILGAN struktura bonusi:
    # struktura ruxsat bergani UCHUNGINA belgi tanlanib qolmasin — rasm dalili kerak.
    struct_bonus_variable: float = 0.15
    # Qabul chegarasi (validated VIN final_score shundan past bo'lsa rad etiladi).
    # min_final_score endi HAQIQIY YAKUNIY GATE (faqat ranking emas).
    min_final_score: float = 0.55
    # --- GLOBAL ACCEPT GATE (position-level consensus fusiondan keyin) ---
    accept_final_score: float = 0.80   # real o'qish uchun bo'sag'a (0.92 juda qattiq edi)
    accept_min_crops: int = 2          # production: kamida 2 mustaqil frame dalili
    accept_multi_variant_min: int = 3  # yoki shuncha turli variant kuchli consensus bersa
    accept_risky_margin: float = 0.20  # faqat HAQIQIY ziddiyatda (C/D teng ovoz) rad etadi
    accept_raw_support_ratio: float = 0.50  # tanlangan belgilar xom OCR bilan qo'llab-quvvatlanishi
    # Bitta yuqori-confidence o'qish ham xato yil/serial belgisini (masalan real
    # BL7M U ni V) ishonch bilan berishi mumkin. Productionda fail-closed; faqat
    # HIL/legacy tajribada ongli ravishda yoqiladi.
    accept_single_read: bool = False
    accept_single_min_score: float = 0.80
    # --- POSITION 5 HARD GATE (eng xavfli belgi: C/D/B/G/H/J) ---
    pos5_min_crops: int = 2            # pos5 belgisi kamida shuncha mustaqil cropда ko'rinishi
    pos5_min_variants: int = 3         # YOKI shuncha turli preprocessing variantда
    pos5_min_margin: float = 0.20      # top-belgi va 2-belgi orasidagi minimal margin
    # Xom OCR shu belgilarni ko'rsa (0/3/6/8/R) va tizim strukturaga qarab D/B/G ga
    # aylantirsa — pos5 AMBIGUOUS deb rad etiladi (struktura-faqat tuzatishga yo'l yo'q).
    pos5_reject_structure_only: bool = True
    # Jonli liniya rescue: Paddle pos5 ni 3/8 deb ko'rsa, lekin umumiy VIN juda kuchli
    # va to'liq strukturaga mos bo'lsa B sifatida qabul qilishga ruxsat. P/0/6/R kabi
    # xavfli holatlar default rescue qilinmaydi.
    pos5_structure_rescue_enabled: bool = True
    pos5_structure_rescue_raw_chars: str = "38"
    pos5_structure_rescue_min_score: float = 0.90
    pos5_structure_rescue_min_raw_support: float = 0.85
    # Pos5 bilan cheklanmaydi: serial taildagi 6/8 kabi ziddiyatlar ham false
    # accept manbai. Strict raw/CLAHE to'liq VIN conflict har doim ambiguous.
    accept_variable_margin: float = 0.08
    accept_serial_margin: float = 0.12
    exact_conflict_ratio: float = 0.55
    exact_rescue_min_conf: float = 0.74
    independent_crop_hash_distance: int = 5
    # Production: model (QY/BL7M) aniqlanmagan (model=None) VIN QABUL QILINMAYDI.
    # Bu HSTFC... kabi qoidaga mos kelmaydigan VINlarni saqlanishdan to'xtatadi.
    # False -> generic 17-belgi VIN ham qabul qilinadi (eski xatti-harakat).
    require_known_model: bool = True
    # STANDART: model-mustaqil DOIMIY pozitsiyalar (1=N, 2=S, 3=T, 7=1, 11=J) har
    # doim shu belgi. True -> model aniqlanganда OCR boshqa belgi o'qisa ham majburan
    # standart belgi qo'yiladi (validated VIN standartga to'liq mos bo'ladi). Har bir
    # bunday majburiy tuzatish audit ma'lumotida changed=true bo'lib ko'rinadi.
    enforce_fixed_positions: bool = True


@dataclass
class Pos5VerifierConfig:
    """
    Optional visual verifier for VIN position 5 (C/D/B/G/H/J) — Section 8.
    Ikkinchi bosqich: VIN matn bandini 17 katakka bo'lib, pozitsiya-5 belgi
    cropini alohida model (HOG+SVM yoki kichik CNN) bilan tasdiqlaydi. Verifier
    ishonchi past bo'lsa VIN OCR_AMBIGUOUS deb rad etiladi. Model o'qitilmaguncha
    O'CHIQ (enabled=false) — ishlab chiqarish oqimiga ta'sir qilmaydi.
    """
    enabled: bool = False              # model o'qitilgach true qiling
    model_path: str = ""               # HOG+SVM (.joblib) yoki CNN (.onnx/.pt) yo'li
    backend: str = "hog_svm"           # "hog_svm" | "cnn"
    min_confidence: float = 0.60       # shundan past -> OCR_AMBIGUOUS
    save_char_crops: bool = False      # pos5 katak croplarini datasetga saqlash (training uchun)
    char_crops_dir: str = ""           # bo'sh -> DATA_DIR/pos5_crops


@dataclass
class VinSlotRecognizerConfig:
    """
    Fixed-17-position VIN recognizer. It is trained specifically for this line's
    VIN crops and can assist PaddleOCR when Paddle returns NO_READ/AMBIGUOUS.
    """
    enabled: bool = False               # pilot model validatsiyadan o'tmaguncha productiondan uzilgan
    mode: str = "shadow"               # "shadow" | "assist" | "enforce"
    model_path: str = str(MODELS_DIR / "vin_slot_recognizer_pilot" / "best.pt")
    device: str = "auto"               # "auto" | "cpu" | "cuda"
    accept_on_paddle_fail: bool = False
    min_avg_confidence: float = 0.80
    min_char_confidence: float = 0.35
    require_paddle_support: bool = True
    min_paddle_exact_reads: int = 1
    agree_bonus: float = 0.03
    reject_on_disagree: bool = False


@dataclass
class AuthConfig:
    """Yengil frontend autentifikatsiya (zavod ichki tarmog'i uchun)."""
    enabled: bool = True
    username: str = "admin"
    password: str = "CHANGE_ME"
    cookie_name: str = "ai_cam_session"
    session_ttl_sec: int = 12 * 3600    # 12 soat — keyin qayta login
    # Optional least-privilege account. Empty credentials disable operator login.
    operator_username: str = ""
    operator_password: str = ""
    max_failed_attempts: int = 5
    lockout_seconds: int = 300
    cookie_secure: bool = False          # reverse proxy/TLS bo'lsa True
    csrf_enabled: bool = True


@dataclass
class PLCConfig:
    """
    PLC bilan boshqariladigan ishlov berish (gateway_python uslubida).
    PLC bit signali = 1 -> ishlov berish boshlanadi; = 0 -> to'xtaydi.
    Modellar HAR DOIM xotirada qoladi — PLC faqat oqimni boshqaradi.
    """
    enabled: bool = False              # PLC boshqaruvi yoqilganmi
    mode: str = "simulator"       # "simulator" | "melsec" (haqiqiy Mitsubishi MC)
    ip: str = "192.0.2.20"        # operator productionda sozlaydi
    port: int = 5003                  # MC protokol porti
    plc_type: str = "Q"               # pymcprotocol plctype (Q/L/QnA/iQ-R...)
    signal_address: str = "D2222"   # yagona kuzatiladigan ARRIVE pulse registeri
    trigger_on_value: int = 1         # shu qiymat = "ishlov ber" (value/mask rejimida)
    # --- P1 FIX: WORD registerdan trigger holatini AJRATISH (bit/mask) ---
    # D521 kabi WORD register butun qiymat (mas. 8720) qaytaradi. ARRIVE signali
    # odatda word ichidagi BITTA bit. Butun word ni `==1` bilan solishtirish START/STOP
    # flapping ga olib keladi. signal_kind bilan to'g'ri talqin tanlang:
    #   "auto"  -> trigger_mask berilgan bo'lsa mask; signal_bit>=0 bo'lsa bit; aks holda value
    #   "value" -> butun qiymatni trigger_on_value bilan solishtirish (haqiqiy BIT qurilma uchun)
    #   "bit"   -> word ichidan signal_bit indeksli bitni olish (mas. D521.0)
    #   "mask"  -> (value & trigger_mask) == (trigger_on_value & trigger_mask)
    # MUHIM: D521 ning aniq ARRIVE bitini PLC muhandisi bilan tasdiqlang.
    signal_kind: str = "auto"
    signal_bit: int = -1              # word ichidagi bit indeksi (-1 = o'chiq). Mas. 0 -> D521.0
    trigger_mask: int = 0             # bitmask (0 = o'chiq). Mas. 0x0001
    # --- TRIGGER REJIMI: pulse vs level ---
    # "level" -> ARRIVE biti mashina turganда 1, ketganда 0. Signal 0 ga tushganда
    #            sessiya YOPILADI (mashina ketdi).
    # "pulse" -> ARRIVE qisqa puls (mas. 3s): mashina kelganда qisqa 1, so'ng 0.
    #            0 ga tushish sessiyani YOPMAYDI — sessiya VIN o'qilguncha yoki
    #            timeout_sec gacha davom etadi. Qisqa pulsli liniyalar uchun.
    trigger_mode: str = "pulse"       # "level" | "pulse"
    # Hardening compatibility aliases. New deployments should use
    # signal_kind/signal_bit above; trigger_kind/trigger_bit are retained so
    # older validation and HIL tooling can describe the same word-bit rule.
    trigger_kind: str | None = None  # None | "level" | "bit_or_mask"
    trigger_bit: int = 0
    # KAMERA KECHIKISHI (yagona sozlanadigan delay): puls kelgandan keyin RFID DARHOL
    # boshlanadi, lekin kameradan tasvir olish shuncha SONIYAdan keyin boshlanadi
    # (mashina sensorдан kameragacha yetib kelishi uchun). 0 = darhol.
    # Sessiya davomiyligi = camera_delay_sec + session.timeout_sec (capture oynasi).
    camera_delay_sec: float = 0.0
    # KAMERA QIDIRUV OYNASI: kamera yonganidan keyin YOLO plastinkani shuncha
    # soniya ichida topishi kerak. Agar plastinka bu vaqt ichida umuman
    # ko'rinmasa (YOLO hech qachon aniqlamasa), kamera MAJBURIY to'xtatiladi —
    # sessiya.timeout_sec (uzoq, RFID uchun) gacha befoyda ochiq turmaydi.
    # Plastinka topilib kadrdan chiqsa, kamera BUNDAN OLDINROQ ham to'xtaydi
    # (pause_camera_stream) — bu qiymat faqat "topilmadi" holati uchun tavon.
    camera_search_timeout_sec: float = 30.0

    # D2222 rising pulse yagona trigger: pipeline kamera va RFIDni birga boshlaydi.
    # --- P6 FIX: handshake (re-trigger bo'ronini to'xtatish) ---
    # VIN/sessiya tugagach PLC ga DONE bitini yozamiz; PLC ARRIVE ni 0 ga tushiradi.
    # done_address bo'sh bo'lsa handshake yozilmaydi (faqat lokal disarm ishlatiladi).
    done_address: str = ""            # mas. "D522" yoki "M520" (DONE/ACK biti)
    done_value: int = 1               # DONE ga yoziladigan qiymat
    done_clear_value: int = 0         # keyingi siklдан oldin DONE ni tozalash qiymati
    # Explicit write/ack contract. write_enabled=True preserves the current
    # done_address behavior; production settings should still leave
    # done_address empty until the controls owner approves the register.
    write_enabled: bool = True
    require_acknowledgement: bool = False
    ack_timeout_ms: int = 5_000
    startup_recovery_policy: str = "wait_for_edge"  # wait_for_edge | start_immediately
    # Optional legacy EXIT input. The current D2222-only production profile
    # leaves this empty; it exists for backward-compatible diagnostics only.
    exit_address: str = ""
    exit_on_value: int = 1
    # P6: sessiyadan keyin signal FIZIK 0 ga tushmaguncha qayta trigger bo'lmaydi.
    require_zero_before_rearm: bool = True
    poll_interval_ms: int = 100       # polling davri (P5: 500->100, qisqa pulslar uchun)
    reconnect_max_delay_sec: float = 30.0   # qayta ulanish backoff chegarasi
    # PLC = yagona kamera ON/OFF tugmasi. Signal 0 bo'lganda kamera UZILADI
    # (oqim to'xtaydi). True -> 0 = kamera o'chadi (to'liq idle). Modellar xotirada
    # qoladi (PLC faqat oqimni boshqaradi).
    full_stop_on_zero: bool = True
    # --- ONE-SHOT: VIN o'qilgach avtomatik o'chirish ---
    # True -> VIN muvaffaqiyatli o'qilgach signal 0 ga tushadi, kamera o'chadi
    #         (bitta trigger = bitta VIN). PLC qayta 1 bo'lганда yangi sikl boshlanadi.
    auto_off_on_vin: bool = True
    # VIN o'qilgach kamerani o'chirishdan oldingi kechikish (sekund). 0 = darhol.
    # Kechikishni yoqish uchun shu qiymatni > 0 qiling (masalan 1.5).
    off_delay_sec: float = 0.0

    # --- v1.3.0 D2222 forensic debounce + pulse-width audit ---
    # Rising edge faqat raw signal shu vaqt STABIL high bo'lsagina cycle boshlaydi;
    # 100 ms noise pulse filtrlanadi. Falling'da pulse width o'lchanadi va
    # [min,max] tashqarisidagi kenglik INVALID_PLC_PULSE deb audit qilinadi.
    plc_edge_audit_enabled: bool = True
    debounce_on_ms: float = 200.0
    debounce_off_ms: float = 200.0
    expected_pulse_min_ms: float = 2000.0
    expected_pulse_max_ms: float = 4500.0


@dataclass
class SessionConfig:
    """
    Ishlov berish sessiyasi (PLC=1 dan boshlab). Kamera (VIN) va RFID PARALLEL
    ishlaydi va ikkalasi muvaffaqiyatli bo'lguncha YOKI timeout ga yetguncha
    qayta urinadi. Timeout bo'lsa ham natija HAR DOIM bazaga yoziladi.
    """
    timeout_sec: float = 30.0          # D2222 qabul qilingandan keyingi qat'iy oyna
    # True bo'lsa natija erta tayyor bo'lsa ham sessiya deadline'gacha ochiq
    # qoladi. Production D2222 pulse oqimida bu 30 soniyalik qat'iy oynadir.
    hold_until_deadline: bool = True
    # RFID majburiymi? True -> SUCCESS uchun VIN VA RFID kerak. False -> VIN yetarli
    # (RFID bo'lsa yoziladi, bo'lmasa ham sessiya VIN bilan SUCCESS bo'ladi).
    require_rfid: bool = True
    # Faqat legacy hold_until_deadline=False rejimi uchun VIN-dan keyingi RFID grace.
    rfid_grace_sec: float = 15.0
    # Deadline ichida boshlangan OCR tugamagan bo'lsa natijani tashlab yubormaslik
    # uchun FAQAT in-flight jobga beriladigan cheklangan qo'shimcha oyna.
    ocr_inflight_grace_sec: float = 15.0

    # --- v1.3.0 body-cycle invariant: 1 body = 1 cycle = 1 DB record ---
    # Factory takt: ikki HAQIQIY kuzov orasida kamida 90 s bor. Shundan kamroq
    # oraliqda kelgan rising edge SUSPICIOUS_EARLY_TRIGGER (yangi session ochmaydi,
    # audit qilinadi). Faol cycle paytidagi trigger DUPLICATE_TRIGGER_IGNORED.
    minimum_body_interval_sec: float = 90.0
    # Production: navbat YO'Q — faol cycle paytidagi trigger e'tiborsiz qoldiriladi
    # (queue'ga qo'yilmaydi). Faqat simulator/test rejimida True qilib navbatni
    # tiklash mumkin.
    pending_trigger_queue_enabled: bool = False
    # Oxirgi accepted VIN 180 s ichida qayta chiqsa yangi production record yozilmaydi
    # (DUPLICATE_VIN_SUPPRESSED audit). 0 = o'chirilgan.
    same_vin_dedup_sec: float = 180.0

    # Faol sessiya paytida kelgan D2222 triggerlar yo'qolmasligi uchun
    # bounded FIFO (FAQAT pending_trigger_queue_enabled=True bo'lganda ishlatiladi).
    # Navbat to'lsa yangi trigger aniq alarm bilan rad etiladi.
    max_pending_triggers: int = 10
    overflow_policy: str = "reject_with_alarm"
    # Navbatdagi trigger capture'ni shu muddatdan kech boshlasa, kuzov kamera
    # oldidan allaqachon o'tib ketgan bo'ladi. Kech boshlab NOTO'G'RI kuzovni
    # o'qishdan ko'ra sessiyani MISSED_CAPTURE_WINDOW bilan yopish xavfsizroq
    # (noto'g'ri VIN NO_READ dan xavfliroq). Qiymat production takt vaqti va
    # D2222 sensorining kameragacha bo'lgan fizik masofasiga qarab sozlanadi.
    max_capture_delay_ms: int = 4000

    # Ixtiyoriy D2223 EXIT sensori rejimi. Standartda o'chiq: amaldagi
    # bitta D2222 pulse production oqimi o'zgarmaydi.
    exit_signal_enabled: bool = False
    post_exit_grace_ms: int = 2500
    max_session_duration_sec: float = 30.0
    allow_result_after_d2223: bool = True
    accept_frames_after_d2223: bool = False


@dataclass
class RFIDConfig:
    """
    RFID (Impinj R700) sozlamalari — gateway_python bilan mos.
    PLC=1 trigger kelganda kamera bilan PARALLEL ravishda teg o'qiladi.
    Barcha parametrlar config/settings.yaml dan keladi (hardcode YO'Q).
    """
    enabled: bool = False
    # R700 oqimida teg maydonda turgan ekan qayta-qayta ko'rinadi. Agar teg
    # sessiyaning o'qish oynasi boshlanishidan SHUNCHA ms oldin birinchi marta
    # ko'rilgan bo'lsa — bu OLDINGI kuzovdan qolgan dalil va yangi sessiyaga
    # biriktirilmaydi (traceability: EPC noto'g'ri kuzovga bog'lanmasin).
    # 0 -> tekshiruv o'chiriladi (eski xatti-harakat).
    stale_event_tolerance_ms: int = 1500
    mode: str = "simulator"       # "simulator" | "r700" (haqiqiy Impinj R700)
    ip_address: str = "192.0.2.30"
    port: int = 80                     # R700 REST/stream HTTP porti
    timeout: float = 5.0               # REST/oqim ulanish timeouti (s)
    read_duration_ms: int = 700        # (eski) — endi inventory_duration_ms ishlatiladi
    # --- gateway_python ISHLAYDIGAN o'qish oynasi/qayta urinish parametrlari ---
    # Sabab: 700ms bitta oyna juda qisqa edi (NO_TAG). gateway config.xml proven:
    #   INVENTORY_DURATION_MS=6000, RETRY_COUNT=2..3, RETRY_AFTER_MS=2000,
    #   RETRY_DURATION_MS=4000. Reader 0.15s "isish" (warmup) ham kerak.
    inventory_duration_ms: int = 6000      # 1-urinish teg-yig'ish oynasi (ms)
    inventory_retry_duration_ms: int = 4000  # keyingi urinishlar oynasi (ms)
    inventory_retry_after_ms: int = 2000   # urinishlar orasidagi pauza (ms)
    warmup_ms: int = 150                   # inventory start dan keyin oynagacha isish (ms)
    # --- R700 xususiyatlari (gateway_python bilan mos) ---
    username: str = ""
    password: str = ""
    antenna_ports: tuple = (1, 2, 3, 4)
    tx_power_cdbm: int = 3150          # uzatish quvvati (centi-dBm)
    # --- EPC -> raqam validatsiyasi (gateway: 1000..9999) ---
    epc_decimal_validate: bool = True
    epc_decimal_min: int = 1000
    epc_decimal_max: int = 9999
    reconnect_max_delay_sec: float = 30.0
    # Simulyator: qaytariladigan tayyor EPC (bo'sh -> tasodifiy 1000..9999)
    sim_epc: str = ""


@dataclass
class OCREnginesConfig:
    """v1.2.1 engine-neutral OCR orkestratsiya (docs/v1.2.1/OCR_ENGINE_CONTRACT.md).

    ORQAGA MOSLIK: agar settings.yaml da `ocr_engines` bo'limi bo'lmasa yoki
    shadow/fallback bo'sh ("") bo'lsa — standart faqat legacy PaddleOCR primary,
    ya'ni HOZIRGI ishlab chiqarish xatti-harakati AYNAN saqlanadi. Yangi engraved
    engine STANDART BO'YICHA O'CHIRILGAN (mode=disabled) — model tayyor bo'lmaguncha
    hech qachon chaqirilmaydi.

    Bo'sh string ("") yoki "none"/"null"/"disabled" -> engine ishlatilmaydi.
    """
    primary_engine: str = "paddleocr_legacy"
    shadow_engine: str = ""       # "" = shadow yo'q
    fallback_engine: str = ""     # "" = fallback yo'q
    validation_profile: str = "current_production"
    charset_profile: str = "alphanumeric_36"
    engraved_model_path: str = ""  # kelajakdagi engraved model artefakti (hozir yo'q)


@dataclass
class OCRReleaseConfig:
    """v1.2.1 OCR release integration (shadow-safe). SHADOW is the safe default;
    GUARDED_PRIMARY is available but disabled by default (weak-class blockers)."""
    release_mode: str = "SHADOW"                 # SHADOW | GUARDED_PRIMARY | ENGRAVED_STRICT
    engraved_enabled: bool = True
    engraved_model_dir: str = "models/engraved_ocr_v1.2.1"
    engraved_expected_length: int = 17
    paddle_raw_enabled: bool = True
    paddle_enhanced_enabled: bool = True
    engine_timeout_ms: int = 4000
    store_raw_results: bool = True
    collection_enabled: bool = True
    collection_root: str = "runtime/engraved_ocr_collection"
    collection_target_chars: str = "V,G,A,D,H,B,5,6,7,0,2,3"
    collection_on_unknown: bool = True
    collection_on_disagreement: bool = True
    collection_on_low_confidence: bool = True
    collection_on_segmentation_reject: bool = True
    weak_classes: str = "5,6,7,A,B,D,H"
    weak_classes_enabled: bool = False           # weak classes gated to UNKNOWN by default


# --- Global yagona nusxalar ---
CAMERA = CameraConfig()
DETECTION = DetectionConfig()
OCR = OCRConfig()
SERVER = ServerConfig()
OPERATIONS = OperationsConfig()
AUTH = AuthConfig()
VIN = VINConfig()
POS5_VERIFIER = Pos5VerifierConfig()
VIN_SLOT_RECOGNIZER = VinSlotRecognizerConfig()
PLC = PLCConfig()
RFID = RFIDConfig()
SESSION = SessionConfig()
OCR_ENGINES = OCREnginesConfig()
OCR_RELEASE = OCRReleaseConfig()


def is_production_mode() -> bool:
    """Kamida bitta haqiqiy liniya qurilmasi ishlatilsa production hisoblanadi."""
    plc_real = bool(PLC.enabled) and str(PLC.mode).lower() != "simulator"
    rfid_real = bool(RFID.enabled) and str(RFID.mode).lower() != "simulator"
    # Test/auditda enabled vaqtincha o'zgartirilmasligi mumkin; aniq real mode
    # tanlangan bo'lsa ham xavfsizlik siyosatini qo'llaymiz.
    explicit_real = (str(PLC.mode).lower() != "simulator"
                     or str(RFID.mode).lower() != "simulator")
    return plc_real or rfid_real or explicit_real


# ===================================================================
# Markazlashtirilgan YAML konfiguratsiya (config/settings.yaml)
# ===================================================================
# Barcha muhitga bog'liq sozlamalar shu fayldan o'qiladi va yuqoridagi
# dataclass standart qiymatlarini ALMASHTIRADI. Fayl bo'lmasa — standartlar
# ishlatiladi (ilova baribir ishlaydi). Hardcoded IP/port qolmaydi.
CONFIG_DIR = PROJECT_ROOT / "config"
BASE_SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
# SETTINGS_PATH remains the mutable/UI target for backward compatibility. On
# Ubuntu it resolves to /etc/ai-cam/settings.production.yaml via AI_CAM_CONFIG.
SETTINGS_PATH = _configured_path("AI_CAM_CONFIG", BASE_SETTINGS_PATH)
CONFIG_SOURCES: list[str] = []
CONFIG_LOAD_ERRORS: list[str] = []
MISSING_ENV_REFERENCES: set[str] = set()
_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_env(value):
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            MISSING_ENV_REFERENCES.add(name)
        return os.environ.get(name, "")

    return _ENV_REFERENCE.sub(replace, value)


def _read_yaml(path: Path, *, required: bool) -> dict:
    if not path.exists():
        if required:
            CONFIG_LOAD_ERRORS.append(f"required config file is missing: {path}")
        return {}
    try:
        import yaml
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise TypeError("top-level YAML value must be a mapping")
        CONFIG_SOURCES.append(str(path.resolve()))
        return data
    except Exception as exc:
        CONFIG_LOAD_ERRORS.append(f"{path}: {type(exc).__name__}: {exc}")
        return {}


# Optional local secrets. Hardware credentials may be auto-loaded from a
# git-ignored env file so a fresh copy runs with one `python run.py` and no manual
# `export` or systemd EnvironmentFile. ONLY these credential keys are honoured;
# path-root variables (AI_CAM_PROJECT_ROOT / _DATA_ROOT / _LOG_ROOT / _CONFIG,
# PADDLE_HOME ...) in the file are intentionally IGNORED so runtime paths stay
# project-relative and portable. Real environment variables always win. An absent
# file is not an error. Admin web password is NOT auto-loaded (settings.yaml keeps
# the admin/admin LAN default; export AI_CAM_ADMIN_PASSWORD to override).
_AUTOLOAD_SECRET_KEYS = (
    "AI_CAM_CAMERA_PASSWORD",
    "AI_CAM_RFID_USERNAME",
    "AI_CAM_RFID_PASSWORD",
    "AI_CAM_AUTH_OPERATOR_USERNAME",
    "AI_CAM_AUTH_OPERATOR_PASSWORD",
)
AUTOLOADED_SECRETS: list[str] = []


def _local_secret_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("AI_CAM_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([
        PROJECT_ROOT / "deploy" / "local" / "ai-cam.env",
        PROJECT_ROOT / "config" / "credentials.local.env",
        PROJECT_ROOT / ".env",
    ])
    return candidates


def _parse_env_file(path: Path) -> dict:
    parsed: dict = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                parsed[key] = value
    except Exception:
        return {}
    return parsed


def autoload_local_secrets() -> list[str]:
    """Load whitelisted credential vars from the first existing git-ignored local
    env file into os.environ (only when not already set). Returns names loaded."""
    AUTOLOADED_SECRETS.clear()
    for path in _local_secret_files():
        if not path.is_file():
            continue
        data = _parse_env_file(path)
        for key in _AUTOLOAD_SECRET_KEYS:
            if data.get(key) and not os.environ.get(key):
                os.environ[key] = data[key]
                AUTOLOADED_SECRETS.append(key)
        break  # first existing file wins
    return list(AUTOLOADED_SECRETS)


def _load_yaml_settings() -> dict:
    """Load tracked production structure, then merge an optional host override."""
    CONFIG_SOURCES.clear()
    CONFIG_LOAD_ERRORS.clear()
    MISSING_ENV_REFERENCES.clear()
    autoload_local_secrets()
    base = _read_yaml(BASE_SETTINGS_PATH, required=True)
    effective = base
    if SETTINGS_PATH.resolve() != BASE_SETTINGS_PATH.resolve():
        effective = _deep_merge(
            base,
            _read_yaml(SETTINGS_PATH, required=bool(os.environ.get("AI_CAM_CONFIG"))),
        )
    return _expand_env(effective)


def _apply_section(obj, data) -> None:
    """YAML bo'limidagi kalitlarni mavjud dataclass maydonlariga yozadi."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


def _coerce_field(obj, key, value, issues: list) -> None:
    """
    P11 FIX: YAML qiymatini dataclass maydon tipiga keltiradi (jim qabul qilish
    o'rniga). Coerce bo'lmasa — xato ro'yxatga yoziladi va standart saqlanadi.
    """
    fields = getattr(type(obj), "__dataclass_fields__", {})
    if key not in fields:
        return
    typ = fields[key].type
    # Stringga aylantirilgan annotatsiyalar (from __future__ import annotations) uchun
    typ_name = typ if isinstance(typ, str) else getattr(typ, "__name__", str(typ))
    cur_default = getattr(obj, key)
    try:
        if typ_name in ("bool",):
            if isinstance(value, bool):
                coerced = value
            elif isinstance(value, str):
                coerced = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                coerced = bool(value)
        elif typ_name in ("int",):
            coerced = int(value)
        elif typ_name in ("float",):
            coerced = float(value)
        elif typ_name in ("str",):
            coerced = str(value)
        else:
            coerced = value            # tuple/dict/boshqalar — tegmaymiz
        setattr(obj, key, coerced)
    except (ValueError, TypeError):
        issues.append(f"{type(obj).__name__}.{key}: '{value}' ({typ_name} kutilgan) "
                      f"-> noto'g'ri; standart {cur_default!r} saqlandi")
        setattr(obj, key, cur_default)


def validate_config() -> list:
    """
    P11 FIX: muhim qiymatlarni diapazon/enum bo'yicha tekshiradi. Topilgan
    muammolarni ro'yxat qilib qaytaradi (startupда log qilinadi). Ilova
    to'xtamaydi — xavfsiz standartlar bilan davom etadi.
    """
    issues: list = []

    def rng(obj, key, lo=None, hi=None, label=""):
        v = getattr(obj, key, None)
        if v is None:
            return
        try:
            if lo is not None and v < lo:
                issues.append(f"{label or key}={v} < {lo} (juda kichik)")
            if hi is not None and v > hi:
                issues.append(f"{label or key}={v} > {hi} (juda katta)")
        except TypeError:
            issues.append(f"{label or key}={v!r} — solishtirib bo'lmadi")

    rng(SERVER, "port", 1, 65535, "server.port")
    rng(PLC, "port", 1, 65535, "plc.port")
    rng(RFID, "port", 1, 65535, "rfid.port")
    rng(CAMERA, "cola_port", 1, 65535, "camera.cola_port")
    rng(CAMERA, "blob_port", 1, 65535, "camera.blob_port")
    rng(CAMERA, "stale_after_sec", 0.1, 300, "camera.stale_after_sec")
    rng(CAMERA, "max_consecutive_timeouts", 1, 1000,
        "camera.max_consecutive_timeouts")
    rng(CAMERA, "reconnect_initial_delay_sec", 0.01, 3600,
        "camera.reconnect_initial_delay_sec")
    rng(CAMERA, "reconnect_max_delay_sec", 0.01, 3600,
        "camera.reconnect_max_delay_sec")
    rng(CAMERA, "reconnect_max_attempts", 0, 100000,
        "camera.reconnect_max_attempts")
    rng(PLC, "poll_interval_ms", 10, 60000, "plc.poll_interval_ms")
    rng(SESSION, "timeout_sec", 1, 3600, "session.timeout_sec")
    rng(SESSION, "ocr_inflight_grace_sec", 0, 120, "session.ocr_inflight_grace_sec")
    rng(SESSION, "max_pending_triggers", 0, 100000,
        "session.max_pending_triggers")
    rng(SESSION, "max_capture_delay_ms", 0, 600000,
        "session.max_capture_delay_ms")
    rng(SESSION, "post_exit_grace_ms", 0, 600000,
        "session.post_exit_grace_ms")
    rng(SESSION, "max_session_duration_sec", 0.1, 86400,
        "session.max_session_duration_sec")
    rng(PLC, "camera_delay_sec", 0, 3600, "plc.camera_delay_sec")
    rng(OPERATIONS, "disk_warning_free_gb", 0.1, 100000,
        "operations.disk_warning_free_gb")
    rng(OPERATIONS, "disk_critical_free_gb", 0.1, 100000,
        "operations.disk_critical_free_gb")
    rng(OPERATIONS, "crop_retention_max", 0, 10000000,
        "operations.crop_retention_max")
    rng(OPERATIONS, "collector_retention_days", 0, 36500,
        "operations.collector_retention_days")
    rng(OPERATIONS, "backup_retention_days", 0, 36500,
        "operations.backup_retention_days")
    rng(OPERATIONS, "max_expected_child_processes", 1, 1000,
        "operations.max_expected_child_processes")
    rng(OPERATIONS, "health_preflight_timeout_sec", 0.05, 60,
        "operations.health_preflight_timeout_sec")
    if OPERATIONS.disk_critical_free_gb >= OPERATIONS.disk_warning_free_gb:
        issues.append(
            "operations.disk_critical_free_gb must be below disk_warning_free_gb"
        )
    rng(PLC, "camera_search_timeout_sec", 1, 3600, "plc.camera_search_timeout_sec")
    rng(DETECTION, "conf_threshold", 0.0, 1.0, "detection.conf_threshold")
    rng(DETECTION, "ocr_trigger_conf", 0.0, 1.0, "detection.ocr_trigger_conf")
    rng(OCR, "min_confidence", 0.0, 1.0, "ocr.min_confidence")
    rng(OCR, "parallel_engines", 1, 5, "ocr.parallel_engines")
    rng(OCR, "cpu_threads", 0, 64, "ocr.cpu_threads")
    rng(VIN, "min_final_score", 0.0, 1.0, "vin.min_final_score")
    rng(VIN, "accept_final_score", 0.0, 1.0, "vin.accept_final_score")
    rng(VIN, "accept_risky_margin", 0.0, 1.0, "vin.accept_risky_margin")
    rng(VIN, "accept_variable_margin", 0.0, 1.0, "vin.accept_variable_margin")
    rng(VIN, "accept_serial_margin", 0.0, 1.0, "vin.accept_serial_margin")
    rng(VIN, "exact_conflict_ratio", 0.0, 10.0, "vin.exact_conflict_ratio")
    rng(VIN, "exact_rescue_min_conf", 0.0, 1.0, "vin.exact_rescue_min_conf")
    rng(VIN, "independent_crop_hash_distance", 0, 64,
        "vin.independent_crop_hash_distance")
    rng(VIN, "accept_raw_support_ratio", 0.0, 1.0, "vin.accept_raw_support_ratio")
    rng(VIN, "accept_min_crops", 1, 20, "vin.accept_min_crops")
    rng(VIN, "pos5_min_crops", 1, 20, "vin.pos5_min_crops")
    rng(VIN, "pos5_min_variants", 1, 30, "vin.pos5_min_variants")
    rng(VIN, "pos5_min_margin", 0.0, 1.0, "vin.pos5_min_margin")
    rng(VIN, "pos5_structure_rescue_min_score", 0.0, 1.0,
        "vin.pos5_structure_rescue_min_score")
    rng(VIN, "pos5_structure_rescue_min_raw_support", 0.0, 1.0,
        "vin.pos5_structure_rescue_min_raw_support")
    rng(VIN_SLOT_RECOGNIZER, "min_avg_confidence", 0.0, 1.0,
        "vin_slot_recognizer.min_avg_confidence")
    rng(VIN_SLOT_RECOGNIZER, "min_char_confidence", 0.0, 1.0,
        "vin_slot_recognizer.min_char_confidence")
    rng(VIN_SLOT_RECOGNIZER, "agree_bonus", 0.0, 1.0,
        "vin_slot_recognizer.agree_bonus")
    rng(VIN_SLOT_RECOGNIZER, "min_paddle_exact_reads", 0, 20,
        "vin_slot_recognizer.min_paddle_exact_reads")
    rng(OCR, "max_ocr_attempts", 1, 200, "ocr.max_ocr_attempts")
    rng(OCR, "worker_task_timeout_sec", 1, 120, "ocr.worker_task_timeout_sec")
    # PRODUCTION BARQARORLIK OGOHLANTIRISHLARI (native crash oldini olish)
    if bool(getattr(OCR, "enable_mkldnn", False)):
        issues.append("ocr.enable_mkldnn=true — CPU'da native 'could not execute a "
                      "primitive' SIGSEGV xavfi. Productionда false tavsiya etiladi.")
    if int(getattr(OCR, "parallel_engines", 1)) > 2:
        issues.append(f"ocr.parallel_engines={OCR.parallel_engines} > 2 — process "
                      "izolyatsiyasida ham CPU haddan tashqari yuklanishi mumkin; 1-2 tavsiya.")

    if PLC.mode not in ("simulator", "melsec"):
        issues.append(f"plc.mode='{PLC.mode}' noto'g'ri (simulator|melsec)")
    if RFID.mode not in ("simulator", "r700"):
        issues.append(f"rfid.mode='{RFID.mode}' noto'g'ri (simulator|r700)")
    if str(VIN_SLOT_RECOGNIZER.mode).lower() not in ("shadow", "assist", "enforce"):
        issues.append("vin_slot_recognizer.mode noto'g'ri (shadow|assist|enforce)")
    _KNOWN_ENGINES = ("paddleocr_legacy", "engraved_sequence")
    _DISABLED_TOKENS = ("", "none", "null", "disabled")
    if str(OCR_ENGINES.primary_engine) not in _KNOWN_ENGINES:
        issues.append(f"ocr_engines.primary_engine='{OCR_ENGINES.primary_engine}' "
                      f"noma'lum (kutilgan: {_KNOWN_ENGINES})")
    for _slot in ("shadow_engine", "fallback_engine"):
        _val = str(getattr(OCR_ENGINES, _slot)).strip().lower()
        if _val not in _DISABLED_TOKENS and _val not in _KNOWN_ENGINES:
            issues.append(f"ocr_engines.{_slot}='{getattr(OCR_ENGINES, _slot)}' "
                          f"noma'lum (kutilgan: {_KNOWN_ENGINES} yoki bo'sh)")
    if str(OCR_ENGINES.charset_profile) not in ("alphanumeric_36", "vin_alnum_33"):
        issues.append(f"ocr_engines.charset_profile='{OCR_ENGINES.charset_profile}' "
                      "noma'lum (alphanumeric_36|vin_alnum_33)")
    if str(PLC.signal_kind).lower() not in ("auto", "value", "bit", "mask"):
        issues.append(f"plc.signal_kind='{PLC.signal_kind}' noto'g'ri (auto|value|bit|mask)")
    if str(getattr(PLC, "trigger_mode", "level")).lower() not in ("level", "pulse"):
        issues.append(f"plc.trigger_mode='{PLC.trigger_mode}' noto'g'ri (level|pulse)")
    if str(SESSION.overflow_policy).lower() != "reject_with_alarm":
        issues.append("session.overflow_policy hozir faqat 'reject_with_alarm' ni "
                      "qo'llab-quvvatlaydi")
    if str(PLC.signal_kind).lower() == "bit" and int(PLC.signal_bit) < 0:
        issues.append("plc.signal_kind='bit', lekin plc.signal_bit < 0 (bit indeksini bering)")

    # P15: standart admin parol ogohlantirishi
    if (AUTH.enabled and str(AUTH.password).strip().lower()
            in ("", "admin", "change_me", "changeme", "password")):
        issues.append("auth: placeholder/default parol ishlatilmoqda — productionda "
                      "AI_CAM_AUTH_PASSWORD orqali kuchli credential bering!")
    return issues


def apply_settings() -> dict:
    """Load layered YAML and environment overrides into global config objects."""
    cfg = _load_yaml_settings()
    issues: list = []
    section_map = {"camera": CAMERA, "detection": DETECTION, "ocr": OCR,
                   "server": SERVER, "auth": AUTH, "vin": VIN,
                   "operations": OPERATIONS,
                   "pos5_verifier": POS5_VERIFIER,
                   "vin_slot_recognizer": VIN_SLOT_RECOGNIZER, "plc": PLC,
                   "rfid": RFID, "session": SESSION, "ocr_engines": OCR_ENGINES,
                   "ocr_release": OCR_RELEASE}
    for name, obj in section_map.items():
        data = cfg.get(name)
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if hasattr(obj, key):
                _coerce_field(obj, key, value, issues)   # P11: tip tekshiruvi + coerce
            # noma'lum kalitlar e'tiborsiz (oldinga moslik)
    # antenna_ports YAML da ro'yxat keladi -> tuple ga keltiramiz (barqarorlik)
    try:
        if isinstance(RFID.antenna_ports, list):
            RFID.antenna_ports = tuple(RFID.antenna_ports)
    except Exception:
        pass
    for target, attribute in (
        (DETECTION, "model_path"),
        (POS5_VERIFIER, "model_path"),
        (VIN_SLOT_RECOGNIZER, "model_path"),
    ):
        raw_path = str(getattr(target, attribute, "") or "").strip()
        if raw_path and not Path(raw_path).is_absolute():
            setattr(target, attribute, str((PROJECT_ROOT / raw_path).resolve()))
    _apply_environment_overrides(issues)
    issues += validate_config()
    issues += CONFIG_LOAD_ERRORS
    if issues:
        # stderr — stdout is reserved for machine-readable JSON (--print-config etc.)
        print("[config] OGOHLANTIRISH — konfiguratsiya muammolari:", file=sys.stderr)
        for it in issues:
            print(f"  - {it}", file=sys.stderr)
    return cfg


# Deployment secrets and service-safe toggles never need to be committed.
_ENV_OVERRIDES = (
    ("AI_CAM_CAMERA_PASSWORD", CAMERA, "password"),
    ("AI_CAM_RFID_USERNAME", RFID, "username"),
    ("AI_CAM_RFID_PASSWORD", RFID, "password"),
    ("AI_CAM_AUTH_USERNAME", AUTH, "username"),
    ("AI_CAM_ADMIN_PASSWORD", AUTH, "password"),
    ("AI_CAM_AUTH_PASSWORD", AUTH, "password"),  # explicit legacy name wins
    ("AI_CAM_AUTH_OPERATOR_USERNAME", AUTH, "operator_username"),
    ("AI_CAM_AUTH_OPERATOR_PASSWORD", AUTH, "operator_password"),
    ("AI_CAM_PLC_ENABLED", PLC, "enabled"),
    ("AI_CAM_RFID_ENABLED", RFID, "enabled"),
    ("AI_CAM_OCR_USE_GPU", OCR, "use_gpu"),
    ("AI_CAM_FILE_LOGGING", OPERATIONS, "file_logging_enabled"),
    ("AI_CAM_SERVER_PORT", SERVER, "port"),
)


def _apply_environment_overrides(issues: list) -> None:
    for env_name, target, attribute in _ENV_OVERRIDES:
        if env_name in os.environ:
            _coerce_field(target, attribute, os.environ[env_name], issues)


def apply_no_hardware_mode() -> None:
    """Disable every live device while retaining real model/server startup."""
    os.environ["AI_CAM_NO_HARDWARE"] = "1"
    PLC.enabled = False
    PLC.mode = "simulator"
    RFID.enabled = False
    RFID.mode = "simulator"


def _missing_secret(value: object) -> bool:
    return str(value or "").strip().lower() in {
        "", "admin", "change_me", "changeme", "password"
    }


def validate_required_secrets() -> list[str]:
    """Return human-readable credential WARNINGS (never fatal) for a real-device
    profile. Callers only warn: the app always starts. Hardware that needs an empty
    credential reports "disconnected" until config/settings.yaml is filled in."""
    warnings: list[str] = []
    if is_production_mode():
        if _missing_secret(CAMERA.password):
            warnings.append("camera.password bo'sh/placeholder (config/settings.yaml)")
        if RFID.enabled and str(RFID.mode).lower() == "r700":
            if _missing_secret(RFID.username):
                warnings.append("rfid.username bo'sh/placeholder (config/settings.yaml)")
            if _missing_secret(RFID.password):
                warnings.append("rfid.password bo'sh/placeholder (config/settings.yaml)")
    # admin/admin is an accepted LAN default now — warn only, never block startup.
    if AUTH.enabled and _missing_secret(AUTH.password):
        warnings.append(
            "auth.password 'admin' (LAN default) — kuchli parol tavsiya etiladi "
            "(config/settings.yaml auth.password)"
        )
    return warnings


def effective_config(*, redact: bool = True) -> dict:
    """Serializable effective configuration and path/source provenance."""
    sections = {
        "camera": asdict(CAMERA),
        "plc": asdict(PLC),
        "rfid": asdict(RFID),
        "session": asdict(SESSION),
        "detection": asdict(DETECTION),
        "ocr": asdict(OCR),
        "ocr_engines": asdict(OCR_ENGINES),
        "ocr_release": asdict(OCR_RELEASE),
        "vin": asdict(VIN),
        "server": asdict(SERVER),
        "auth": asdict(AUTH),
        "operations": asdict(OPERATIONS),
    }
    if redact:
        for section in ("camera", "rfid", "auth"):
            for key in list(sections[section]):
                if any(token in key.lower() for token in ("password", "secret", "token")):
                    sections[section][key] = "***" if sections[section][key] else ""
    return {
        "sources": list(CONFIG_SOURCES),
        "load_errors": list(CONFIG_LOAD_ERRORS),
        "missing_environment_references": sorted(MISSING_ENV_REFERENCES),
        "paths": {
            "project_root": str(PROJECT_ROOT),
            "runtime_root": str(RUNTIME_DIR),
            "data_root": str(DATA_DIR),
            "log_root": str(LOGS_DIR),
            "database": str(DB_PATH),
            "crops": str(CROPS_DIR),
            "temp": str(TEMP_DIR),
            "collector": str(ENGRAVED_COLLECTION_DIR),
            "backups": str(BACKUPS_DIR),
            "models": str(MODELS_DIR),
        },
        "sections": sections,
    }


# Load once at import. settings_store may call this again after an operator edit.
_LOADED_SETTINGS = apply_settings()

# P16 FIX: crop retention is explicit and environment-overridable.
CROP_RETENTION_MAX = int(os.environ.get(
    "AI_CAM_CROP_RETENTION_MAX", str(OPERATIONS.crop_retention_max)
))

# VIN format qoidasi: 17 belgi, I/O/Q harflari yo'q (ISO 3779)
VIN_LENGTH = 17
VIN_INVALID_CHARS = set("IOQ")
VIN_ALLOWED = set("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")
# (end of config)
