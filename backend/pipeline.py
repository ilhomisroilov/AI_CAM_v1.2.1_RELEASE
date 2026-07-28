"""
============================================================
pipeline.py  —  Tizim orkestratori (yagona boshqaruv markazi)
============================================================
Tarmoq logikasi `lector652_pipeline` loyihasidagi ISHLAYDIGAN koddan
olingan (camera_client.Lector652Client). Taxmin qilingan generic TCP
reader EMAS — aniq mLIStart live-push + BLOB framing ishlatiladi.

Oqim:
   SICK Lector652
       │  CoLa-A (2111): connect handshake + sMN mLIStart 0
       ▼  BLOB (2113): kamera AVTOMATIK frame push qiladi
   read_stream_frame() -> decode_bmp()  (XOM grayscale, AYLANTIRISH YO'Q)
       │
       ├─► live_frame (MJPEG uchun)
       ├─► YOLOv8n aniqlash + bbox chizish   (faqat "Start" bosilganda)
       └─► yuqori ishonchli ROI ──► OCR navbati ──► DB

UI semantikasi:
  Connect Camera : ulanadi + live stream boshlanadi (xom video ko'rinadi)
  Start Processing: YOLO + OCR yoqiladi (bbox + VIN o'qish + DB)
  Stop           : YOLO + OCR o'chadi (xom video davom etadi)
  Disconnect     : stream va ulanish yopiladi
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

from .ai.crop_quality import quality_score
from .ai.detector import PlateDetector
from .ai.dataset_collector import DatasetCollector
from .ai.ocr_retry_guard import (
    OCRRetryGuard,
    OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT,
    STABLE_AMBIGUITY,
)
from .ai.ocr_worker import OCRWorker
from .camera.camera_client import Lector652Client, decode_bmp, decode_payload
from .config import CAMERA, CROPS_DIR, DATASET_DIR, DETECTION, OCR, SERVER
from .database import db
from .logger import log


class CameraState(Enum):
    """Public camera-watchdog state contract retained across release merges."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


class SessionState(Enum):
    """Public session state contract used by observability and stress tooling."""
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    WAITING_RESULTS = "WAITING_RESULTS"
    EXIT_SEEN = "EXIT_SEEN"
    GRACE_WAIT = "GRACE_WAIT"
    FINALIZING = "FINALIZING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # Capture oynasi yo'qolgan: trigger navbatda `max_capture_delay_ms` dan uzoq
    # turgan, shuning uchun kamera NOTO'G'RI (keyingi) kuzovni o'qishi mumkin edi.
    # Jim yo'qotish EMAS — aniq terminal holat + DB yozuvi.
    MISSED_CAPTURE_WINDOW = "MISSED_CAPTURE_WINDOW"


_TERMINAL_SESSION_STATES = frozenset({
    SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS, SessionState.TIMEOUT,
    SessionState.FAILED, SessionState.CANCELLED,
    SessionState.MISSED_CAPTURE_WINDOW,
})


def _new_session_id() -> str:
    """Restartlar orasida ham to'qnashmaydigan global sessiya identifikatori."""
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


@dataclass
class Session:
    """Stable session DTO contract; live legacy fields remain mirrored by Pipeline."""
    session_id: str
    trigger_sequence: int
    triggered_at: float
    started_at: Optional[float] = None
    deadline: float = 0.0
    state: SessionState = SessionState.STARTING
    d2222_at: Optional[float] = None
    d2223_at: Optional[float] = None
    exit_seen_at: Optional[float] = None
    grace_deadline: Optional[float] = None
    hard_deadline: float = 0.0
    vin_at: Optional[float] = None
    rfid_at: Optional[float] = None
    vin: Optional[str] = None
    vin_confidence: float = 0.0
    vin_raw: Optional[str] = None
    vin_model: Optional[str] = None
    vin_rel_path: Optional[str] = None
    rfid_epc: Optional[str] = None
    rfid_raw: Optional[str] = None
    rfid_antenna: Optional[int] = None
    rfid_rssi: Optional[float] = None
    # EPC ownership audit (traceability): teg qachon O'QUVCHI tomonidan birinchi
    # ko'rilgan, shu sessiyaning o'qish oynasi qachon boshlangan va nega rad
    # etilgan. Eskirgan (oldingi kuzovdan qolgan) event jim yutilmaydi.
    rfid_reader_event_at: Optional[float] = None
    rfid_read_started_at: Optional[float] = None
    rfid_rejection_reason: Optional[str] = None
    finalized_at: Optional[float] = None
    failure_reason: Optional[str] = None
    # --- Pre-OCR observability (incident forensics) ---------------------
    # Incidentda ko'p sessiyalar `[OCR TRIGGER]` gacha yetmagan, lekin qaysi
    # bosqichda yo'qolgani ANIQLANMAGAN. Quyidagi hisoblagichlar zanjirning
    # har bo'g'inini o'lchaydi va `failure_stage` aniq javob beradi.
    payloads_received: int = 0
    frames_decoded: int = 0
    decode_failures: int = 0
    yolo_inference_count: int = 0
    yolo_detection_count: int = 0
    crops_created: int = 0
    crops_rejected: int = 0
    ocr_jobs_submitted: int = 0
    ocr_jobs_started: int = 0
    ocr_jobs_completed: int = 0
    ocr_no_read_count: int = 0
    ocr_ambiguous_count: int = 0
    ocr_timeout_count: int = 0
    first_payload_at: Optional[float] = None
    first_decoded_frame_at: Optional[float] = None
    first_detection_at: Optional[float] = None
    first_crop_at: Optional[float] = None
    first_ocr_submitted_at: Optional[float] = None
    first_ocr_result_at: Optional[float] = None
    plate_exit_at: Optional[float] = None
    capture_completed_at: Optional[float] = None
    queue_wait_ms: float = 0.0
    failure_stage: Optional[str] = None
    ocr_terminal_reason: Optional[str] = None
    last_evidence_signature: Optional[str] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    watchdog: Optional[threading.Thread] = None


class Pipeline:
    """Butun AI_CAM ish jarayonini boshqaradi (ishlaydigan tarmoq logikasi bilan)."""

    def __init__(self) -> None:
        self.detector = PlateDetector()
        self.ocr = OCRWorker(on_result=self._on_ocr_result, on_fail=self._on_ocr_fail)
        self._ocr_retry_guard = OCRRetryGuard()
        self.client: Optional[Lector652Client] = None

        # Avtomatik dataset yig'uvchi (fon thread + navbat)
        self.collector = DatasetCollector(
            out_dir=str(DATASET_DIR),
            collect_conf=DETECTION.collect_conf,
            class_id=DETECTION.collect_class_id,
            min_interval_sec=DETECTION.collect_min_interval_sec,
        )

        # Live kadr (JPEG bytes) — MJPEG stream shu yerdan o'qiydi
        self._live_jpeg: Optional[bytes] = None
        self._frame_lock = threading.Lock()

        # --- PF3/PF5 FIX: capture/inference ni AJRATISH (decoupled) ---
        # Capture thread: faqat o'qish+dekod, DOIM eng yangi kadrni saqlaydi (eski
        # tashlanadi) -> socket buferida backlog yig'ilmaydi, latency past.
        # Inference thread: eng yangi kadrni oladi (oraliq kadrlar o'tkazib yuboriladi),
        # YOLO/trigger + MJPEG kodlash o'z tezligida. Sekin YOLO live FPS ni bloklamaydi.
        self._capture_thread: Optional[threading.Thread] = None
        self._inference_thread: Optional[threading.Thread] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_id: int = 0          # monotonik kadr hisoblagichi (drop-old)
        self._capture_lock = threading.Lock()
        self._capture_alive = False             # capture loop sog'ligi (inference shu bilan to'xtaydi)
        self._dropped_frames = 0                # inference o'tkazib yuborgan (eskirgan) kadrlar
        self._decode_fail_streak = 0            # ketma-ket decode fail soni (diagnostika)
        self._decode_fail_total = 0             # jami decode fail (status uchun)
        self._last_decode_format = None         # oxirgi muvaffaqiyatli format (status)
        self._stream_thread: Optional[threading.Thread] = None  # (eski moslik)
        self._stop_event = threading.Event()
        # Plastinka kadrdan chiqqach FRAME OLISHNI (BLOB oqimini) pauza qilish
        # uchun — TCP ulanishni (CoLa control+blob) UZMASDAN. To'liq disconnect/
        # reconnect har mashinada qimmat va xavfli (qurilma bitta ulanish qabul
        # qiladi); shuning uchun mLIStop/mLIStart bilan yengil pauza qilamiz.
        self._capture_pause = threading.Event()

        self._camera_connected = False
        self._processing = False           # YOLO + OCR yoniq/ o'chiq

        # Kamera watchdog/reconnect state. `_camera_connected` faqat birinchi
        # haqiqiy frame kelgandan keyin True bo'ladi.
        self._camera_lock = threading.RLock()
        self._camera_state = CameraState.DISCONNECTED
        self._camera_thread_alive = False
        self._last_frame_received_at = 0.0
        self._last_successful_decode_at = 0.0
        self._last_detection_at = 0.0
        self._consecutive_timeouts = 0
        self._consecutive_errors = 0
        self._consecutive_decode_errors = 0
        self._camera_reconnect_attempts = 0
        self._camera_last_error: Optional[str] = None
        self._camera_generation = 0
        self._camera_shutting_down = False
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_lock = threading.Lock()
        self._reconnect_stop_event = threading.Event()

        # ONE-SHOT callback: VIN muvaffaqiyatli o'qilgach chaqiriladi (server
        # buni plc_service.notify_vin_done ga ulaydi) -> PLC signali 0 ga tushadi,
        # kamera o'chadi. None bo'lsa hech narsa qilinmaydi.
        self.on_vin_done = None

        # RFID xizmati (server ulaydi). PLC=1 da kamera bilan PARALLEL teg o'qiladi.
        # _current_rfid: shu trigger hodisasi uchun o'qilgan RFID (VIN bilan BIR
        # qatorga yoziladi). Har yangi sikl boshida tozalanadi.
        self.rfid_service = None
        self._current_rfid: Optional[dict] = None
        self._rfid_lock = threading.Lock()

        # --- PLC sessiya state machine ---
        # Har D2222 = alohida, global-unique Session. Tarix late-result egaligi
        # uchun chegaralangan holda saqlanadi; band paytdagi trigger bounded FIFO.
        self._session_lock = threading.RLock()
        self._sessions: Dict[str, Session] = {}
        self._session_order: deque = deque()
        self._session_history_max = 200
        self._active_session_id: Optional[str] = None
        # Capture ownership sessiya umridan AJRATILGAN: plastinka kadrdan chiqishi
        # bilan kamera keyingi kuzov uchun ozod bo'ladi, sessiya esa OCR/RFID
        # natijasini fonda kutishda davom etadi. Bir vaqtda kamera kadri FAQAT
        # bitta sessiyaga tegishli bo'ladi (ownership invariantini buzmaydi).
        self._capture_owner_id: Optional[str] = None
        self._last_session_id: Optional[str] = None
        self._last_completed_session_id: Optional[str] = None
        self._trigger_sequence = 0
        self._pending_triggers: deque = deque()
        self._dropped_trigger_count = 0
        # v1.3.0: production body-cycle invariant. Triggers arriving during an
        # active cycle (or sooner than the minimum body interval) are duplicates
        # / bounce / external pulses — suppressed, not queued, and audited.
        self._suppressed_trigger_count = 0
        self._last_accepted_trigger_mono: Optional[float] = None
        self._metrics = {
            "late_ocr_results_total": 0,
            "late_rfid_results_total": 0,
            "session_mismatch_total": 0,
            "database_write_failure_total": 0,
        }
        self._legacy_bare_runtime = False

        # v1.1.3 D2222 hard-deadline testlari va eski ichki tooling uchun
        # mirror maydonlar. Asosiy truth yuqoridagi Session obyektlaridir.
        self._session_active = False
        self._session_id = 0
        self._session_deadline = 0.0
        self._session_finalized = False
        self._session_vin: Optional[dict] = None
        self._session_rfid: Optional[dict] = None
        self._session_stop = threading.Event()
        self._session_done = threading.Event()
        self._session_watchdog: Optional[threading.Thread] = None
        self._rfid_grace_applied = False        # VIN topilgach RFID grace bir marta qo'llanadi
        self._ocr_attempts = 0                  # shu sessiyada OCR trigger soni (retry chegarasi)
        self._dropped_triggers = 0              # eski status nomi uchun mirror
        self._session_start_ts = 0.0            # trigger qabul qilingan monotonic vaqt
        self._ocr_inflight_session = None
        self._ocr_inflight_started = 0.0
        self._ocr_inflight_done = threading.Event()
        self._ocr_inflight_events: Dict[str, threading.Event] = {}
        self._ocr_grace_active = False
        self._ocr_retry_guard = OCRRetryGuard()
        self._session_frames_seen = 0
        self._session_max_yolo_conf = 0.0
        self._session_best_crop_quality = 0.0
        self._session_ocr_triggered = 0

        # Statistika (UI uchun)
        self.stats = {"frames": 0, "detections": 0, "vins": 0, "rfid_reads": 0, "fps": 0.0}
        self._ts_buf: list[float] = []     # FPS hisoblash uchun
        self._last_frame_ts: float = 0.0   # oxirgi kadr vaqti (last packet — camera)

        # --- Single-shot trigger holati (debounce / state-lock) ---
        # Bitta plastinka kadrda bir necha kadr turganda OCR FAQAT BIR MARTA
        # ishga tushadi. Plastinka kadrdan ketganda qulf bo'shaydi (re-arm).
        self._plate_locked = False         # True -> bu plastinka allaqachon OCR ga yuborilgan
        self._absent_frames = 0            # ketma-ket "plastinka yo'q" kadrlar soni
        self._last_ocr_ts = -1e9           # oxirgi OCR trigger vaqti (birinchi trigger doim o'tadi)
        # Multi-frame fusion buferi: bitta hodisa uchun (crop, sifat_balli) lar
        self._event_buf: list = []
        self._confirm = 0                  # ketma-ket yuqori-ishonch kadrlar soni

    # ===============================================================
    # Kamera ulanish (Connect Camera tugmasi) — IP UI dan dinamik
    # ===============================================================
    def connect_camera(self, ip: str | None = None) -> bool:
        """
        Kameraga ulanadi va live streamni boshlaydi.
        IP UI dan dinamik keladi (hardcode YO'Q). BLOB Port 2113.
        """
        with self._camera_lock:
            if self._camera_thread_alive:
                return True
            self._camera_shutting_down = False
        self._cancel_reconnect_locked()
        return self._connect_camera_now(ip)

    def _connect_camera_now(self, ip: str | None = None) -> bool:
        """Bitta fizik ulanish urinishi; on-demand va reconnect ikkalasi ishlatadi."""
        ip = (ip or CAMERA.ip).strip()
        CAMERA.ip = ip
        log.info(f"Kamera IP: {ip} (CoLa {CAMERA.cola_port}, BLOB {CAMERA.blob_port})")
        with self._camera_lock:
            self._camera_state = CameraState.CONNECTING
            self._camera_connected = False

        client = Lector652Client(
            ip=ip, control_port=CAMERA.cola_port, blob_port=CAMERA.blob_port,
            password=CAMERA.password, on_log=log.info,
        )
        try:
            client.connect()
        except Exception as exc:
            log.error(f"Kamera ulanishi muvaffaqiyatsiz: {exc}")
            try:
                client.disconnect()
            except Exception:
                pass
            self.client = None
            with self._camera_lock:
                self._camera_state = CameraState.FAILED
                self._camera_last_error = str(exc)
            self._schedule_reconnect()
            return False

        self.client = client
        self._stop_event.clear()
        self._capture_pause.clear()
        with self._capture_lock:
            self._latest_frame = None
            self._latest_frame_id = 0
        self._dropped_frames = 0
        self._capture_alive = True
        with self._camera_lock:
            self._camera_generation += 1
            generation = self._camera_generation
            self._camera_thread_alive = True
            self._consecutive_timeouts = 0
            self._consecutive_errors = 0
            self._consecutive_decode_errors = 0

        self._capture_thread = threading.Thread(
            target=self._stream_loop_wrapper, args=(generation,),
            name="lector-capture", daemon=True,
        )
        self._inference_thread = threading.Thread(
            target=self._inference_loop, name="lector-inference", daemon=True,
        )
        self._capture_thread.start()
        self._inference_thread.start()
        return True

    def disconnect_camera(self) -> None:
        with self._camera_lock:
            self._camera_shutting_down = True
            self._camera_state = CameraState.STOPPING
            self._camera_generation += 1
        self._cancel_reconnect_locked()
        self.stop_processing()
        self._stop_event.set()
        for t_attr in ("_inference_thread", "_capture_thread", "_stream_thread"):
            t = getattr(self, t_attr, None)
            if t is not None:
                t.join(timeout=8.0)
                setattr(self, t_attr, None)
        self._capture_alive = False
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        with self._camera_lock:
            self._camera_connected = False
            self._camera_thread_alive = False
            self._camera_state = CameraState.DISCONNECTED
        with self._frame_lock:
            self._live_jpeg = None
        log.info("Kamera uzildi.")

    def _stream_loop_wrapper(self, generation: int) -> None:
        """Capture o'limini state'ga aks ettiradi va reconnectni rejalashtiradi."""
        try:
            self._stream_loop()
        except Exception as exc:
            log.error(f"[CAMERA] Stream thread kutilmagan xato: {exc}", exc_info=True)
            with self._camera_lock:
                self._camera_last_error = str(exc)
        finally:
            self._capture_alive = False
            with self._camera_lock:
                self._camera_thread_alive = False
                current = generation == self._camera_generation
                shutting_down = self._camera_shutting_down
                if current:
                    self._camera_connected = False
                    self._camera_state = (CameraState.DISCONNECTED if shutting_down
                                          else CameraState.FAILED)
            if current and not shutting_down:
                self._schedule_reconnect()

    def _stream_loop(self) -> None:
        """Watchdog wrapper uchun current decoupled capture loop adapteri."""
        self._capture_loop()

    def _schedule_reconnect(self) -> None:
        with self._reconnect_lock:
            if self._camera_shutting_down:
                return
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return
            with self._camera_lock:
                self._camera_state = CameraState.RECONNECTING
            self._reconnect_stop_event.clear()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, name="camera-reconnect", daemon=True,
            )
            self._reconnect_thread.start()

    def _cancel_reconnect_locked(self) -> None:
        with self._reconnect_lock:
            thread = self._reconnect_thread
            self._reconnect_thread = None
        self._reconnect_stop_event.set()
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=5.0)

    def _reconnect_loop(self) -> None:
        delay = max(0.01, float(CAMERA.reconnect_initial_delay_sec))
        attempt = 0
        while not self._reconnect_stop_event.is_set():
            with self._camera_lock:
                if self._camera_shutting_down:
                    return
            max_attempts = int(CAMERA.reconnect_max_attempts)
            if max_attempts and attempt >= max_attempts:
                with self._camera_lock:
                    self._camera_state = CameraState.FAILED
                return
            attempt += 1
            with self._camera_lock:
                self._camera_reconnect_attempts += 1
                self._camera_state = CameraState.RECONNECTING
            if self._reconnect_stop_event.wait(delay):
                return
            with self._camera_lock:
                if self._camera_shutting_down:
                    return
            if self._connect_camera_now(None):
                return
            delay = min(delay * 2.0, max(0.01, float(CAMERA.reconnect_max_delay_sec)))

    def _camera_stream_healthy(self) -> bool:
        """Kamera ulangan va capture/inference threadlari tirikligini tekshiradi."""
        if not self._camera_connected or not self._capture_alive:
            return False
        if self._capture_thread is not None and not self._capture_thread.is_alive():
            return False
        if self._inference_thread is not None and not self._inference_thread.is_alive():
            return False
        return True

    def _ensure_camera_stream(self) -> bool:
        """
        PLC trigger oldidan kamera oqimi haqiqatda tirik ekanini kafolatlaydi.
        Capture loop timeout/socket xatosi bilan o'lsa, _camera_connected eski True
        holatda qolishi mumkin; bu keyingi D2222 triggerda OCRni bloklar edi.
        """
        if self._camera_stream_healthy():
            return True
        if self._camera_connected:
            log.warning("Kamera stream sog'lom emas - qayta ulanish bajariladi.")
            self.disconnect_camera()
        return self.connect_camera(CAMERA.ip)

    # ===============================================================
    # YOLO + OCR yoqish / o'chirish (Start / Stop)
    # ===============================================================
    def _discard_frame_ownership(self) -> None:
        """Oldingi kuzovga tegishli crop va pending latest-frame'ni bekor qiladi."""
        self._reset_event()
        self._absent_frames = 0
        with self._capture_lock:
            self._latest_frame = None
            self._latest_frame_id += 1

    def start_processing(self) -> bool:
        if not self._camera_connected and not self._capture_alive:
            log.warning("Avval kamerani ulang (Connect Camera).")
            return False
        # Oldingi mashinada pauza qilingan bo'lsa (plastinka ketgach) — yangi
        # trigger uchun kadr olishni qayta boshlaymiz.
        self.resume_camera_stream()
        if self._processing:
            return True
        # MUHIM: har yangi sikl (PLC=1) trigger holatini TOZALAYDI. Aks holda oldingi
        # one-shot dan qolgan _plate_locked=True yangi avtomobil plastinkasini bloklab,
        # OCR ishga tushmay qolishi mumkin edi (osilib qolish/yo'qotilgan VIN).
        self._discard_frame_ownership()
        self._ocr_attempts = 0             # OCR retry hisoblagichini tozalaymiz
        # Yangi trigger -> oldingi RFID natijasini tozalaymiz (VIN bilan bir qatorga
        # FAQAT shu trigger ning RFID i tushishi uchun).
        with self._rfid_lock:
            self._current_rfid = None
        self.ocr.start()
        if DETECTION.collect_enabled:
            self.collector.start()               # dataset auto-collection (fon thread)
        self._processing = True
        log.info("AI ishlov berish yoqildi (YOLO + OCR + dataset yig'ish).")
        return True

    def stop_processing(self) -> None:
        self._discard_frame_ownership()
        if not self._processing:
            return
        self._processing = False
        self.ocr.stop()
        self.collector.stop()
        log.info("AI ishlov berish o'chirildi (xom video davom etadi).")

    # ===============================================================
    # Kamera KADR OLISHNI pauza/davom ettirish (mLIStop/mLIStart) — TCP
    # ulanishni (CoLa control+blob) SAQLAB QOLADI, faqat qurilmani frame
    # push qilishdan to'xtatadi/qayta ishga tushiradi. Har mashinada to'liq
    # connect/disconnect qilishdan (sekin + qurilma bitta ulanish qabul
    # qilgani uchun xavfli) ko'ra yengilroq va xavfsizroq.
    # ===============================================================
    def pause_camera_stream(self) -> None:
        """Plastinka kadrdan chiqqach chaqiriladi: kamera kadr YUBORISHNI to'xtatadi."""
        if self._capture_pause.is_set():
            return
        self._capture_pause.set()
        if self.client is not None:
            try:
                self.client.stop_stream()            # sMN mLIStop
                log.info("[CAMERA] Kadr olish to'xtatildi (mLIStop) — ulanish ochiq qoladi.")
            except Exception as exc:
                log.warning(f"[CAMERA] pause (mLIStop) xatosi: {exc}")

    def resume_camera_stream(self) -> None:
        """Keyingi mashina uchun chaqiriladi: kamera kadr yuborishni qayta boshlaydi."""
        if not self._capture_pause.is_set():
            return
        self._capture_pause.clear()
        if self.client is not None:
            try:
                self.client.start_stream()           # sMN mLIStart 0
                log.info("[CAMERA] Kadr olish qayta boshlandi (mLIStart).")
            except Exception as exc:
                log.warning(f"[CAMERA] resume (mLIStart) xatosi: {exc}")

    # ===============================================================
    # Modellarni oldindan yuklash (PLC kechikishini nolga tushirish)
    # ===============================================================
    def warmup_models(self) -> bool:
        """
        YOLO + PaddleOCR ni ilova ishga tushganda xotiraga yuklaydi va
        DOIM xotirada qoldiradi. PLC=1 bo'lganda model yuklanmaydi -> 0 kechikish.
        """
        log.info("Modellar oldindan yuklanmoqda (YOLO + PaddleOCR)...")
        if self.detector.ready:
            log.info(f"YOLO tayyor (startup da yuklangan, device={self.detector.device}).")
        else:
            log.warning("YOLO model tayyor emas — best.pt ni tekshiring.")
        ocr_preload_ready = False
        try:
            ocr_preload_ready = bool(
                self.ocr.preload()
            )                                # PaddleOCR engine ni oldindan yuklaydi
        except Exception as exc:
            log.error(f"OCR warmup xatosi: {exc}")
        # Device holatini ANIQ bitta qatorda (task 4: GPU/CPU ko'rinishi)
        try:
            import torch
            cuda = torch.cuda.is_available()
        except Exception:
            cuda = False
        # OCR engine alohida processda yashaydi; parentda `_engine` yo'q. Startup
        # runtime policy OCR.use_gpu ni faqat Paddle CUDA probe o'tsa True qoldiradi.
        ocr_gpu = bool(getattr(OCR, "use_gpu", False))
        log.info(f"[DEVICE] CUDA mavjud={cuda} | YOLO device={self.detector.device} | "
                 f"OCR GPU={ocr_gpu}. (Ubuntu+NVIDIA serverda YOLO=cuda:0 bo'lishi kerak; "
                 f"aks holda CUDA torch / paddlepaddle-gpu o'rnating.)")
        if not cuda:
            log.warning("[DEVICE] CUDA topilmadi — YOLO/OCR CPU da ishlaydi (sekin). "
                        "Ubuntu NVIDIA serverda CUDA drayver + CUDA torch o'rnating.")
        return ocr_preload_ready

    # ===============================================================
    # PLC boshqaruvi (signal=1 -> ishlov ber; signal=0 -> to'xta)
    # MODELLAR HAR DOIM XOTIRADA — PLC faqat oqimni boshqaradi.
    # ===============================================================
    def _ensure_session_runtime(self) -> None:
        """Pipeline.__new__ ishlatadigan v1.1.3 tooling uchun lazy compatibility."""
        if hasattr(self, "_sessions"):
            return
        self._sessions = {}
        self._session_order = deque()
        self._session_history_max = 200
        self._active_session_id = None
        self._capture_owner_id = None
        self._last_session_id = None
        self._trigger_sequence = 0
        self._pending_triggers = deque()
        self._dropped_trigger_count = 0
        self._metrics = {
            "late_ocr_results_total": 0, "late_rfid_results_total": 0,
            "session_mismatch_total": 0, "database_write_failure_total": 0,
        }
        self._legacy_bare_runtime = True
        self._ocr_inflight_events = {}
        self._ocr_inflight_session = None
        self._ocr_inflight_started = 0.0
        self._ocr_inflight_done = threading.Event()
        self._ocr_grace_active = False
        self._ocr_retry_guard = OCRRetryGuard()
        self._session_frames_seen = 0
        self._session_max_yolo_conf = 0.0
        self._session_best_crop_quality = 0.0
        self._session_ocr_triggered = 0

    def _mirror_legacy_locked(self, session: Session) -> None:
        terminal = session.state in _TERMINAL_SESSION_STATES
        self._session_active = not terminal
        self._session_finalized = terminal or session.state == SessionState.FINALIZING
        self._session_id = session.session_id
        self._session_deadline = session.deadline
        self._session_stop = session.stop_event
        self._session_done = session.done_event
        self._session_watchdog = session.watchdog
        self._session_vin = (None if session.vin is None else {
            "vin": session.vin, "conf": session.vin_confidence,
            "rel_path": session.vin_rel_path, "raw_vin": session.vin_raw,
            "model": session.vin_model,
        })
        self._session_rfid = (None if session.rfid_epc is None else {
            "raw": session.rfid_raw or "", "number": session.rfid_epc,
            "ts": session.rfid_at,
        })

    def _sync_legacy_locked(self, session: Session) -> None:
        if not self._legacy_bare_runtime:
            return
        vin = getattr(self, "_session_vin", None)
        if session.vin is None and vin:
            session.vin = vin.get("vin")
            session.vin_confidence = float(vin.get("conf") or 0.0)
            session.vin_rel_path = vin.get("rel_path")
            session.vin_raw = vin.get("raw_vin")
            session.vin_model = vin.get("model")
            session.vin_at = time.time()
        rfid = getattr(self, "_session_rfid", None)
        if session.rfid_epc is None and rfid:
            session.rfid_epc = rfid.get("number")
            session.rfid_raw = rfid.get("raw")
            session.rfid_at = time.time()

    @property
    def capture_owner_id(self) -> Optional[str]:
        """
        Hozir kamera kadrlariga EGA bo'lgan sessiya (yoki None — kamera bo'sh).

        Bu `_active_session_id` dan FARQ qiladi: natija kutayotgan (draining)
        sessiya hamon "faol" bo'lishi mumkin, lekin capture'ga ega bo'lmaydi.
        """
        return self._capture_owner_id

    def _release_capture(self, session_id: str, reason: str) -> None:
        """
        Capture ownership'ni bo'shatadi va navbatdagi triggerni DARHOL boshlaydi.

        MUHIM: sessiyani YOPMAYDI. Yuborilgan OCR joblari va RFID grace fonda
        davom etadi; sessiya o'z deadline'i bo'yicha mustaqil finalize bo'ladi.
        Aynan shu ajratish "keyingi kuzov oldingi sessiyaning DB finalization'ini
        kutib turadi" incidentini yo'q qiladi.
        """
        next_trigger: Optional[Tuple[int, float]] = None
        with self._session_lock:
            if self._capture_owner_id != session_id:
                return                       # capture allaqachon boshqa sessiyada
            self._capture_owner_id = None
            session = self._sessions.get(session_id)
            # FAQAT ACTIVE -> WAITING_RESULTS. EXIT_SEEN/GRACE_WAIT (D2223 rejimi)
            # o'z semantikasini saqlaydi — grace deadline'ini WAITING_RESULTS
            # bilan almashtirib yubormaymiz.
            if session is not None:
                session.capture_completed_at = time.time()
                if reason == "plate_exit":
                    session.plate_exit_at = session.capture_completed_at
                    session.ocr_terminal_reason = OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT
                    self._ocr_retry_guard.mark_plate_exit(session_id)
            if session is not None and session.state == SessionState.ACTIVE:
                session.state = SessionState.WAITING_RESULTS
                self._mirror_legacy_locked(session)
            if self._pending_triggers:
                next_trigger = self._pending_triggers.popleft()
        log.info(f"[CAPTURE] #{session_id} capture ownership bo'shatildi (sabab={reason}) — "
                 f"kamera keyingi kuzov uchun tayyor.")
        if next_trigger is not None:
            seq, triggered_at = next_trigger
            log.info(f"[QUEUE] Navbatdagi trigger #{seq} DARHOL boshlanmoqda "
                     f"(oldingi sessiya finalization KUTILMAYDI, "
                     f"navbatda qolgan={len(self._pending_triggers)}).")
            self._start_session(seq, triggered_at)

    def plc_on(self) -> None:
        """
        PLC signal=1: YANGI TRIGGER keldi.

        Navbatga qo'yish sharti endi SESSIYA emas, CAPTURE EGALIGI:
          * kamera bo'sh bo'lsa (capture_owner_id is None) -> DARHOL boshlanadi,
            oldingi sessiya hali OCR/RFID natijasini kutayotgan bo'lsa ham;
          * kamera band bo'lsa -> bounded FIFO navbat (jim tashlanmaydi);
          * navbat to'lsa -> aniq rad + CRITICAL log + metrika.
        """
        from .config import OPERATIONS, RUNTIME_DIR, SESSION
        self._ensure_session_runtime()
        try:
            free_gb = shutil.disk_usage(RUNTIME_DIR).free / (1024 ** 3)
            if free_gb <= float(OPERATIONS.disk_critical_free_gb):
                with self._session_lock:
                    self._dropped_trigger_count += 1
                log.critical(
                    "[DISK CRITICAL] PLC trigger rejected before capture: "
                    f"{free_gb:.2f} GiB free <= "
                    f"{OPERATIONS.disk_critical_free_gb:.2f} GiB critical threshold."
                )
                return
        except OSError as exc:
            log.warning(f"[DISK] Trigger-time free-space check failed: {exc}")
        queue_enabled = bool(getattr(SESSION, "pending_trigger_queue_enabled", False))
        min_interval = float(getattr(SESSION, "minimum_body_interval_sec", 0.0) or 0.0)
        with self._session_lock:
            self._trigger_sequence += 1
            seq = self._trigger_sequence
            triggered_at = time.time()
            now_mono = time.monotonic()
            owner = (self._sessions.get(self._capture_owner_id)
                     if self._capture_owner_id else None)
            active = owner is not None and owner.state not in _TERMINAL_SESSION_STATES
            if active:
                if not queue_enabled:
                    # PRODUCTION: real bodies are >= minimum_body_interval apart, so a
                    # trigger during an active cycle is a duplicate/bounce/external
                    # pulse. Suppress + audit; do NOT open a second session/record.
                    self._suppressed_trigger_count += 1
                    self._audit_suppressed_trigger(
                        seq, triggered_at, "DUPLICATE_TRIGGER_IGNORED",
                        "ACTIVE_BODY_CYCLE", owner.session_id)
                    log.warning(
                        f"[PLC] Trigger #{seq} DUPLICATE_TRIGGER_IGNORED "
                        f"(reason=ACTIVE_BODY_CYCLE, kamera #{owner.session_id} band; "
                        f"suppressed_trigger_count={self._suppressed_trigger_count}).")
                    return
                # SIMULATOR/TEST queue mode (opt-in): bounded FIFO, never silent-drop.
                if len(self._pending_triggers) >= int(SESSION.max_pending_triggers):
                    self._dropped_trigger_count += 1
                    log.critical(
                        f"[TRIGGER QUEUE OVERFLOW] max_pending_triggers="
                        f"{SESSION.max_pending_triggers} to'lgan — trigger #{seq} RAD ETILDI "
                        f"(overflow_policy={SESSION.overflow_policy}). "
                        f"dropped_trigger_count={self._dropped_trigger_count}."
                    )
                    return
                self._pending_triggers.append((seq, triggered_at))
                log.warning(f"[PLC] Trigger #{seq} navbatga qo'yildi (kamera #"
                            f"{owner.session_id} sessiyasida band) — navbat uzunligi="
                            f"{len(self._pending_triggers)}.")
                return
            # Not active. PRODUCTION: reject an implausibly-early re-trigger.
            if (not queue_enabled and min_interval > 0.0
                    and self._last_accepted_trigger_mono is not None
                    and (now_mono - self._last_accepted_trigger_mono) < min_interval):
                gap = now_mono - self._last_accepted_trigger_mono
                self._suppressed_trigger_count += 1
                self._audit_suppressed_trigger(
                    seq, triggered_at, "SUSPICIOUS_EARLY_TRIGGER",
                    f"interval {gap:.1f}s < minimum_body_interval {min_interval:.0f}s", None)
                log.warning(
                    f"[PLC] Trigger #{seq} SUSPICIOUS_EARLY_TRIGGER "
                    f"(gap {gap:.1f}s < {min_interval:.0f}s; "
                    f"suppressed_trigger_count={self._suppressed_trigger_count}).")
                return
            self._last_accepted_trigger_mono = now_mono
        self._start_session(seq, triggered_at)

    def _audit_suppressed_trigger(self, seq: int, triggered_at: float,
                                  decision: str, reason: str,
                                  active_session_id: Optional[str]) -> None:
        """Append a suppressed-trigger forensic row (best-effort, never blocks)."""
        try:
            from .config import RUNTIME_DIR
            from datetime import datetime
            import json as _json
            adir = Path(RUNTIME_DIR) / "audit"
            adir.mkdir(parents=True, exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            row = {
                "wall_clock": datetime.fromtimestamp(triggered_at).isoformat(timespec="milliseconds"),
                "trigger_sequence": seq,
                "decision": decision,
                "reason": reason,
                "active_session_id": active_session_id,
                "suppressed_trigger_count": self._suppressed_trigger_count,
            }
            with (adir / f"suppressed_triggers_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write(_json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _start_session(self, trigger_sequence: int, triggered_at: float) -> None:
        """Yangi Session ni yaratadi va kamera+RFID+watchdog ni ishga tushiradi."""
        from .config import PLC, RFID, SESSION
        self._ensure_session_runtime()
        session_id = _new_session_id()
        started_at = time.time()
        now_mono = time.monotonic()
        # Navbatda qancha kutdi? Kuzov kamera oldidan o'tib ketgan bo'lsa, kech
        # boshlanган capture NOTO'G'RI kuzovni o'qishi mumkin — bu VIN/kuzov
        # bog'lanishini buzadi (noto'g'ri VIN NO_READ dan xavfliroq).
        queue_wait_ms = max(0.0, (started_at - triggered_at) * 1000.0)
        max_capture_delay_ms = float(getattr(SESSION, "max_capture_delay_ms", 0) or 0)
        cam_delay = max(0.0, float(getattr(PLC, "camera_delay_sec", 0) or 0))
        capture_window = float(SESSION.timeout_sec)
        hard_hold = (bool(getattr(SESSION, "hold_until_deadline", False))
                     and bool(RFID.enabled) and bool(SESSION.require_rfid)
                     and not bool(SESSION.exit_signal_enabled))
        session = Session(
            session_id=session_id,
            trigger_sequence=trigger_sequence,
            triggered_at=triggered_at,
            started_at=started_at,
            deadline=(now_mono + capture_window if hard_hold
                      else now_mono + cam_delay + capture_window),
            state=SessionState.ACTIVE,
            d2222_at=triggered_at,
            # MVP exit-mode: D2223 kelmasa failsafe hard-deadline. Non-exit rejimda
            # ishlatilmaydi (watchdog `deadline` bo'yicha yopadi — backward compat).
            hard_deadline=now_mono + float(SESSION.max_session_duration_sec),
            queue_wait_ms=queue_wait_ms,
        )

        with self._session_lock:
            self._sessions[session_id] = session
            self._session_order.append(session_id)
            self._prune_session_history_locked()
            self._active_session_id = session_id
            self._capture_owner_id = session_id
            self._last_session_id = session_id
            self._session_start_ts = now_mono
            self._rfid_grace_applied = False
            self._ocr_attempts = 0
            self._ocr_inflight_session = None
            self._ocr_inflight_started = 0.0
            self._ocr_inflight_done = threading.Event()
            self._ocr_inflight_events[session_id] = self._ocr_inflight_done
            self._ocr_grace_active = False
            self._session_frames_seen = 0
            self._session_max_yolo_conf = 0.0
            self._session_best_crop_quality = 0.0
            self._session_ocr_triggered = 0
            self._mirror_legacy_locked(session)

        if not self._legacy_bare_runtime:
            try:
                db.insert_pending_session(
                    session_id, trigger_sequence,
                    datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S"),
                )
            except Exception as exc:
                log.error(f"[SESSION #{session_id}] PENDING yozuv xatosi "
                          f"(restart-recovery buzilishi mumkin): {exc}")

        log.info(f"[PLC] TRIGGER qabul qilindi (PLC=1) — trigger_sequence=#{trigger_sequence}.")

        # Capture oynasi yo'qolganmi? Navbatda juda uzoq turgan trigger uchun
        # kamerani ochish NOTO'G'RI kuzovni o'qish demakdir. Bunday sessiya
        # DARHOL, aniq sabab bilan yopiladi (jim yo'qotish EMAS) va capture
        # keyingi triggerga o'tadi.
        if max_capture_delay_ms > 0 and queue_wait_ms > max_capture_delay_ms:
            log.critical(
                f"[MISSED CAPTURE WINDOW] #{session_id} trigger #{trigger_sequence} "
                f"navbatda {queue_wait_ms:.0f}ms kutdi (chegara "
                f"{max_capture_delay_ms:.0f}ms) — kuzov kamera oldidan o'tib ketgan. "
                f"NOTO'G'RI kuzovni o'qimaslik uchun sessiya yopilmoqda."
            )
            session.failure_reason = "MISSED_CAPTURE_WINDOW"
            self._finalize_session(session_id, reason="MISSED_CAPTURE_WINDOW",
                                   failure_state=SessionState.MISSED_CAPTURE_WINDOW)
            return

        log.info(f"[SESSION #{session_id}] Boshlandi — timeout={SESSION.timeout_sec:.0f}s "
                 f"(navbatda kutish={queue_wait_ms:.0f}ms, VIN va RFID parallel).")

        # 1) RFID DARHOL, kamera bilan parallel boshlanadi.
        # P1 fix: `trigger_read()` qaytish qiymati (busy holati) ENDI TEKSHIRILADI
        # — band bo'lsa sessiya jim RFID'siz qolmaydi, failure_reason yoziladi.
        if self.rfid_service is not None and RFID.enabled:
            # EPC ownership uchun o'qish oynasining boshlanish vaqti qayd etiladi:
            # bundan OLDIN ko'rilgan teg oldingi kuzovga tegishli hisoblanadi.
            with self._session_lock:
                session.rfid_read_started_at = started_at
            try:
                try:
                    started = self.rfid_service.trigger_read(
                        self._on_rfid_result, stop_event=session.stop_event,
                        deadline=session.deadline, session_id=session_id,
                        started_at=started_at,
                    )
                except TypeError:
                    started = self.rfid_service.trigger_read(
                        lambda epc, number, ts, sid=session_id:
                            self._on_rfid_result(epc, number, ts, session_id=sid),
                        stop_event=session.stop_event, deadline=session.deadline,
                    )
                if started:
                    log.info(f"[SESSION #{session_id}] RFID o'qish boshlandi (parallel, retry).")
                else:
                    log.error(f"[SESSION #{session_id}] RFID BAND (busy) — bu sessiya uchun "
                              "RFID o'qish BOSHLANMADI (oldingi o'qish hali tugamagan).")
                    with self._session_lock:
                        session.failure_reason = "RFID_BUSY"
            except Exception as exc:
                log.error(f"[SESSION #{session_id}] RFID trigger_read xatosi: {exc}")
                with self._session_lock:
                    session.failure_reason = f"RFID_ERROR:{exc}"

        # 2) Kamera configured transport delay bilan boshlanadi. Qat'iy
        # production deadline delay sabab uzaymaydi.
        if cam_delay > 0:
            threading.Thread(
                target=self._deferred_camera_start, args=(session_id, cam_delay),
                name=f"cam-delay-{session_id}", daemon=True,
            ).start()
        else:
            try:
                if self._ensure_camera_stream():
                    self.start_processing()
                    self._arm_camera_search_watch(session_id)
            except Exception as exc:
                log.error(f"[SESSION #{session_id}] kamera start xatosi: {exc}")

        # 3) Watchdog: deadline yoki "done" gacha kutadi, so'ng finalize qiladi.
        # P2 fix: tashqi try/except bilan o'ralgan — kutilmagan xato watchdog'ni
        # jim o'ldirmaydi (loglanadi + sessiya FAILED bilan yopishga urinadi).
        watchdog = threading.Thread(
            target=self._session_watch, args=(session_id,),
            name=f"session-{session_id}", daemon=True,
        )
        with self._session_lock:
            session.watchdog = watchdog
            self._mirror_legacy_locked(session)
        watchdog.start()

    def _deferred_camera_start(self, session_id: str, delay: float) -> None:
        with self._session_lock:
            session = self._sessions.get(session_id)
        if session is None or session.stop_event.wait(delay):
            return
        with self._session_lock:
            if self._active_session_id != session_id or session.state != SessionState.ACTIVE:
                return
        try:
            if self._ensure_camera_stream():
                self.start_processing()
                self._arm_camera_search_watch(session_id)
        except Exception as exc:
            log.error(f"[SESSION #{session_id}] delayed camera start xatosi: {exc}")

    def _arm_camera_search_watch(self, session_id: str) -> None:
        from .config import PLC
        timeout = max(1.0, float(getattr(PLC, "camera_search_timeout_sec", 30) or 30))
        threading.Thread(
            target=self._camera_search_watch, args=(session_id, timeout),
            name=f"cam-search-{session_id}", daemon=True,
        ).start()

    def _camera_search_watch(self, session_id: str, timeout: float) -> None:
        with self._session_lock:
            session = self._sessions.get(session_id)
        if session is None or session.stop_event.wait(timeout):
            return
        with self._session_lock:
            if self._active_session_id != session_id or session.state != SessionState.ACTIVE:
                return
        self.stop_processing()
        self.pause_camera_stream()

    # ===============================================================
    # Pre-OCR observability: per-session zanjir hisoblagichlari
    # ===============================================================
    _FIRST_TS_FIELDS = {
        "payloads_received": "first_payload_at",
        "frames_decoded": "first_decoded_frame_at",
        "yolo_detection_count": "first_detection_at",
        "crops_created": "first_crop_at",
        "ocr_jobs_submitted": "first_ocr_submitted_at",
        "ocr_jobs_completed": "first_ocr_result_at",
    }

    def _bump_capture_counter(self, name: str, n: int = 1,
                              session_id: Optional[str] = None) -> None:
        """
        Capture zanjiri hisoblagichini oshiradi (default — CAPTURE EGASI sessiya).

        Per-frame LOG YOZMAYDI (log spam yo'q) — faqat hisoblagich. Yakunda
        bitta `[SESSION SUMMARY]` qatori chiqadi.
        """
        with self._session_lock:
            sid = session_id or self._capture_owner_id
            session = self._sessions.get(sid) if sid else None
            if session is None:
                return
            setattr(session, name, int(getattr(session, name, 0)) + int(n))
            ts_field = self._FIRST_TS_FIELDS.get(name)
            if ts_field is not None and getattr(session, ts_field, None) is None:
                setattr(session, ts_field, time.time())

    def _classify_failure_stage(self, session: "Session") -> Optional[str]:
        """
        Sessiya qaysi bo'g'inda yo'qolganini ANIQ aytadi. Muvaffaqiyatda None.

        Eng muhim ajratish: "OCR ishladi, lekin o'qimadi" (OCR_NO_READ) va
        "OCR umuman ishga tushmadi" (OCR_NOT_SUBMITTED) — bular boshqa-boshqa
        nosozliklar va boshqa-boshqa tuzatishni talab qiladi.
        `UNKNOWN` qaytarish — tasniflash mantig'idagi kamchilik belgisi.
        """
        if session.failure_reason == "MISSED_CAPTURE_WINDOW":
            return "MISSED_CAPTURE_WINDOW"
        if session.failure_reason == "DATABASE_FAILED":
            return "DATABASE_FAILURE"
        if session.vin:
            return None                                  # VIN olindi — muvaffaqiyat
        if session.payloads_received == 0:
            return "NO_PAYLOAD"
        if session.frames_decoded == 0:
            return "DECODE_FAILED"
        if session.yolo_detection_count == 0:
            return "NO_DETECTION"
        if session.crops_created == 0:
            return "NO_CROP"
        if session.crops_created > 0 and session.crops_created == session.crops_rejected:
            return "CROP_REJECTED"
        if session.ocr_jobs_submitted == 0:
            # Crop tayyor edi, lekin OCR ga yuborilmadi — INVARIANT buzilishi.
            return "OCR_NOT_SUBMITTED"
        if session.ocr_jobs_completed == 0:
            return "OCR_NOT_STARTED"
        if session.ocr_timeout_count > 0:
            return "OCR_TIMEOUT"
        if session.ocr_ambiguous_count > 0:
            return "OCR_AMBIGUOUS"
        return "OCR_NO_READ"

    def _log_session_summary(self, session: "Session") -> None:
        """Sessiya uchun AYNAN BITTA yakuniy diagnostika qatori (per-frame spam emas)."""
        trigger_to_capture_ms = (
            (session.first_payload_at - session.triggered_at) * 1000.0
            if session.first_payload_at and session.triggered_at else -1.0
        )
        log.info(
            "[SESSION SUMMARY] "
            f"session={session.session_id} "
            f"trigger_seq={session.trigger_sequence} "
            f"queue_wait_ms={session.queue_wait_ms:.0f} "
            f"trigger_to_capture_ms={trigger_to_capture_ms:.0f} "
            f"payloads={session.payloads_received} "
            f"decoded={session.frames_decoded} "
            f"decode_failures={session.decode_failures} "
            f"yolo_inferences={session.yolo_inference_count} "
            f"detections={session.yolo_detection_count} "
            f"crops={session.crops_created} "
            f"crops_rejected={session.crops_rejected} "
            f"ocr_submitted={session.ocr_jobs_submitted} "
            f"ocr_completed={session.ocr_jobs_completed} "
            f"vin={session.vin or 'NO_READ'} "
            f"rfid={session.rfid_epc or 'NO_TAG'} "
            f"state={session.state.value} "
            f"failure_stage={session.failure_stage or 'NONE'}"
        )
        if session.failure_stage == "OCR_NOT_SUBMITTED":
            log.critical(
                f"[CROP_AVAILABLE_BUT_OCR_NOT_SUBMITTED] #{session.session_id}: "
                f"{session.crops_created} crop tayyor edi "
                f"({session.crops_rejected} rad etilgan), lekin OCR ga birorta job "
                "yuborilmadi — SOFTWARE INVARIANT BUZILISHI."
            )

    def _reject_stale_rfid(self, session: "Session", result) -> bool:
        """
        Teg dalili SHU sessiyaga tegishlimi? True qaytsa — rad etilgan.

        Qoida: `first_seen_at` sessiyaning RFID o'qish oynasi boshlanishidan
        `RFID.stale_event_tolerance_ms` dan ko'proq OLDIN bo'lsa, teg oldingi
        kuzovdan qolgan hisoblanadi. Tolerance 0 bo'lsa tekshiruv o'chadi.
        """
        from .config import RFID
        tolerance_ms = float(getattr(RFID, "stale_event_tolerance_ms", 0) or 0)
        first_seen = getattr(result, "first_seen_at", None)
        with self._session_lock:
            read_started_at = session.rfid_read_started_at
            if first_seen is not None:
                try:
                    session.rfid_reader_event_at = first_seen.timestamp()
                except AttributeError:
                    session.rfid_reader_event_at = float(first_seen)
            event_at = session.rfid_reader_event_at
        if tolerance_ms <= 0 or read_started_at is None or event_at is None:
            return False
        age_ms = (read_started_at - event_at) * 1000.0
        if age_ms <= tolerance_ms:
            return False
        with self._session_lock:
            session.rfid_rejection_reason = "STALE_RFID_EVENT"
            self._metrics["late_rfid_results_total"] += 1
        log.warning(
            f"[STALE_RFID_EVENT] sessiya #{session.session_id}: EPC={result.epc} "
            f"o'qish oynasidan {age_ms:.0f}ms OLDIN ko'rilgan (chegara "
            f"{tolerance_ms:.0f}ms) — oldingi kuzovdan qolgan dalil, RAD ETILDI. "
            f"Yangi kuzov uchun fresh RFID dalili kutiladi."
        )
        return True

    def _validate_result_session(self, session_id: Optional[str], kind: str) -> Optional["Session"]:
        """
        Late-result / ownership tekshiruvi (P0-2 fix). `kind` — "ocr" yoki "rfid"
        (log/metrika kategoriyasi uchun). Qaytaradi: qabul qilinadigan bo'lsa
        Session obyekti, aks holda None (sabab loglanadi + metrika oshiriladi).
        """
        from .config import SESSION
        kind_up = kind.upper()
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None:
                log.error(f"[UNKNOWN_SESSION_RESULT] {kind_up}: noma'lum session_id="
                          f"{session_id!r} — rad etildi.")
                self._metrics[f"late_{kind}_results_total"] += 1
                return None
            if session.state in _TERMINAL_SESSION_STATES:
                log.warning(f"[FINALIZED_SESSION_RESULT_REJECTED] {kind_up}: sessiya "
                            f"#{session_id} allaqachon {session.state.value} — rad etildi.")
                self._metrics[f"late_{kind}_results_total"] += 1
                return None
            # --- MVP exit-mode: GRACE_WAIT sessiya FAOL bo'lmasa ham (overlap:
            # keyingi D2222 yangi sessiya ochgan) grace oynasi ichida natija
            # QABUL QILINADI (allow_result_after_d2223). Grace tugagach rad. ---
            if session.state in (SessionState.EXIT_SEEN, SessionState.GRACE_WAIT):
                if not (SESSION.exit_signal_enabled and SESSION.allow_result_after_d2223):
                    log.warning(f"[LATE_RESULT_REJECTED] {kind_up}: sessiya #{session_id} "
                                "GRACE_WAIT, lekin allow_result_after_d2223=false — rad.")
                    self._metrics[f"late_{kind}_results_total"] += 1
                    return None
                if session.grace_deadline is not None and time.monotonic() > session.grace_deadline:
                    log.warning(f"[LATE_RESULT_REJECTED] {kind_up}: sessiya #{session_id} "
                                "grace oynasidan keyin keldi — rad etildi.")
                    self._metrics[f"late_{kind}_results_total"] += 1
                    return None
                return session
            # Capture ownership sessiya umridan ajratilgani uchun (incident fix)
            # capture'ni topshirgan, lekin hali natija kutayotgan sessiya FAOL
            # bo'lmasligi mumkin. U O'Z natijasini qabul qilishda davom etadi —
            # aks holda decoupling OCR/RFID natijalarini yo'qotardi.
            # Ownership invarianti buzilmaydi: natija session_id bo'yicha
            # biriktiriladi va terminal sessiya baribir rad etadi.
            if session.state == SessionState.WAITING_RESULTS:
                return session
            if self._active_session_id != session_id:
                log.warning(f"[SESSION_MISMATCH] {kind_up}: session_id={session_id} faol "
                            f"sessiya emas (active={self._active_session_id}) — rad etildi.")
                self._metrics["session_mismatch_total"] += 1
                return None
            if time.monotonic() > session.deadline:
                log.warning(f"[LATE_RESULT_REJECTED] {kind_up}: sessiya #{session_id} "
                            "deadline'dan keyin keldi — rad etildi.")
                self._metrics[f"late_{kind}_results_total"] += 1
                return None
            return session

    def _on_rfid_result(self, result, number=None, ts=None, session_id=None) -> None:
        """RFID worker tugadi — egalik tekshiriladi (P0-2), so'ng sessiyaga yoziladi."""
        if not hasattr(result, "session_id"):
            class _LegacyRFIDResult:
                pass
            legacy = _LegacyRFIDResult()
            legacy.session_id = session_id
            legacy.epc = result or ""
            legacy.number = number or ""
            legacy.received_at = ts
            legacy.antenna = None
            legacy.rssi = None
            result = legacy
        with self._rfid_lock:
            self._current_rfid = {"raw": result.epc or "", "number": result.number or "",
                                   "ts": result.received_at}

        if result.session_id is None:
            # Sessiyasiz (qo'lda) trigger — /rfid/trigger endpoint on_done=None
            # yuboradi, shuning uchun bu shoxobcha amalda ishlamaydi; xavfsizlik
            # uchun saqlanadi.
            return

        session = self._validate_result_session(result.session_id, kind="rfid")
        if session is None:
            return

        # --- EPC ownership: eskirgan (oldingi kuzovdan qolgan) event rad etiladi ---
        # R700 oqimida teg maydonda turgan ekan qayta ko'rinadi; `first_seen_at`
        # sessiya o'qish oynasidan sezilarli oldin bo'lsa, bu YANGI kuzov uchun
        # dalil emas. Jim yutilmaydi: sabab sessiyada saqlanadi + loglanadi.
        if self._reject_stale_rfid(session, result):
            return

        with self._session_lock:
            session.rfid_epc = result.number or ""
            session.rfid_raw = (result.epc or "").strip() or None
            session.rfid_antenna = result.antenna
            session.rfid_rssi = result.rssi
            session.rfid_at = time.time()       # latency uchun (traceability)

        valid = result.number and result.number not in ("NO_TAG", "NO_READ")
        if valid:
            log.info(f"[RFID] DETECTED (sessiya #{result.session_id}): EPC={result.epc} "
                     f"-> RFID_EPC={result.number}")
        else:
            log.info(f"[RFID] o'qilmadi (sessiya #{result.session_id}) -> "
                     f"{result.number or 'NO_TAG'}")
        self._maybe_complete_session(result.session_id)

    # ===============================================================
    # Sessiya: to'liqlik tekshiruvi, watchdog, finalize (thread-safe)
    # ===============================================================
    def _maybe_complete_session(self, session_id: Optional[str] = None) -> None:
        """
        VIN VA (kerak bo'lsa) RFID muvaffaqiyatli bo'lsa — sessiyani SUCCESS bilan yopadi.

        MVP exit-mode (SESSION.exit_signal_enabled=True): VIN+RFID muvaffaqiyati
        sessiyani DARHOL yopMAYDI — D2223 (exit) yoki hard-deadline kutiladi
        (best-frame strategiyasi: birinchi VIN yakuniy emas). Faqat GRACE_WAIT
        holatida (D2223 keldi) ikkalasi ham tayyor bo'lsa erta yopiladi.
        """
        from .config import RFID, SESSION
        with self._session_lock:
            session_id = session_id or self._active_session_id
            session = self._sessions.get(session_id)
            if session is None or session.state in _TERMINAL_SESSION_STATES:
                return
            self._sync_legacy_locked(session)
            self._mirror_legacy_locked(session)
            exit_mode = bool(SESSION.exit_signal_enabled)
            # Non-exit rejimda faqat FAOL sessiya erta yopiladi (backward compat).
            if not exit_mode and self._active_session_id != session_id:
                return
            vin_ok = session.vin is not None
            rfid_required = bool(RFID.enabled) and bool(SESSION.require_rfid)
            rfid_ok = session.rfid_epc not in (None, "", "NO_TAG", "NO_READ")
            complete = vin_ok and (rfid_ok or not rfid_required)
            if (bool(getattr(SESSION, "hold_until_deadline", False))
                    and rfid_required and not exit_mode):
                return
            if exit_mode:
                # Exit rejimida ACTIVE holatda erta yopilmaydi — D2223/failsafe kutiladi.
                # GRACE_WAIT da ikkalasi ham tayyor bo'lsa gina grace'ni qisqartirib yopamiz.
                if session.state != SessionState.GRACE_WAIT:
                    complete = False
        if complete:
            self._finalize_session(session_id, reason="SUCCESS")

    def on_exit_signal(self, session_id: Optional[str] = None) -> None:
        """
        D2223 (EXIT) rising-edge ishlovi — MVP dual-signal grace-based close.

        Sessiyani DARHOL yopMAYDI: ACTIVE -> EXIT_SEEN -> GRACE_WAIT ga o'tkazadi,
        yangi frame qabul qilishni to'xtatadi (drop_new_frames), grace_deadline
        o'rnatadi. Davom etayotgan OCR/RFID natijasi grace ichida qabul qilinadi;
        grace tugagach watchdog deterministik FINALIZE qiladi.

        `session_id=None` bo'lsa joriy capture-owner (faol) sessiyaga qo'llanadi
        (PLC poll threadi shu tarzda chaqiradi — u faqat "exit keldi"ni biladi).
        """
        from .config import SESSION
        if not bool(SESSION.exit_signal_enabled):
            return
        grace_sec = max(0.0, float(SESSION.post_exit_grace_ms) / 1000.0)
        with self._session_lock:
            if session_id is None:
                session_id = self._active_session_id
            session = self._sessions.get(session_id) if session_id else None
            if session is None:
                log.warning("[D2223] EXIT signali keldi, lekin faol sessiya yo'q — "
                            "e'tiborsiz qoldirildi (bo'sh liniya yoki takroriy edge).")
                return
            if session.state in _TERMINAL_SESSION_STATES:
                log.info(f"[D2223] EXIT: sessiya #{session_id} allaqachon "
                         f"{session.state.value} — e'tiborsiz (idempotent).")
                return
            if session.state in (SessionState.EXIT_SEEN, SessionState.GRACE_WAIT):
                log.info(f"[D2223] EXIT: sessiya #{session_id} allaqachon grace'da "
                         "(takroriy D2223) — e'tiborsiz (idempotent).")
                return
            now_mono = time.monotonic()
            session.d2223_at = time.time()
            session.exit_seen_at = now_mono
            session.grace_deadline = now_mono + grace_sec
            session.state = SessionState.GRACE_WAIT
            # Capture-owner bo'shaydi: keyingi D2222 yangi sessiya ocha oladi (overlap).
            # Sessiya `_sessions` da qoladi -> grace natijalari hali qabul qilinadi.
            if self._active_session_id == session_id:
                self._active_session_id = None
            # D2223 = plastinka chiqdi = capture tugadi. Kamerani darhol ozod
            # qilamiz, aks holda navbatdagi trigger grace tugashini kutib qolardi.
            release_capture = self._capture_owner_id == session_id
        # Yangi frame qabul qilinmasin (bu sessiya uchun ishlov to'xtaydi).
        self.stop_processing()
        if release_capture:
            self._release_capture(session_id, reason="d2223_exit")
        log.info(f"[D2223] EXIT qayd etildi — sessiya #{session_id} GRACE_WAIT "
                 f"({grace_sec*1000:.0f}ms). Yangi frame qabul qilinmaydi; "
                 "davom etayotgan OCR/RFID natijasi kutilmoqda.")
        # Watchdog grace_deadline ni keyingi iteratsiyada ko'radi. Agar ikkalasi
        # (VIN+RFID) allaqachon tayyor bo'lsa — darhol yopamiz (grace'ni kutmaymiz).
        self._maybe_complete_session(session_id)

    def _session_watch(self, session_id: str) -> None:
        """
        Sessiya deadline gacha kutadi; vaqt tugasa TIMEOUT bilan finalize qiladi.
        P2 fix: tashqi try/except — kutilmagan xato bu threadni JIM O'LDIRMAYDI;
        loglanadi va sessiyani FAILED bilan yopishga urinadi (resurs oqishi oldi olinadi).
        """
        try:
            self._session_watch_inner(session_id)
        except Exception:
            log.error(f"[SESSION #{session_id}] Watchdog KUTILMAGAN XATO bilan to'xtadi!",
                      exc_info=True)
            try:
                self._finalize_session(session_id, reason="WATCHDOG_ERROR",
                                       failure_state=SessionState.FAILED)
            except Exception:
                log.error(f"[SESSION #{session_id}] Watchdog fallback finalize ham "
                          "muvaffaqiyatsiz.", exc_info=True)

    def _session_watch_inner(self, session_id: str) -> None:
        from .config import RFID, SESSION
        exit_mode = bool(SESSION.exit_signal_enabled)
        hard_hold = (bool(getattr(SESSION, "hold_until_deadline", False))
                     and bool(RFID.enabled) and bool(SESSION.require_rfid)
                     and not exit_mode)
        while True:
            with self._session_lock:
                session = self._sessions.get(session_id)
                if session is None or session.state in _TERMINAL_SESSION_STATES:
                    return
                # --- Effektiv deadline (grace_deadline > 0.25 granularity bilan
                # kuzatiladi — D2223 kelganda watchdog uni keyingi siklda ko'radi). ---
                if exit_mode:
                    if (session.state == SessionState.GRACE_WAIT
                            and session.grace_deadline is not None):
                        effective = session.grace_deadline
                        timeout_reason = "GRACE_EXPIRED"
                    else:
                        effective = session.hard_deadline    # D2223 kelmasa failsafe
                        timeout_reason = "D2223_TIMEOUT"
                else:
                    effective = session.deadline             # eski single-signal xatti-harakat
                    timeout_reason = "HARD_DEADLINE" if hard_hold else "TIMEOUT"
                remaining = effective - time.monotonic()
                done_event = session.done_event
            if remaining <= 0:
                break
            if done_event.wait(min(remaining, 0.25)):
                return                      # boshqa joyda finalize bo'ldi (SUCCESS)
        if timeout_reason == "HARD_DEADLINE":
            grace = max(0.0, float(getattr(SESSION, "ocr_inflight_grace_sec", 0) or 0))
            with self._session_lock:
                event = self._ocr_inflight_events.get(session_id)
                inflight = (self._ocr_inflight_session == session_id
                            and session.vin is None and event is not None
                            and not event.is_set())
                self._ocr_grace_active = bool(inflight and grace > 0)
            if inflight and grace > 0:
                event.wait(grace)
                with self._session_lock:
                    self._ocr_grace_active = False
        log.warning(f"[SESSION #{session_id}] {timeout_reason} — deterministik yopilmoqda.")
        self._finalize_session(session_id, reason=timeout_reason)

    def _prune_session_history_locked(self) -> None:
        """`_session_lock` ushlab turilganda chaqiriladi — sessiya tarixini chegaralaydi."""
        keep_open: list = []
        while len(self._session_order) > self._session_history_max:
            old_id = self._session_order.popleft()
            if old_id == self._active_session_id or old_id == self._capture_owner_id:
                keep_open.append(old_id)
                continue
            old = self._sessions.get(old_id)
            # Capture'ni topshirgan, lekin hali natija kutayotgan sessiya
            # (WAITING_RESULTS/GRACE_WAIT) TARIXDAN CHIQARILMAYDI — aks holda
            # uning OCR/RFID natijasi "noma'lum sessiya" sifatida rad etilardi.
            if old is not None and old.state not in _TERMINAL_SESSION_STATES:
                keep_open.append(old_id)
                continue
            self._sessions.pop(old_id, None)
        # Hali yopilmagan sessiyalar navbat boshiga qaytariladi (tartib saqlanadi).
        for sid in reversed(keep_open):
            self._session_order.appendleft(sid)

    def _finalize_session(self, session_id=None, reason: Optional[str] = None,
                          failure_state: Optional["SessionState"] = None) -> None:
        """
        Sessiyani BIR MARTA yopadi (idempotent — CAS-uslubida holat tekshiruvi):
        kamera + RFID to'xtatadi, resurslarni bo'shatadi, natijani HAR DOIM
        bazaga yozadi (UPDATE, PENDING qatorni yakunlaydi — P1 restart-recovery),
        PLC ni 0 ga tushiradi, so'ng NAVBATDAGI triggerni (agar bor bo'lsa)
        avtomatik boshlaydi (P0-1 fix). Har qanday threaddan xavfsiz chaqiriladi.
        """
        from .config import RFID, PLC, SESSION
        self._ensure_session_runtime()
        with self._session_lock:
            # v1.1.3 compatibility: _finalize_session("SUCCESS")
            if reason is None and session_id not in self._sessions:
                reason = str(session_id or "MANUAL")
                session_id = self._active_session_id
            elif reason is None:
                reason = "MANUAL"
            session = self._sessions.get(session_id)
            # CAS: FINALIZING/terminal holatlarda ikkinchi chaqiruv DARHOL qaytadi
            # (test 7/8: concurrent SUCCESS/TIMEOUT yoki ikki marta finalize -> bitta yozuv).
            if (session is None or session.state in _TERMINAL_SESSION_STATES
                    or session.state == SessionState.FINALIZING):
                return
            self._sync_legacy_locked(session)
            hard_hold = (bool(getattr(SESSION, "hold_until_deadline", False))
                         and bool(RFID.enabled) and bool(SESSION.require_rfid)
                         and not bool(SESSION.exit_signal_enabled))
            if (hard_hold and time.monotonic() < session.deadline
                    and reason not in ("HARD_DEADLINE", "WATCHDOG_ERROR",
                                       "TEST_FORCE_CLOSE", "MISSED_CAPTURE_WINDOW",
                                       "SERVICE_SHUTDOWN")):
                log.info(f"[SESSION #{session_id}] '{reason}' erta finalize rad etildi.")
                self._mirror_legacy_locked(session)
                return
            owns_capture = self._capture_owner_id == session_id
            session.state = SessionState.FINALIZING
            self._mirror_legacy_locked(session)

        session.stop_event.set()
        session.done_event.set()
        if owns_capture:
            self.stop_processing()
            try:
                self.pause_camera_stream()
            except Exception:
                pass

        # --- Natijalarni shakllantirish (qiymat HECH QACHON O'YLAB TOPILMAYDI) ---
        if session.vin is not None:
            vin_val = session.vin; conf = session.vin_confidence; rel_path = session.vin_rel_path
            raw_vin = session.vin_raw; model = session.vin_model
        else:
            vin_val = "NO_READ"; conf = 0.0; rel_path = None
            raw_vin = None; model = None

        if not RFID.enabled:
            rfid_epc = None; rfid_raw = None
        elif session.rfid_epc:
            rfid_epc = session.rfid_epc or "NO_READ"
            rfid_raw = session.rfid_raw
        else:
            # RFID yoqilgan, lekin natija umuman kelmadi -> NO_TAG
            rfid_epc = "NO_TAG"; rfid_raw = None

        vin_done = session.vin is not None
        rfid_done = (not RFID.enabled) or (rfid_epc not in (None, "NO_TAG", "NO_READ"))
        # RFID o'chiq bo'lsa "partial" tushunchasi VIN'ga bog'liq (RFID hisobga olinmaydi).
        vin_present = session.vin is not None
        rfid_present = RFID.enabled and (rfid_epc not in (None, "NO_TAG", "NO_READ"))
        if failure_state is not None:
            final_state = failure_state
        elif SESSION.exit_signal_enabled:
            # --- MVP exit-mode: batafsil statuslar (PARTIAL_SUCCESS) ---
            if vin_done and rfid_done:
                final_state = SessionState.SUCCESS
            elif vin_present and not rfid_present:
                final_state = SessionState.PARTIAL_SUCCESS
                session.failure_reason = session.failure_reason or "VIN_OK_RFID_TIMEOUT"
            elif rfid_present and not vin_present:
                final_state = SessionState.PARTIAL_SUCCESS
                session.failure_reason = session.failure_reason or "RFID_OK_VIN_TIMEOUT"
            else:
                final_state = SessionState.TIMEOUT
                session.failure_reason = session.failure_reason or "BOTH_TIMEOUT"
        else:
            # Eski single-signal xatti-harakat (backward compat): SUCCESS yoki TIMEOUT.
            final_state = SessionState.SUCCESS if (vin_done and rfid_done) else SessionState.TIMEOUT

        ts = datetime.now()
        ts_iso = ts.strftime("%Y-%m-%d %H:%M:%S")
        # --- Traceability: D2222/D2223 timestamp + latency (epoch -> ms) ---
        def _iso(epoch):
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else None
        started = session.started_at
        ocr_latency = ((session.vin_at - started) * 1000.0
                       if session.vin_at and started else None)
        rfid_latency = ((session.rfid_at - started) * 1000.0
                        if session.rfid_at and started else None)
        total_ms = ((time.time() - session.triggered_at) * 1000.0
                    if session.triggered_at else None)
        db_failed = False
        try:
            rec_id = db.finalize_session_record(
                session_id, timestamp=ts_iso, vin=vin_val, confidence=conf, image_path=rel_path,
                raw_vin=raw_vin, model=model, status=final_state.value,
                rfid_epc=rfid_epc, rfid_raw=rfid_raw,
                session_finalized_at=ts_iso, failure_reason=session.failure_reason,
                d2222_timestamp=_iso(session.d2222_at), d2223_timestamp=_iso(session.d2223_at),
                ocr_latency_ms=(round(ocr_latency, 1) if ocr_latency is not None else None),
                rfid_latency_ms=(round(rfid_latency, 1) if rfid_latency is not None else None),
                total_session_ms=(round(total_ms, 1) if total_ms is not None else None),
            )
            if vin_present:
                self.stats["vins"] += 1

            if rfid_present:
                self.stats["rfid_reads"] += 1
            log.info(f"[DB] Saqlandi #{rec_id}: VIN={vin_val} model={model} "
                     f"RFID_EPC={rfid_epc} STATUS={final_state.value}")
            log.info(f"[SESSION #{session_id}] {final_state.value} (sabab={reason}).")
        except sqlite3.IntegrityError:
            log.error(f"[SESSION #{session_id}] DB: bu session_id uchun yozuv ALLAQACHON "
                      "mavjud — ikkinchi finalize urinishi e'tiborsiz qoldirildi (idempotent).")
        except Exception as exc:
            # Tier-8: DB yozuvi xato bersa sessiya JIMGINA "SUCCESS" bo'lib
            # QOLMAYDI — DATABASE_FAILED sifatida belgilanadi + metrika (operator
            # noto'g'ri "muvaffaqiyat" ko'rmasligi uchun).
            db_failed = True
            with self._session_lock:
                self._metrics["database_write_failure_total"] += 1
            log.critical(f"[SESSION_DB_WRITE_FAILED] #{session_id}: DB yozuv xatosi: {exc} "
                         "-> DATABASE_FAILED (JIM SUCCESS EMAS).")

        next_trigger: Optional[Tuple[int, float]] = None
        with self._session_lock:
            if db_failed:
                final_state = SessionState.FAILED
                session.failure_reason = "DATABASE_FAILED"
            session.finalized_at = time.time()
            session.state = final_state
            if self._active_session_id == session_id:
                self._active_session_id = None
            # Fallback: agar sessiya capture'ni hali ushlab turган bo'lsa (plate
            # exit yo'li ishlamagan — mas. kamera umuman kadr bermagan), capture
            # shu yerda bo'shatiladi. Odatda `_release_capture()` allaqachon
            # bo'shatgan bo'ladi va bu shart bajarilmaydi.
            if self._capture_owner_id == session_id:
                self._capture_owner_id = None
            session.failure_stage = self._classify_failure_stage(session)
            self._mirror_legacy_locked(session)
            next_trigger = (self._pending_triggers.popleft()
                            if self._capture_owner_id is None and self._pending_triggers
                            else None)

        # Sessiya uchun AYNAN BITTA diagnostika qatori (incident forensikasi).
        self._log_session_summary(session)

        # ONE-SHOT: PLC signalini 0 ga tushiramiz (kamera o'chadi). Haqiqiy teardown
        # PLC poll threadida bajariladi — bu thread bloklanmaydi.
        if PLC.auto_off_on_vin and self.on_vin_done is not None:
            try:
                self.on_vin_done()
            except Exception as exc:
                log.error(f"[SESSION #{session_id}] on_vin_done (PLC auto-off) xatosi: {exc}")

        # P0-1 fix: navbatdagi trigger (agar bor bo'lsa) AVTOMATIK, TARTIBDA boshlanadi.
        if next_trigger is not None:
            seq, triggered_at = next_trigger
            log.info(f"[QUEUE] Navbatdagi trigger #{seq} avtomatik boshlanmoqda "
                     f"(navbatda qolgan={len(self._pending_triggers)}).")
            self._start_session(seq, triggered_at)

    def prepare_shutdown(self) -> None:
        """Reject queued work and durably cancel every nonterminal session."""
        self._ensure_session_runtime()
        with self._session_lock:
            dropped = len(self._pending_triggers)
            self._pending_triggers.clear()
            open_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.state not in _TERMINAL_SESSION_STATES
                and session.state != SessionState.FINALIZING
            ]
            for session_id in open_ids:
                self._sessions[session_id].failure_reason = "SERVICE_SHUTDOWN"
            self._dropped_trigger_count += dropped
        if dropped:
            log.warning(
                f"[SHUTDOWN] {dropped} queued trigger(s) cancelled before exit."
            )
        for session_id in open_ids:
            self._finalize_session(
                session_id,
                reason="SERVICE_SHUTDOWN",
                failure_state=SessionState.CANCELLED,
            )

    def plc_off(self) -> None:
        """
        PLC signal=0: kamera O'CHADI (PLC = kamera ON/OFF tugmasi).
        Ishlov berish to'xtaydi va kamera uziladi. Modellar xotirada qoladi
        (PLC faqat oqimni boshqaradi). full_stop_on_zero=False bo'lsa faqat AI
        to'xtaydi (kamera oqimi ko'rinishda qoladi — kamdan-kam holat).
        """
        from .config import PLC, SESSION
        self._ensure_session_runtime()
        # Agar sessiya hali faol bo'lsa (operator PLC=0 qildi yoki tashqi to'xtatish),
        # sessiyani majburan yopamiz — natija baribir bazaga yoziladi (TIMEOUT).
        with self._session_lock:
            sid = self._active_session_id
            session = self._sessions.get(sid) if sid else None
            session_running = session is not None and session.state not in _TERMINAL_SESSION_STATES
        if session_running and bool(getattr(SESSION, "hold_until_deadline", False)):
            log.info("PLC stop sessiya davomida e'tiborsiz — qat'iy deadline kutiladi.")
            return
        if session_running:
            self._finalize_session(sid, reason="PLC=0")
        self.stop_processing()
        if PLC.full_stop_on_zero:
            self.disconnect_camera()         # kamera o'chadi (to'liq idle)

    # ===============================================================
    # PF3/PF5: Capture loop — FAQAT o'qish+dekod, DOIM eng yangi kadr
    # ===============================================================
    def _capture_loop(self) -> None:
        """
        Kameradan kadr oqimini o'qiydi va DOIM eng yangi kadrni saqlaydi (eski
        tashlanadi). Socket buferida backlog yig'ilmaydi -> latency past (eskirgan
        kadr OCR ga ketmaydi). YOLO/kodlash bu yerda EMAS (inference threadда).
        """
        try:
            self.client.start_stream()            # sMN mLIStart 0
        except Exception as exc:
            log.error(f"start_stream xatosi: {exc}")
            with self._camera_lock:
                self._camera_last_error = str(exc)
            self._capture_alive = False
            log.warning("[CAMERA] start_stream ishlamadi - keyingi triggerda reconnect qilinadi.")
            return

        errors = 0
        timeout_count = 0
        while not self._stop_event.is_set():
            if self._capture_pause.is_set():
                # Plastinka kadrdan chiqqan — BLOB dan kadr O'QIMAYMIZ (real
                # "frame olish to'xtadi"). Socket/CoLa ulanish ochiq qoladi —
                # keyingi mashinada to'liq qayta ulanish shart emas.
                if self._stop_event.wait(0.1):
                    break
                continue
            try:
                payload = self.client.read_stream_frame()
                with self._camera_lock:
                    self._last_frame_received_at = time.time()
                    self._consecutive_timeouts = 0
                    self._consecutive_errors = 0
                # SICK W×H hint (mDIGetEffImgSize) — container topilmasa RAW fallback
                hint_wh = (getattr(self.client, "img_width", 0),
                           getattr(self.client, "img_height", 0))
                self._bump_capture_counter("payloads_received")
                img, dinfo = decode_payload(payload, hint_wh=hint_wh)   # ROBUST dekod
                if img is None:
                    # Eski SICK BMP oqimi va watchdog compatibility uchun
                    # proven decoder fallback.
                    img = decode_bmp(payload)
                    if img is not None:
                        dinfo = {"format": "BMP_LEGACY", "shape": tuple(img.shape),
                                 "len": len(payload)}
                if img is None:
                    # Frame keldi, lekin DECODE BO'LMADI — jim o'tib ketmaymiz (task 3)
                    self._bump_capture_counter("decode_failures")
                    self._decode_fail_streak += 1
                    self._decode_fail_total += 1
                    with self._camera_lock:
                        self._consecutive_decode_errors += 1
                        decode_errors = self._consecutive_decode_errors
                    self._maybe_dump_payload(payload, dinfo)   # ground-truth diagnostika
                    # Dastlabki fail'lar + har 30-da diagnostika (log spam bo'lmasin)
                    if self._decode_fail_streak <= 10 or self._decode_fail_streak % 30 == 0:
                        log.warning(
                            "[CAMERA] decode FAIL #%d: payload=%d B, magic offsetlar "
                            "BMP=%d JPEG=%d PNG=%d",
                            self._decode_fail_streak, dinfo.get("len", 0),
                            dinfo.get("bmp_off", -1), dinfo.get("jpg_off", -1),
                            dinfo.get("png_off", -1))
                        if dinfo.get("hexdump"):
                            log.warning("[CAMERA] payload boshi (128B):\n%s", dinfo["hexdump"])
                    if self._decode_fail_streak in (5, 10) or self._decode_fail_streak % 50 == 0:
                        log.error("[CAMERA] %d ketma-ket frame decode bo'lmadi — "
                                  "kamera image output formati mos emas "
                                  "(BMP/JPEG/PNG topilmadi). Kamera 'Image transfer' "
                                  "formatini tekshiring.", self._decode_fail_streak)
                    if decode_errors >= int(CAMERA.max_consecutive_timeouts):
                        with self._camera_lock:
                            self._camera_state = CameraState.FAILED
                            self._camera_connected = False
                        break
                    continue
                # Decode OK — fail streak tozalanadi; format o'zgarsa log (camera_client da)
                if self._decode_fail_streak:
                    log.info("[CAMERA] decode tiklandi (oldin %d fail) — format=%s shape=%s",
                             self._decode_fail_streak, dinfo.get("format"), dinfo.get("shape"))
                self._decode_fail_streak = 0
                self._last_decode_format = dinfo.get("format")
                with self._camera_lock:
                    self._consecutive_decode_errors = 0
                    self._last_successful_decode_at = time.time()
                    self._camera_connected = True
                    self._camera_state = CameraState.CONNECTED
                frame = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img
                # Eng yangi kadrни almashtiramiz (eskisini tashlaб) — drop-old
                with self._capture_lock:
                    self._bump_capture_counter("frames_decoded")
                    self._latest_frame = frame
                    self._latest_frame_id += 1
                self._last_frame_ts = time.time()
                errors = 0
                timeout_count = 0
            except TimeoutError:
                if self._stop_event.is_set():
                    break
                timeout_count += 1
                with self._camera_lock:
                    self._consecutive_timeouts = timeout_count
                    if self._camera_state == CameraState.CONNECTED:
                        self._camera_state = CameraState.DEGRADED
                log.warning(f"Frame timeout #{timeout_count} — kutilmoqda...")
                if timeout_count >= int(CAMERA.max_consecutive_timeouts):
                    log.error(f"{timeout_count} timeout — kamera push qilmayapti, "
                              "capture to'xtatildi.")
                    with self._camera_lock:
                        self._camera_state = CameraState.STALE
                        self._camera_connected = False
                    break
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                errors += 1
                with self._camera_lock:
                    self._consecutive_errors = errors
                    self._camera_last_error = str(exc)
                log.error(f"Capture xatosi #{errors}: {exc}")
                if errors >= int(CAMERA.max_consecutive_timeouts):
                    log.error(f"{errors} xato — capture to'xtatildi.")
                    with self._camera_lock:
                        self._camera_state = CameraState.FAILED
                        self._camera_connected = False
                    break
                time.sleep(0.1)

        self._capture_alive = False
        if not self._stop_event.is_set():
            log.warning("[CAMERA] capture stream to'xtadi - keyingi triggerda reconnect qilinadi.")
        try:
            self.client.stop_stream()
        except Exception:
            pass

    _payload_dumped = False

    def _maybe_dump_payload(self, payload: bytes, dinfo: dict) -> None:
        """
        Decode bir necha marta ketma-ket muvaffaqiyatsiz bo'lsa, BITTA xom payloadни
        diskka saqlaydi (logs/failed_payload_*.bin). Keyin uni offline tahlil qilish:
            python tools/inspect_camera_payload.py logs/failed_payload_XXXX.bin
        Bu kamera ASLIDA qanday format yuborayotganini aniqlashga yordam beradi
        (takror muammoning oldini olish — ground truth). Faqat BIR MARTA saqlaydi.

        """
        if self._payload_dumped or self._decode_fail_streak != 3:
            return
        try:
            from .config import LOGS_DIR
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fpath = Path(LOGS_DIR) / f"failed_payload_{ts}.bin"
            with open(fpath, "wb") as f:
                f.write(payload)
            self._payload_dumped = True
            log.error("[CAMERA] xom payload diagnostika uchun saqlandi: %s "
                      "(len=%d). Tahlil: python tools/inspect_camera_payload.py %s",
                      fpath, len(payload), fpath)
        except Exception as exc:
            log.warning("[CAMERA] payload dump xatosi: %s", exc)

    # ===============================================================
    # PF3/PF5: Inference loop — eng yangi kadrда YOLO/trigger + MJPEG
    # ===============================================================
    def _inference_loop(self) -> None:
        """
        Capture saqlagan ENG YANGI kadrни oladi (oraliq kadrlar o'tkazib yuboriladi)
        va YOLO/trigger + MJPEG kodlashni O'Z TEZLIGIDA bajaradi. Sekin YOLO live
        ko'rinish FPS ini bloklamaydi; OCR ga DOIM eng yangi kadr boradi.
        """
        last_id = 0
        while not self._stop_event.is_set():
            # Capture o'lgan bo'lsa (5 timeout/xato) — inference ham to'xtaydi
            if not self._capture_alive and self._latest_frame is None:
                break
            with self._capture_lock:
                fid = self._latest_frame_id
                frame = self._latest_frame
            if frame is None or fid == last_id:
                # Yangi kadr yo'q — qisqa kutamiz (CPU ni bo'shatamiz)
                if self._stop_event.wait(0.005):
                    break
                if not self._capture_alive:
                    break
                continue
            if fid - last_id > 1:                 # oraliq kadrlar tashlandi (latency himoyasi)
                self._dropped_frames += (fid - last_id - 1)
            last_id = fid

            try:
                annotated = frame
                if self._processing and self.detector.ready:
                    dets = self.detector.detect(frame)
                    self._bump_capture_counter("yolo_inference_count")
                    if dets:
                        self._bump_capture_counter("yolo_detection_count", len(dets))
                        self.stats["detections"] += 1
                        with self._camera_lock:
                            self._last_detection_at = time.time()
                        annotated = self.detector.draw_boxes(frame, dets)
                        self.collector.maybe_collect(frame, dets)
                    # Single-shot trigger (debounce / state-lock) — eng yangi kadr
                    self._handle_trigger(frame, dets)

                ok, buf = cv2.imencode(
                    ".jpg", annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), SERVER.jpeg_quality],
                )
                if ok:
                    with self._frame_lock:
                        self._live_jpeg = buf.tobytes()

                self.stats["frames"] += 1
                self._tick_fps()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                log.error(f"Inference xatosi: {exc}")
                time.sleep(0.05)

    # ===============================================================
    # Single-shot trigger (debounce / state-lock)
    # ===============================================================
    def _submit_ocr_frames(self, crops: list, reason: str) -> bool:
        """Session-scoped OCR submit + in-flight ownership/telemetry."""
        if not crops:
            return False
        with self._session_lock:
            # Kadrlar CAPTURE EGASIGA tegishli. Capture ownership sessiya umridan
            # ajratilgani uchun (incident fix) egalikni aynan shu yerdan olamiz —
            # aks holda crop natija kutayotgan boshqa sessiyaga yozilishi mumkin.
            sid = self._capture_owner_id or self._active_session_id
            session = self._sessions.get(sid) if sid else None
            if (session is not None and
                    (session.state in _TERMINAL_SESSION_STATES
                     or session.state == SessionState.FINALIZING
                     or self._ocr_attempts >= DETECTION.ocr_session_max_triggers)):
                return False
            if session is not None:
                try:
                    conf_hint = float(self._session_max_yolo_conf or 1.0)
                    best_quality = max(
                        float(quality_score(crop, conf_hint)[0])
                        for crop in crops
                    )
                except Exception:
                    best_quality = float(self._session_best_crop_quality or 0.0)
                retry_decision = self._ocr_retry_guard.evaluate(
                    sid,
                    crops,
                    quality=best_quality,
                    remaining_sec=max(0.0, session.deadline - time.monotonic()),
                    plate_exited=bool(session.plate_exit_at),
                )
                session.last_evidence_signature = retry_decision.signature
                if not retry_decision.allowed:
                    session.ocr_terminal_reason = retry_decision.reason
                    if retry_decision.reason == STABLE_AMBIGUITY:
                        session.ocr_ambiguous_count += 1
                    log.warning(
                        f"[OCR RETRY] session=#{sid} terminal="
                        f"{retry_decision.reason}; unchanged/insufficient evidence."
                    )
                    return False
            self._ocr_attempts += 1
            self._session_ocr_triggered += 1
            self._ocr_inflight_session = sid
            self._ocr_inflight_started = time.monotonic()
            event = self._ocr_inflight_events.setdefault(sid, threading.Event()) if sid else self._ocr_inflight_done
            event.clear()
            self._ocr_inflight_done = event
            attempt = self._ocr_attempts
        self._bump_capture_counter("ocr_jobs_submitted", session_id=sid)
        # v1.2.1 common-input insertion point. This call only schedules work:
        # ENGRAVED + real RAW Paddle + real ENHANCED Paddle all receive this
        # session-owned crop before legacy acceptance/ambiguity is known.
        if sid is not None:
            try:
                from .ai.ocr_shadow_hook import dispatch_shadow_ocr_input

                dispatch_shadow_ocr_input(
                    session_id=sid,
                    crop=crops[0],
                    capture_generation=self._latest_frame_id,
                )
            except Exception:
                pass
        self.ocr.submit_frames(crops, session_id=sid)
        self._plate_locked = True
        self._last_ocr_ts = time.monotonic()
        log.info(f"[OCR TRIGGER] reason={reason} crops={len(crops)} attempt={attempt} "
                 f"session=#{sid} max_yolo={self._session_max_yolo_conf:.3f} "
                 f"best_q={self._session_best_crop_quality:.3f}.")
        return True

    def _handle_trigger(self, frame: np.ndarray, dets: list) -> None:
        """
        Multi-frame fusion + 2-frame gating. OCR bitta plastinka hodisasi uchun
        FAQAT BIR MARTA, eng yaxshi kadrlar to'plamida ishga tushadi.

        Mantiq:
          * Plastinka yo'q (absent) -> rearm hisoblagich; yetarli bo'lsa hodisa
            tugaydi (qulf ochiladi, bufer tozalanadi) -> keyingi avtomobil.
          * Yuqori ishonch (>=0.95) kadr -> margin bilan crop, sifati baholanadi,
            buferga (eng yaxshilari) yig'iladi; confirm hisoblagich oshadi.
          * confirm >= ocr_confirm_frames VA eng yaxshi crop sifati yetarli VA
            cooldown o'tgan bo'lsa -> top fusion_k crop OCR ga (ovoz berish)
            yuboriladi, qulf YOPILADI.
        """
        with self._session_lock:
            session = (self._sessions.get(self._active_session_id)
                       if self._active_session_id else None)
            if session is not None and session.state not in _TERMINAL_SESSION_STATES:
                self._session_frames_seen += 1
                for det in dets:
                    try:
                        self._session_max_yolo_conf = max(
                            self._session_max_yolo_conf, float(det[4]))
                    except Exception:
                        pass

        if len(dets) == 0:
            # Kadrda plastinka yo'q — hodisa tugaganini tasdiqlash uchun sanaymiz
            self._absent_frames += 1
            if (self._plate_locked or self._confirm > 0) \
                    and self._absent_frames >= DETECTION.ocr_rearm_absent_frames:
                was_locked = self._plate_locked
                if (not was_locked and bool(getattr(DETECTION, "ocr_submit_on_exit", True))
                        and self._event_buf
                        and self._event_buf[0][1] >= DETECTION.min_crop_quality):
                    top = [c for c, _q in self._event_buf[:DETECTION.fusion_k]]
                    was_locked = self._submit_ocr_frames(top, reason="plate_exit_single_frame")
                self._reset_event()
                log.info("Plastinka kadrdan ketdi — hodisa yopildi (re-arm).")
                # YANGI: plastinka kadrdan chiqqach YOLO/OCR ishlov berishni
                # DARHOL to'xtatamiz (kamerani navbatdagi trigger gacha bekorga
                # band qilib turmaslik uchun). Xavfsiz: agar OCR ALLAQACHON shu
                # hodisa uchun yuborilgan bo'lsa ham (was_locked=True), u FON
                # navbatida ishlayotgan job'ni to'xtatmaydi — faqat YANGI ish
                # qabul qilishni/YOLO kadr uzatishni to'xtatadi. ocr.stop()
                # navbatdagi (hali boshlanmagan) ishlarni tozalaydi, lekin
                # allaqachon ishga tushgan OCR hisoblashini KECHIKTIRMAYDI —
                # natija (_on_ocr_result) baribir keladi.
                if was_locked:
                    log.info("[CAMERA] Plastinka ketdi, OCR allaqachon yuborilgan — "
                             "natija fonda kutiladi, YOLO/oqim shu sessiya uchun to'xtatiladi.")
                else:
                    log.info("[CAMERA] Plastinka OCR chegarasiga yetmasdan ketdi — "
                             "ishlov berish to'xtatiladi.")
                self.stop_processing()
                # SO'ROV: "YOLO to'xtayapti, lekin kamera kadr olishni davom
                # ettiryapti" — chunki stop_processing() faqat YOLO/OCR ni
                # o'chiradi, capture loop esa BLOB dan doim kadr o'qishda
                # davom etardi. Endi kadr olishning O'ZINI ham pauza qilamiz
                # (mLIStop) — TCP ulanish ochiq qoladi, keyingi mashinada
                # to'liq qayta ulanish shart emas (start_processing() buni
                # avtomatik davom ettiradi).
                self.pause_camera_stream()
                # CAPTURE OWNERSHIP BO'SHATILADI (incident fix): kamera endi
                # keyingi kuzov uchun ozod. Sessiya YOPILMAYDI — yuborilgan OCR
                # va RFID grace fonda davom etadi va sessiya o'z deadline'i
                # bo'yicha mustaqil finalize bo'ladi. Ilgari navbatdagi trigger
                # aynan shu nuqtada 20-22 soniya kutib qolar edi.
                owner = self._capture_owner_id
                if owner is not None:
                    self._release_capture(owner, reason="plate_exit")
            return

        self._absent_frames = 0
        if self._plate_locked:
            return  # bu hodisa uchun OCR allaqachon yuborilgan

        high = self.detector.best_detection(dets)   # conf >= ocr_trigger_conf (0.95)
        if high is None:
            return  # plastinka bor, lekin yuqori ishonch yo'q — kutamiz

        # Margin bilan crop -> sifat balli -> buferga (eng yaxshilarini saqlaymiz)
        crop = self.detector.crop_with_margin(frame, high, DETECTION.ocr_crop_margin_frac)
        if crop is None:
            return
        score, _metrics = quality_score(crop, high[4])
        # Pre-OCR telemetriya: crop yaratildi; sifat gate'idan o'tmasa alohida
        # sanaladi (NO_CROP va CROP_REJECTED ni ajratish uchun).
        self._bump_capture_counter("crops_created")
        if score < DETECTION.min_crop_quality:
            self._bump_capture_counter("crops_rejected")
        with self._session_lock:
            self._session_best_crop_quality = max(self._session_best_crop_quality, float(score))
        self._event_buf.append((crop, score))
        self._event_buf.sort(key=lambda x: x[1], reverse=True)
        del self._event_buf[DETECTION.event_buffer_max:]
        self._confirm += 1

        # Trigger sharti: yetarli tasdiq + sifat + cooldown
        now = time.monotonic()
        best_q = self._event_buf[0][1]
        if (self._confirm >= DETECTION.ocr_confirm_frames
                and best_q >= DETECTION.min_crop_quality
                and (now - self._last_ocr_ts) >= DETECTION.ocr_cooldown_sec):
            top = [c for c, _ in self._event_buf[:DETECTION.fusion_k]]
            # Model YOLO dan EMAS — OCR dan keyin VIN prefiksidan aniqlanadi.
            # session_id bilan: eski sessiya cropi yangi sessiyaga yozilmaydi.
            if self._submit_ocr_frames(top, reason="confirmed_fusion"):
                log.info(f"TRIGGER (fusion): {self._confirm} tasdiq kadr, "
                         f"best_q={best_q:.2f} -> top-{len(top)} crop OCR ga yuborildi.")

    def _reset_event(self) -> None:
        """Plastinka hodisasini yopadi: qulfni ochadi, buferni tozalaydi (re-arm)."""
        self._plate_locked = False
        self._confirm = 0
        self._event_buf = []

    def _tick_fps(self) -> None:
        now = time.monotonic()
        self._ts_buf.append(now)
        if len(self._ts_buf) > 60:
            self._ts_buf = self._ts_buf[-60:]
        if len(self._ts_buf) >= 2:
            self.stats["fps"] = round(
                (len(self._ts_buf) - 1) / (self._ts_buf[-1] - self._ts_buf[0]), 1
            )

    # ===============================================================
    # Callback: OCR yaroqli (dublikat bo'lmagan) VIN qaytardi
    # ===============================================================
    def _on_ocr_fail(self, session_id, raw) -> None:
        """
        OCR yaroqli VIN bermadi (yoki model=None rad bo'ldi). Plastinka qulfini
        QAYTA OCHAMIZ — shu sessiyada qayta urinish mumkin bo'lsin (bitta failed OCR
        butun sessiyani bloklab qo'ymasin). Urinishlar soni cheklangan.
        """
        # OCR HAQIQATAN ishladi, faqat VIN chiqmadi — bu "OCR ishga tushmadi"
        # dan tubdan farq qiladi (failure_stage: OCR_NO_READ vs OCR_NOT_SUBMITTED).
        if session_id is not None:
            self._bump_capture_counter("ocr_jobs_completed", session_id=session_id)
            self._bump_capture_counter("ocr_no_read_count", session_id=session_id)
        with self._session_lock:
            if session_id == self._ocr_inflight_session:
                self._ocr_inflight_session = None
            event = self._ocr_inflight_events.get(session_id, self._ocr_inflight_done)
            event.set()
            session = self._sessions.get(session_id) if session_id is not None else None
            active = (session_id is None or
                      (session is not None and self._active_session_id == session_id
                       and session.state not in _TERMINAL_SESSION_STATES
                       and session.state != SessionState.FINALIZING))
            have_vin = session is not None and session.vin is not None
            attempts = self._ocr_attempts
            plate_exited = bool(
                session is not None
                and (
                    session.plate_exit_at is not None
                    or (
                        session.capture_completed_at is not None
                        and self._capture_owner_id != session_id
                    )
                )
            )
            if session is not None:
                self._ocr_retry_guard.record_outcome(session_id, raw or "")
        # P4: eski/yakunlangan sessiya fail'i -> e'tiborsiz (qulfni ham ochmaymiz)
        if session_id is not None and not active:
            return
        if have_vin:
            return  # VIN allaqachon bor — qayta urinish shart emas
        if plate_exited:
            with self._session_lock:
                if session is not None:
                    session.ocr_terminal_reason = (
                        OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT
                    )
                self._ocr_retry_guard.mark_plate_exit(session_id)
            log.warning(
                f"[OCR] {OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT} "
                f"(session=#{session_id}, raw='{raw}'). "
                "Capture ownership qayta olinmaydi; evidence collectorga yuborilgan."
            )
            # OCR channel is terminal. If RFID is not a remaining dependency,
            # close now instead of idling until HARD_DEADLINE.
            from .config import RFID
            if not RFID.enabled or (
                session is not None
                and session.rfid_epc not in (None, "", "NO_TAG", "NO_READ")
            ):
                self._finalize_session(
                    session_id,
                    reason=OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT,
                )
            return
        if attempts >= DETECTION.ocr_session_max_triggers:
            log.warning(f"[OCR] Sessiyada {attempts} urinish — chegara; qulf ochilmaydi.")
            return
        # Qulfni ochamiz: keyingi yuqori-ishonch kadr (cooldowndan keyin) qayta yuboradi.
        self._plate_locked = False
        log.info(f"[OCR] yaroqli VIN chiqmadi (xom='{raw}') — qulf qayta ochildi, retry.")

    def _on_ocr_result(self, result, confidence: float = None, crop: np.ndarray = None,
                       raw_vin: str = None, model: str = None, session_id=None) -> None:
        """
        OCR yaroqli VIN qaytardi. Crop saqlanadi. So'ng:
          * SESSIYA FAOL bo'lsa -> VIN ni sessiyaga yozamiz (BIRINCHISI olinadi).
            DB yozuvi finalize da (VIN+RFID yoki timeout) bajariladi.
          * Sessiya bo'lmasa (qo'lda Start, PLC o'chiq) -> eski xatti-harakat:
            darhol DB ga yoziladi.
        Eski sessiya cropi (session_id mos kelmasa) -> e'tiborsiz qoldiriladi.
        """
        # OCRWorker avval yangi OCRResult obyektini beradi; eski 6-argument
        # callback ham test/tooling uchun saqlanadi.
        if hasattr(result, "vin") and hasattr(result, "session_id"):
            vin = result.vin
            confidence = result.confidence
            crop = result.crop
            raw_vin = result.raw_vin
            model = result.model
            session_id = result.session_id
        else:
            vin = result

        session = None
        if session_id is not None:
            self._bump_capture_counter("ocr_jobs_completed", session_id=session_id)
            session = self._validate_result_session(session_id, kind="ocr")
        with self._session_lock:
            if session_id == self._ocr_inflight_session:
                self._ocr_inflight_session = None
            self._ocr_inflight_events.get(session_id, self._ocr_inflight_done).set()
        if session_id is not None and session is None:
            return
        ts = datetime.now()
        fname = f"{vin}_{ts.strftime('%Y%m%d_%H%M%S')}.jpg"
        fpath = Path(CROPS_DIR) / fname
        rel_path = None
        try:
            if crop is not None and cv2.imwrite(str(fpath), crop):
                rel_path = f"crops/{fname}"
                self._maybe_cleanup_crops()   # P16: disk to'lishining oldini olish
        except Exception as exc:
            log.error(f"Crop saqlanmadi: {exc}")

        # v1.2.1 SHADOW OCR — additive telemetry (engraved engine evidence + active-learning
        # collector), flag-guarded and FULLY ISOLATED. It never alters the session's VIN/RFID
        # binding, capture ownership, or any session-safety behavior (SHADOW is evidence-only).
        try:
            from .ai.ocr_shadow_hook import record_legacy_shadow_final
            record_legacy_shadow_final(
                session_id=session_id,
                legacy_vin=vin,
                legacy_conf=confidence,
            )
        except Exception:
            pass

        if session is not None:
            with self._session_lock:
                if session.vin is not None:
                    log.info(f"[VIN] '{vin}' sessiya #{session_id} da allaqachon bor.")
                    return
                session.vin = vin
                session.vin_confidence = float(confidence or 0.0)
                session.vin_rel_path = rel_path
                session.vin_raw = raw_vin
                session.vin_model = model
                session.vin_at = time.time()
            log.info(f"[VIN] DETECTED (sessiya #{session_id}): VIN={vin} model={model} "
                     f"score={confidence:.2f} xom='{raw_vin}'")
            self._maybe_complete_session(session_id)
            return

        # --- Sessiya yo'q (qo'lda rejim): eski darhol-yozish yo'li ---
        # P4 FIX: legacy yozuv FAQAT haqiqiy qo'lda rejimda (session_id is None).
        # session_id berilgan bo'lsa-yu sessiya endi faol bo'lmasa — kech natija, rad.
        self._legacy_write_record(ts, vin, confidence, rel_path, raw_vin, model)

    def _legacy_write_record(self, ts, vin, confidence, rel_path, raw_vin, model) -> None:
        """PLC sessiyasidan TASHQARI (qo'lda Start) VIN uchun darhol DB yozuvi."""
        from .config import RFID, PLC
        ts_iso = ts.strftime("%Y-%m-%d %H:%M:%S")
        rfid_epc = None
        rfid_raw = None
        if self.rfid_service is not None and RFID.enabled:
            with self._rfid_lock:
                rfid = self._current_rfid
            if rfid is None:
                rfid_epc = "NO_READ"
            else:
                rfid_epc = rfid.get("number") or "NO_READ"
                rfid_raw = (rfid.get("raw") or "").strip() or None
        try:
            rec_id = db.insert_record(ts_iso, vin, confidence, rel_path,
                                      session_id=f"manual-{_new_session_id()}",
                                      raw_vin=raw_vin, model=model, status="OK",
                                      rfid_epc=rfid_epc, rfid_raw=rfid_raw)
            self.stats["vins"] += 1
            log.info(f"DB ga yozildi #{rec_id}: VIN={vin} model={model} "
                     f"RFID={rfid_epc} xom={raw_vin} score={confidence:.2f}")
        except Exception as exc:
            log.error(f"DB yozuv xatosi: {exc}")

        if PLC.auto_off_on_vin and self.on_vin_done is not None:
            if PLC.off_delay_sec > 0:
                time.sleep(PLC.off_delay_sec)
            try:
                self.on_vin_done()
            except Exception as exc:
                log.error(f"on_vin_done (PLC auto-off) xatosi: {exc}")

    # ===============================================================
    # P16: Crop retention — eng eski rasmlarni o'chirib disk to'lishini oldini olish
    # ===============================================================
    _crop_save_counter = 0

    def _maybe_cleanup_crops(self) -> None:
        """
        Har ~100 saqlashda bir marta CROPS_DIR ni tekshiradi. Fayllar soni
        CROP_RETENTION_MAX dan oshsa, eng eski (mtime) fayllarni o'chiradi.
        Ish disk I/O — kamdan-kam va yengil. Xato bo'lsa jim o'tadi (kritik emas).
        """
        from .config import CROP_RETENTION_MAX
        self._crop_save_counter += 1
        if CROP_RETENTION_MAX <= 0 or (self._crop_save_counter % 100) != 1:
            return
        try:
            files = [p for p in Path(CROPS_DIR).glob("*.jpg") if p.is_file()]
            if len(files) <= CROP_RETENTION_MAX:
                return
            files.sort(key=lambda p: p.stat().st_mtime)   # eng eski boshda
            remove = len(files) - CROP_RETENTION_MAX
            for p in files[:remove]:
                try:
                    p.unlink()
                except Exception:
                    pass
            log.info(f"[CROP] retention: {remove} eski crop o'chirildi "
                     f"(chegara={CROP_RETENTION_MAX}).")
        except Exception as exc:
            log.warning(f"[CROP] retention tozalash xatosi: {exc}")

    # ===============================================================
    # MJPEG stream uchun: oxirgi JPEG kadr
    # ===============================================================
    def get_jpeg(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._live_jpeg

    # ===============================================================
    # Holat (UI status uchun)
    # ===============================================================
    def _compute_camera_health(self) -> dict:
        with self._camera_lock:
            state = self._camera_state
            connected = self._camera_connected
            thread_alive = self._camera_thread_alive
            last_frame = self._last_frame_received_at
            last_decode = self._last_successful_decode_at
            last_detection = self._last_detection_at
            timeouts = self._consecutive_timeouts
            errors = self._consecutive_errors
            reconnects = self._camera_reconnect_attempts
            last_error = self._camera_last_error
        now = time.time()
        frame_age = now - last_frame if last_frame else None
        effective = state
        if (connected and frame_age is not None
                and frame_age > float(CAMERA.stale_after_sec)
                and state not in (CameraState.RECONNECTING, CameraState.FAILED,
                                  CameraState.STOPPING, CameraState.DISCONNECTED)):
            effective = CameraState.STALE
        return {
            "state": effective.value,
            "thread_alive": thread_alive,
            "last_frame_age_ms": None if frame_age is None else round(frame_age * 1000.0, 1),
            "last_decode_age_ms": (None if not last_decode
                                   else round((now - last_decode) * 1000.0, 1)),
            "last_detection_age_ms": (None if not last_detection
                                      else round((now - last_detection) * 1000.0, 1)),
            "consecutive_timeouts": timeouts,
            "consecutive_errors": errors,
            "reconnect_attempts": reconnects,
            "last_error": last_error,
        }

    def status(self) -> dict:
        st = {
            "camera_connected": self._camera_connected,
            "processing": self._processing,
            "yolo_ready": self.detector.ready,
            "stats": self.stats,
            "last_frame_ts": self._last_frame_ts,
            "ocr": self.ocr.get_stats(),     # OCR monitoring (navbat, ms, accepted...)
            "camera": self._compute_camera_health(),
            "stream": {                      # PF3/PF5: decoupled capture/inference metrikasi
                "capture_alive": self._capture_alive,
                "frame_id": self._latest_frame_id,
                "dropped_frames": self._dropped_frames,
                "decode_format": self._last_decode_format,
                "decode_fail_total": self._decode_fail_total,
                "decode_fail_streak": self._decode_fail_streak,
            },
        }
        if self.rfid_service is not None:
            try:
                st["rfid"] = self.rfid_service.status()
            except Exception:
                pass

        # --- Operator health: bir qarashda NIMA ishlamayotganini ajratish ---
        # "kamera ishlamayapti" / "YOLO topmayapti" / "OCR ishlamayapti" /
        # "OCR rad qilyapti" / "navbat kechikyapti" — bular boshqa-boshqa
        # nosozliklar va boshqa-boshqa harakat talab qiladi.
        now_wall = time.time()
        ocr_stats = st.get("ocr") or {}
        with self._session_lock:
            open_result_sessions = [
                s.session_id for s in self._sessions.values()
                if s.state not in _TERMINAL_SESSION_STATES
            ]
            last_finalized = next(
                (s for s in reversed(list(self._sessions.values()))
                 if s.finalized_at is not None), None)
            st["health"] = {
                "camera_connected": self._camera_connected,
                "camera_payload_flowing": bool(
                    self._last_frame_ts and (now_wall - self._last_frame_ts) < 5.0),
                "last_frame_age_ms": (round((now_wall - self._last_frame_ts) * 1000.0)
                                      if self._last_frame_ts else None),
                "yolo_ready": self.detector.ready,
                "last_detection_age_ms": (
                    round((now_wall - self._last_detection_at) * 1000.0)
                    if getattr(self, "_last_detection_at", None) else None),
                "ocr_ready": bool(ocr_stats.get("engine_ready", ocr_stats.get("ready", True))),
                "ocr_queue_depth": ocr_stats.get("queue_depth", ocr_stats.get("queued", 0)),
                "ocr_last_success_at": ocr_stats.get("last_success_at"),
                "active_capture_session": self._capture_owner_id,
                "open_result_sessions": len(open_result_sessions),
                "trigger_queue_depth": len(self._pending_triggers),
                "dropped_trigger_count": self._dropped_trigger_count,
                "suppressed_trigger_count": self._suppressed_trigger_count,
                "last_failure_stage": (last_finalized.failure_stage
                                       if last_finalized is not None else None),
            }
        with self._session_lock:
            active_id = self._active_session_id
            display_id = active_id or self._last_session_id
            session = self._sessions.get(display_id) if display_id else None
            if session is not None:
                remaining = max(0.0, session.deadline - time.monotonic()) if active_id else 0.0
                st["session"] = {
                    "active": active_id is not None,
                    "id": session.trigger_sequence,
                    "remaining_sec": round(remaining, 1),
                    "vin_done": session.vin is not None,
                    "rfid_done": session.rfid_epc not in (None, "", "NO_TAG", "NO_READ"),
                    "session_id": session.session_id,
                    "state": session.state.value,
                    "failure_reason": session.failure_reason,
                    "d2222_at": session.d2222_at,
                    "d2223_at": session.d2223_at,
                    "grace_remaining_sec": (
                        round(max(0.0, session.grace_deadline - time.monotonic()), 2)
                        if session.grace_deadline is not None
                        and session.state == SessionState.GRACE_WAIT else None
                    ),
                    "ocr_grace_active": self._ocr_grace_active,
                    "ocr_inflight": self._ocr_inflight_session == session.session_id,
                    "frames_seen": self._session_frames_seen,
                    "max_yolo_conf": round(self._session_max_yolo_conf, 3),
                    "best_crop_quality": round(self._session_best_crop_quality, 3),
                    "ocr_triggered": self._session_ocr_triggered,
                }
            else:
                st["session"] = {
                    "active": False, "id": 0, "remaining_sec": 0.0,
                    "vin_done": False, "rfid_done": False,
                    "session_id": None, "state": None, "failure_reason": None,
                    "d2222_at": None, "d2223_at": None,
                    "grace_remaining_sec": None,
                }
            st["session"]["dropped_triggers"] = self._dropped_trigger_count
            st["session"]["pending_triggers"] = len(self._pending_triggers)
            st["session"]["dropped_trigger_count"] = self._dropped_trigger_count
            st["session"]["finalizing_session_count"] = sum(
                s.state in (SessionState.EXIT_SEEN, SessionState.GRACE_WAIT)
                for s in self._sessions.values()
            )
            last_done = next(
                (self._sessions[sid] for sid in reversed(self._session_order)
                 if sid in self._sessions
                 and self._sessions[sid].state in _TERMINAL_SESSION_STATES),
                None,
            )
            st["session"]["last_completed"] = (
                {"session_id": last_done.session_id, "state": last_done.state.value,
                 "vin": last_done.vin, "rfid_epc": last_done.rfid_epc,
                 "failure_reason": last_done.failure_reason}
                if last_done is not None else None
            )
            st["metrics"] = dict(self._metrics)
        return st


# Global yagona pipeline nusxasi (server import qiladi).
pipeline = Pipeline()
