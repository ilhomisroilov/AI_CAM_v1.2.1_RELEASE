"""
============================================================
ocr_worker.py  —  OCR orkestratsiya (process-izolyatsiyalangan + consensus)
============================================================
Maqsad: YOLO bergan croplardan VIN ni YUQORI ANIQLIK va BARQARORLIK bilan o'qish.

Arxitektura (yangilangan — Section 1..6):
  * OCR inference ALOHIDA PROCESS(lar)da (ocr_process.OCRProcessPool). PaddleOCR
    native SIGSEGV/MKLDNN crashi ASOSIY ILOVANI (run.py) O'LDIRMAYDI. Python
    thread ICHIDA parallel PaddleOCR YO'Q (ThreadPoolExecutor olib tashlandi).
  * Har crop bir nechta NOMLI preprocessing variantiga bo'linadi (ocr_variants) —
    filtrlar engine ichига hardcode QILINMAYDI, ular tashqi OCR tasklari.
  * PRIMARY rejim rec-only (paddle_det=false); det+rec faqat fallback.
  * Natijalar POSITION-LEVEL weighted consensus (vin_fusion.fuse) bilan
    birlashtiriladi va qat'iy GLOBAL ACCEPT GATE + POSITION-5 HARD GATE dan
    o'tkaziladi. `fully_compliant` O'ZI qabul qilmaydi.
  * Har qabul qilingan VIN uchun AUDIT log: consensus, xavfli pozitsiyalar,
    marginlar, variant support, pos5 audit.

Tashqi interfeys O'ZGARMAGAN: OCRWorker, submit/submit_frames/start/stop/shutdown/
preload/get_stats/set_enabled/set_min_confidence, on_result(vin, score, crop,
raw_vin, model, session_id), on_fail(session_id, raw).
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

from ..config import (OCR, VIN, POS5_VERIFIER, VIN_SLOT_RECOGNIZER,
                      CROPS_DIR, CROP_RETENTION_MAX,
                      PADDLE_DET_DIR, PADDLE_REC_DIR, PADDLE_CLS_DIR)
from ..logger import log
from . import vin_rules
from .crop_quality import quality_score
from .ocr_process import (
    OCRProcessPool,
    OK as _OK,
    EMPTY as _EMPTY,
    ERROR as _ERROR,
    ENGINE_UNAVAILABLE as _ENGINE_UNAVAILABLE,
    OCR_ENGINE_CRASH,
    OCR_ENGINE_TIMEOUT,
)
from .ocr_variants import build_variants, interleave_variants
from .vin_fusion import VariantRead, fuse

# on_result(validated_vin, score, crop_bgr, raw_vin, model, session_id)
ResultCallback = Callable[[str, float, np.ndarray, str, object, object], None]

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


@dataclass
class OCRJob:
    """Session/frame ownership contract used by Pipeline and process supervisor."""
    frames: list
    session_id: object = None
    frame_id: object = None
    capture_timestamp: float = 0.0
    model_hint: Optional[str] = None


@dataclass
class OCRResult:
    session_id: object
    frame_id: object
    vin: str
    confidence: float
    model: Optional[str]
    completed_at: float
    attempt_count: int
    raw_vin: str = ""
    crop: object = None
    worker_pid: Optional[int] = None


@dataclass
class OCRReadTelemetry:
    """Bitta OCR jobidagi variant natijalarining yo'qolmaydigan klassifikatsiyasi."""

    ok: int = 0
    empty: int = 0
    errors: int = 0
    crashes: int = 0
    timeouts: int = 0
    engine_unavailable: int = 0
    error_details: list = field(default_factory=list)
    raw_payloads: list = field(default_factory=list)
    boxes: list = field(default_factory=list)

    def merge(self, other: "OCRReadTelemetry") -> "OCRReadTelemetry":
        for name in ("ok", "empty", "errors", "crashes", "timeouts",
                     "engine_unavailable"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        room = max(0, 8 - len(self.error_details))
        if room:
            self.error_details.extend(other.error_details[:room])
        self.raw_payloads.extend(other.raw_payloads)
        self.boxes.extend(other.boxes)
        return self

    @property
    def circuit_open(self) -> bool:
        return self.engine_unavailable > 0

    @property
    def failed(self) -> int:
        return self.errors + self.crashes + self.timeouts + self.engine_unavailable

    def summary(self) -> str:
        return (f"ok={self.ok} empty={self.empty} error={self.errors} "
                f"crash={self.crashes} timeout={self.timeouts} "
                f"engine_unavailable={self.engine_unavailable}")


def normalize_vin(text: str) -> str:
    """OCR matnini tozalaydi: bo'shliq/belgilarni olib tashlaydi, katta harf, A-Z0-9."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def verify_vin(text: str) -> Tuple[bool, str]:
    """Standart VIN tekshiruvi (17 belgi, I/O/Q yo'q). Qaytadi: (ok, cleaned)."""
    cleaned = normalize_vin(text)
    return bool(_VIN_RE.match(cleaned)), cleaned


def is_valid_vin(vin: str) -> bool:
    return bool(_VIN_RE.match(normalize_vin(vin)))


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


class OCRWorker:
    """OCR ni fon threadida navbat orqali orkestratsiya qiladi (process pool + consensus)."""

    def __init__(self, on_result: ResultCallback, on_fail=None) -> None:
        self.on_result = on_result
        self.on_fail = on_fail
        self._queue: "queue.Queue" = queue.Queue(maxsize=OCR.queue_maxsize)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._engine = None               # in-process compatibility/test engine
        self._frame_seq = 0

        # Process-izolyatsiyalangan OCR pool (lazy)
        self._pool: Optional[OCRProcessPool] = None
        self._pool_lock = threading.Lock()

        # Dinamik boshqaruv
        self._enabled = True
        self._min_conf = OCR.min_confidence
        self._last_run = 0.0

        # Anti-duplicate (session-aware)
        self._last_vin: Optional[str] = None
        self._last_vin_ts: float = 0.0
        self._last_vin_session: object = None
        self._dup_lock = threading.Lock()

        # Optional pos5 verifier (lazy)
        self._pos5_verifier = None
        self._pos5_verifier_tried = False

        # VIN-specific fixed-slot recognizer (lazy, PaddleOCR assistant/fallback)
        self._vin_slot_recognizer = None
        self._vin_slot_tried = False

        # Crop saqlash retention hisoblagichi (o'qilmagan croplar ham saqlanadi)
        self._crop_save_counter = 0

        # Statistika
        self._stats_lock = threading.Lock()
        self._processed = 0
        self._accepted = 0
        self._rejected = 0
        self._ambiguous = 0
        self._dropped = 0
        self._crashes = 0
        self._timeouts = 0
        self._engine_errors = 0
        self._empty_results = 0
        self._engine_unavailable_tasks = 0
        self._engine_status = "not_ready"
        self._last_engine_error = ""
        self._last_ms = 0.0
        self._ms_hist: deque = deque(maxlen=30)
        self._last_vin_read = ""
        self._last_conf = 0.0
        self._early_exits = 0
        self._variant_tasks = 0
        self._evidence_groups = {}

    # ===============================================================
    # Pool config
    # ===============================================================
    @staticmethod
    def _pool_cfg() -> dict:
        return {
            "lang": OCR.lang,
            "use_gpu": bool(OCR.use_gpu),
            "enable_mkldnn": bool(getattr(OCR, "enable_mkldnn", False)),
            "cpu_threads": int(getattr(OCR, "cpu_threads", 1) or 1),
            "use_angle_cls": bool(OCR.use_angle_cls),
            "drop_score": float(OCR.drop_score),
            # These are mandatory release artifacts. Never omit them: omission
            # would make PaddleOCR consult/download a user-profile cache.
            "det_model_dir": str(PADDLE_DET_DIR),
            "rec_model_dir": str(PADDLE_REC_DIR),
            "cls_model_dir": str(PADDLE_CLS_DIR),
        }

    def _ensure_pool(self) -> bool:
        with self._pool_lock:
            if self._pool is not None:
                return True
            try:
                n = max(1, min(int(getattr(OCR, "parallel_engines", 1) or 1), 4))
                self._pool = OCRProcessPool(
                    cfg=self._pool_cfg(), n_workers=n,
                    task_timeout_sec=float(getattr(OCR, "worker_task_timeout_sec", 12.0)),
                    start_timeout_sec=float(getattr(OCR, "worker_start_timeout_sec", 60.0)),
                    max_restarts=int(getattr(OCR, "worker_max_restarts", 50)),
                    logger=log,
                )
                self._pool.start()
                return True
            except Exception as exc:
                log.error(f"OCR process pool yaratilmadi: {exc}")
                self._pool = None
                return False

    # ===============================================================
    # Hayotiy sikl
    # ===============================================================
    def preload(self) -> bool:
        if not self._ensure_pool():
            return False
        try:
            ok = bool(self._pool.preload())
            self._engine_status = getattr(
                self._pool, "engine_status", "ready" if ok else "engine_unavailable")
            if not ok:
                meta = dict(getattr(self._pool, "engine_unavailable_meta", {}) or {})
                self._last_engine_error = str(meta.get("err") or meta.get("reason") or
                                              "PaddleOCR preload failed")[:1000]
                log.error(f"OCR preload ENGINE_UNAVAILABLE: {self._last_engine_error}")
            self._ensure_vin_slot_recognizer()
            return ok
        except Exception as exc:
            self._engine_status = "engine_unavailable"
            self._last_engine_error = f"{type(exc).__name__}: {exc}"[:1000]
            log.error(f"OCR pool preload ENGINE_UNAVAILABLE: {self._last_engine_error}")
            return False

    def start(self) -> None:
        self._enabled = True
        self._ensure_pool()
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="ocr-worker", daemon=True)
        self._thread.start()
        log.info("OCR worker ishga tushdi (process-izolyatsiyalangan pool + consensus).")

    def stop(self) -> None:
        self._enabled = False
        # Session ownership bilan navbatga kirgan jobni tashlamaymiz. Plastinka
        # kadrdan chiqishi inference'ni bekor qilsa deadline'da sun'iy NO_READ
        # paydo bo'ladi. Yangi submitlar `_enabled` orqali to'xtaydi.
        log.info("OCR worker pauza qilindi (in-flight/navbatdagi session job tugatiladi).")

    def shutdown(self) -> None:
        self._running = False
        self._enabled = False
        self._drain_queue()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        with self._pool_lock:
            if self._pool is not None:
                try:
                    self._pool.shutdown()
                except Exception:
                    pass
                self._pool = None
        log.info("OCR worker to'liq to'xtatildi (shutdown).")

    def _drain_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    # ===============================================================
    # Dinamik nazorat
    # ===============================================================
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        log.info("OCR yoqildi." if enabled else "OCR o'chirildi.")

    def set_min_confidence(self, value: float) -> None:
        self._min_conf = _clamp01(float(value))
        log.info(f"OCR minimal ishonch chegarasi: {self._min_conf:.2f}")

    def get_stats(self) -> dict:
        with self._stats_lock:
            avg_ms = (sum(self._ms_hist) / len(self._ms_hist)) if self._ms_hist else 0.0
            restarts = 0
            try:
                restarts = self._pool._total_restarts if self._pool is not None else 0
            except Exception:
                restarts = 0
            return {
                "enabled": self._enabled,
                "running": self._running,
                "queue": self._queue.qsize(),
                "queue_max": OCR.queue_maxsize,
                "processed": self._processed,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "ambiguous": self._ambiguous,
                "dropped": self._dropped,
                "engine_crashes": self._crashes,
                "engine_timeouts": self._timeouts,
                "engine_errors": self._engine_errors,
                "empty_results": self._empty_results,
                "engine_unavailable_tasks": self._engine_unavailable_tasks,
                "engine_status": self._engine_status,
                "last_engine_error": self._last_engine_error,
                "worker_restarts": restarts,
                "avg_ms": round(avg_ms, 1),
                "last_ms": round(self._last_ms, 1),
                "min_conf": round(self._min_conf, 2),
                "last_vin": self._last_vin_read,
                "last_conf": round(self._last_conf, 2),
                "early_exits": self._early_exits,
                "variant_tasks": self._variant_tasks,
                "vin_slot_ready": bool(self._vin_slot_recognizer is not None
                                       and getattr(self._vin_slot_recognizer, "ready", False)),
            }

    # ===============================================================
    # Navbat (drop-oldest — FPS himoyasi)
    # ===============================================================
    def submit(self, crop_bgr: np.ndarray, model: Optional[str] = None,
               session_id: Optional[int] = None) -> None:
        self.submit_frames([crop_bgr], model, session_id)

    def submit_frames(self, crops: list, model: Optional[str] = None,
                      session_id: Optional[int] = None, frame_id=None,
                      capture_timestamp: Optional[float] = None) -> None:
        if not self._enabled or not crops:
            return
        if frame_id is None:
            self._frame_seq += 1
            frame_id = self._frame_seq
        job = OCRJob(frames=list(crops), session_id=session_id, frame_id=frame_id,
                     capture_timestamp=(float(capture_timestamp) if capture_timestamp is not None
                                        else time.time()), model_hint=model)
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(job)
                with self._stats_lock:
                    self._dropped += 1
            except queue.Empty:
                pass

    def _loop(self) -> None:
        if not self._ensure_pool():
            self._running = False
            return
        while self._running:
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                continue
            if OCR.min_interval_sec > 0:
                dt = time.monotonic() - self._last_run
                wait = OCR.min_interval_sec - dt
                if wait > 0:
                    end = time.monotonic() + wait
                    while self._running and time.monotonic() < end:
                        time.sleep(min(0.1, end - time.monotonic()))
                    if not self._running:
                        break
            self._last_run = time.monotonic()
            try:
                self._process_job(job)
            except Exception as exc:
                log.error(f"OCR _process_job xatosi: {exc}")
                self._emit_fail(getattr(job, "session_id", None), "")

    # ===============================================================
    # Variant -> OCR (process pool) -> VariantRead
    # ===============================================================
    def _build_tasks(self, crops: list):
        """Har crop uchun variantlar + interleave. Qaytadi (tasks, crop_quality)."""
        per_crop: List[list] = []
        crop_quality: List[float] = []
        self._evidence_groups = self._group_near_duplicate_crops(crops)
        rotations = tuple(getattr(OCR, "variant_rotations", (0.0, -3.0, 3.0, -6.0, 6.0)) or (0.0,))
        if not OCR.retry_enabled:
            rotations = (0.0,)
        names = tuple(getattr(OCR, "variants_enabled", ()) or ())
        kw = dict(
            rotations=rotations,
            deskew=bool(getattr(OCR, "variant_deskew", True)),
            upscale_height=int(getattr(OCR, "upscale_height", 96) or 96),
            clahe_clip=float(getattr(OCR, "clahe_clip", 3.0)),
            clahe_grid=int(getattr(OCR, "clahe_grid", 8)),
        )
        if names:
            kw["variant_names"] = names
        for c in crops:
            try:
                q, _m = quality_score(c, 0.8)
            except Exception:
                q = 0.5
            crop_quality.append(_clamp01(q))
            per_crop.append(build_variants(c, **kw))
        tasks = interleave_variants(per_crop, int(getattr(OCR, "max_ocr_attempts", 16)))
        return tasks, crop_quality

    @staticmethod
    def _crop_dhash(crop: np.ndarray) -> np.ndarray:
        """64-bit perceptual difference hash (frame byte hash emas)."""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
        return (small[:, 1:] > small[:, :-1]).reshape(-1)

    def _group_near_duplicate_crops(self, crops: list) -> dict:
        """Takror/near-identical camera framelar bitta mustaqil dalil sanaladi."""
        max_dist = int(getattr(VIN, "independent_crop_hash_distance", 5) or 0)
        reps: List[np.ndarray] = []
        groups = {}
        for idx, crop in enumerate(crops):
            try:
                h = self._crop_dhash(crop)
            except Exception:
                groups[idx] = len(reps)
                reps.append(np.zeros(64, dtype=bool))
                continue
            assigned = None
            for group_id, rep in enumerate(reps):
                if int(np.count_nonzero(h != rep)) <= max_dist:
                    assigned = group_id
                    break
            if assigned is None:
                assigned = len(reps)
                reps.append(h)
            groups[idx] = assigned
        return groups

    def _read_pool(self, tasks, crop_quality, det: bool,
                   deadline: Optional[float] = None
                   ) -> Tuple[List[VariantRead], OCRReadTelemetry]:
        """Variantlarni OCR qiladi; EMPTY/ERROR/CRASH/TIMEOUT telemetry yo'qolmaydi."""
        if not tasks:
            return [], OCRReadTelemetry()
        pool_tasks = [(v.image, det) for (_ci, v) in tasks]
        results = self._pool.run_batch(pool_tasks, batch_deadline=deadline)
        reads: List[VariantRead] = []
        telemetry = OCRReadTelemetry()
        for task_index, ((ci, v), res) in enumerate(zip(tasks, results)):
            if res is None:
                telemetry.errors += 1
                telemetry.error_details.append({
                    "status": _ERROR, "reason": "missing_result",
                    "task_index": task_index, "variant": v.variant_name,
                })
                continue
            status = res.status
            meta = dict(res.meta or {})
            if status == _EMPTY:
                telemetry.empty += 1
                continue
            if status == OCR_ENGINE_CRASH:
                telemetry.crashes += 1
                continue
            if status == OCR_ENGINE_TIMEOUT:
                telemetry.timeouts += 1
                telemetry.error_details.append({
                    "status": status, "task_index": task_index,
                    "variant": v.variant_name, **meta,
                })
                continue
            if status == _ENGINE_UNAVAILABLE:
                telemetry.engine_unavailable += 1
                telemetry.error_details.append({
                    "status": status, "task_index": task_index,
                    "variant": v.variant_name, **meta,
                })
                continue
            if status != _OK:
                telemetry.errors += 1
                telemetry.error_details.append({
                    "status": status or _ERROR, "task_index": task_index,
                    "variant": v.variant_name, **meta,
                })
                continue
            telemetry.ok += 1
            if meta.get("raw_payload") is not None:
                telemetry.raw_payloads.append({
                    "task_index": task_index,
                    "crop_index": ci,
                    "variant": v.variant_name,
                    "payload": meta.get("raw_payload"),
                })
            if meta.get("boxes"):
                telemetry.boxes.extend(meta.get("boxes") or [])
            if not res.fragments:
                continue
            text = "".join(t for (t, _c) in res.fragments)
            confs = [float(c) for (_t, c) in res.fragments if c is not None]
            ocr_conf = float(np.mean(confs)) if confs else 0.0
            nr = normalize_vin(text)
            reads.append(VariantRead(
                crop_index=ci, variant_name=v.variant_name, ocr_conf=ocr_conf,
                raw_text=text, normalized_raw=nr,
                crop_quality=crop_quality[ci] if ci < len(crop_quality) else 0.5,
                variant_weight=float(v.weight),
                evidence_group=self._evidence_groups.get(ci, ci)))
        if telemetry.error_details:
            first = telemetry.error_details[0]
            err = str(first.get("err") or first.get("reason") or first.get("status"))[:1000]
            self._last_engine_error = err
            if telemetry.engine_unavailable:
                self._engine_status = "engine_unavailable"
                log.error(f"[OCR ENGINE] ENGINE_UNAVAILABLE: {err}; {telemetry.summary()}")
            elif telemetry.errors:
                log.error(f"[OCR ENGINE] Python ERROR: {err}; {telemetry.summary()}")
            elif telemetry.timeouts:
                log.error(f"[OCR ENGINE] TIMEOUT: {err}; {telemetry.summary()}")
        elif telemetry.ok or telemetry.empty:
            self._engine_status = "ready"
        return reads, telemetry

    def _fuse_params(self) -> dict:
        return dict(
            confusion_prior=float(getattr(VIN, "confusion_prior", 0.6)),
            struct_penalty=float(getattr(VIN, "struct_penalty", 0.5)),
            enforce_fixed_positions=bool(getattr(VIN, "enforce_fixed_positions", True)),
            accept_final_score=max(float(getattr(VIN, "accept_final_score", 0.92)),
                                   float(getattr(VIN, "min_final_score", 0.0))),
            accept_min_crops=int(getattr(VIN, "accept_min_crops", 2)),
            accept_multi_variant_min=int(getattr(VIN, "accept_multi_variant_min", 4)),
            accept_risky_margin=float(getattr(VIN, "accept_risky_margin", 0.25)),
            accept_raw_support_ratio=float(getattr(VIN, "accept_raw_support_ratio", 0.60)),
            accept_single_read=bool(getattr(VIN, "accept_single_read", True)),
            accept_single_min_score=float(getattr(VIN, "accept_single_min_score",
                                                   getattr(VIN, "accept_final_score", 0.92))),
            require_known_model=bool(getattr(VIN, "require_known_model", True)),
            pos5_min_crops=int(getattr(VIN, "pos5_min_crops", 2)),
            pos5_min_variants=int(getattr(VIN, "pos5_min_variants", 3)),
            pos5_min_margin=float(getattr(VIN, "pos5_min_margin", 0.20)),
            pos5_reject_structure_only=bool(getattr(VIN, "pos5_reject_structure_only", True)),
            pos5_structure_rescue_enabled=bool(getattr(VIN, "pos5_structure_rescue_enabled", True)),
            pos5_structure_rescue_raw_chars=str(getattr(VIN, "pos5_structure_rescue_raw_chars", "38")),
            pos5_structure_rescue_min_score=float(getattr(VIN, "pos5_structure_rescue_min_score", 0.90)),
            pos5_structure_rescue_min_raw_support=float(
                getattr(VIN, "pos5_structure_rescue_min_raw_support", 0.85)),
            accept_variable_margin=float(getattr(VIN, "accept_variable_margin", 0.08)),
            accept_serial_margin=float(getattr(VIN, "accept_serial_margin", 0.12)),
            exact_conflict_ratio=float(getattr(VIN, "exact_conflict_ratio", 0.55)),
            exact_rescue_min_conf=float(getattr(VIN, "exact_rescue_min_conf", 0.74)),
        )

    def _run_variant_cascade(self, tasks, crop_quality, det: bool,
                             deadline: Optional[float] = None):
        """raw+CLAHE first; only independent strict agreement may early-exit."""
        primary = [(ci, v) for ci, v in tasks
                   if "@" not in v.variant_name
                   and v.variant_name in ("raw_resized", "clahe_unsharp")]
        primary_ids = {id(v) for _ci, v in primary}
        retry = [(ci, v) for ci, v in tasks if id(v) not in primary_ids]
        reads, telemetry = self._read_pool(
            primary, crop_quality, det=det, deadline=deadline)
        fr = fuse(reads, **self._fuse_params())
        evidence = fr.gate.get("evidence", {}) if fr.gate else {}
        safe_early = (fr.accepted and evidence.get("exact_groups", 0) >= 2
                      and not evidence.get("conflict", False))
        if safe_early:
            with self._stats_lock:
                self._early_exits += 1
                self._variant_tasks += len(primary)
            return fr, reads, telemetry, True
        # Fatal engine init va umumiy/per-task timeoutda qolgan variantlarni
        # boshlash foydasiz. Aynan shu breaker 16x re-init/22s regressiyasini yopadi.
        if telemetry.circuit_open or telemetry.timeouts:
            with self._stats_lock:
                self._variant_tasks += len(primary)
            return fr, reads, telemetry, False
        if retry:
            more, more_telemetry = self._read_pool(
                retry, crop_quality, det=det, deadline=deadline)
            reads.extend(more)
            telemetry.merge(more_telemetry)
            fr = fuse(reads, **self._fuse_params())
        with self._stats_lock:
            self._variant_tasks += len(primary) + len(retry)
        return fr, reads, telemetry, False

    def compute_result(self, job: OCRJob) -> Optional[OCRResult]:
        """
        Process-supervisor compatible pure computation entry. Tests may inject an
        in-process engine; production orchestration continues through isolated
        variant pool below.
        """
        if job is None or not job.frames or self._engine is None:
            return None
        reads: List[VariantRead] = []
        best_crop = job.frames[0]
        for crop_index, crop in enumerate(job.frames):
            try:
                fragments = self._engine.read(crop) or []
            except Exception:
                continue
            fragments = sorted(fragments, key=lambda x: min(p[0] for p in x[0])
                               if x and x[0] else 0)
            text = "".join(str(x[1]) for x in fragments if len(x) >= 3)
            confs = [float(x[2]) for x in fragments if len(x) >= 3]
            if not text:
                continue
            reads.append(VariantRead(
                crop_index=crop_index, evidence_group=crop_index,
                variant_name="raw_resized", ocr_conf=(sum(confs) / len(confs) if confs else 0.0),
                raw_text=text, crop_quality=1.0, variant_weight=1.0))
        fr = fuse(reads, **self._fuse_params())
        if not fr.accepted:
            return None
        return OCRResult(
            session_id=job.session_id, frame_id=job.frame_id, vin=fr.validated_vin,
            confidence=fr.final_score, model=fr.model, completed_at=time.time(),
            attempt_count=max(1, len(reads)), raw_vin=self._plurality_raw(reads),
            crop=best_crop)

    def _deliver_result(self, result: OCRResult) -> None:
        """New object callback first; legacy six-argument callback as fallback."""
        try:
            self.on_result(result)
            return
        except TypeError:
            pass
        self.on_result(result.vin, result.confidence, result.crop, result.raw_vin,
                       result.model, result.session_id)

    def _process_job(self, crops: list, model=None, session_id=None) -> None:
        """
        Bitta plastinka hodisasi: variantlar -> process pool OCR -> position-level
        consensus + gates. Xom natija AUDIT uchun log qilinadi.
        """
        job_meta = crops if isinstance(crops, OCRJob) else None
        if job_meta is not None:
            # Deterministic contract/test or old real supervisor path.
            if self._engine is not None:
                result = self.compute_result(job_meta)
                if result is None:
                    self._emit_fail(job_meta.session_id, "")
                    return
                if self._is_duplicate(result.session_id, result.vin):
                    return
                self._deliver_result(result)
                return
            crops = job_meta.frames
            model = job_meta.model_hint
            session_id = job_meta.session_id
        t0 = time.perf_counter()
        job_started_monotonic = time.monotonic()
        job_timeout_sec = max(
            0.1,
            float(getattr(OCR, "worker_job_timeout_sec",
                          getattr(OCR, "worker_task_timeout_sec", 12.0)) or 12.0),
        )
        if not self._ensure_pool():
            self._emit_fail(session_id, "")
            return
        # Startup preload chaqirilmagan cold-start yo'lida model yuklash uchun
        # alohida start budjeti saqlanadi. Preload fatal bo'lsa pool circuit allaqachon
        # ochiq bo'ladi va bu katta budjet 16x retryga sabab bo'lmaydi.
        if getattr(self._pool, "engine_status", "not_ready") == "not_ready":
            job_timeout_sec = max(
                job_timeout_sec,
                float(getattr(OCR, "worker_start_timeout_sec", 60.0) or 60.0),
            )
        job_deadline = job_started_monotonic + job_timeout_sec

        tasks, crop_quality = self._build_tasks(crops)
        # ENG YAXSHI cropni oldindan tanlaymiz — OCR muvaffaqiyatsiz bo'lsa ham
        # (crash/NO_READ/AMBIGUOUS) rasm vaqt+sessiya bo'yicha crops ga saqlanadi.
        best_crop = self._best_crop(crops, crop_quality)
        det_primary = bool(getattr(OCR, "paddle_det", False))
        fr, reads, telemetry, early_exit = self._run_variant_cascade(
            tasks, crop_quality, det=det_primary, deadline=job_deadline)

        # --- DET FALLBACK: rec-only kam natija bersa, raw_resized crop'da det+rec ---
        if (not early_exit and not telemetry.circuit_open and not telemetry.timeouts
                and time.monotonic() < job_deadline
                and not det_primary and bool(getattr(OCR, "det_fallback_enabled", True))
                and len({r.normalized_raw for r in reads if len(r.normalized_raw) == 17}) < 2):
            det_tasks = [(ci, v) for (ci, v) in tasks
                         if v.variant_name in ("raw_resized", "clahe_unsharp")]
            det_tasks = det_tasks[:max(1, len(crops))]
            if det_tasks:
                dreads, dtelemetry = self._read_pool(
                    det_tasks, crop_quality, det=True, deadline=job_deadline)
                reads.extend(dreads)
                telemetry.merge(dtelemetry)
                fr = fuse(reads, **self._fuse_params())
        slot_pred = self._vin_slot_predict(best_crop)

        dt_ms = (time.perf_counter() - t0) * 1000.0
        with self._stats_lock:
            self._processed += 1
            self._crashes += telemetry.crashes
            self._timeouts += telemetry.timeouts
            self._engine_errors += telemetry.errors
            self._empty_results += telemetry.empty
            self._engine_unavailable_tasks += telemetry.engine_unavailable
            self._last_ms = dt_ms
            self._ms_hist.append(dt_ms)
            if fr.validated_vin:
                self._last_vin_read = fr.validated_vin

        # Fatal runtime/init ERROR oddiy NO_READ emas: operatorga aniq sabab beriladi.
        if not reads and (telemetry.engine_unavailable > 0 or telemetry.errors > 0):
            paddle_status = ("PADDLE_ENGINE_UNAVAILABLE" if telemetry.engine_unavailable
                             else "PADDLE_ERROR")
            if self._try_accept_slot_fallback(slot_pred, reads, best_crop, session_id,
                                              paddle_status, dt_ms):
                return
            with self._stats_lock:
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "ENGINE_UNAVAILABLE")
            detail = self._last_engine_error or "unknown engine error"
            log.error(f"[OCR] {paddle_status}: {telemetry.summary()}, {dt_ms:.0f} ms; "
                      f"reason={detail}. Crop saqlandi: {saved} (session #{session_id}).")
            self._emit_fail(session_id, "")
            return

        if not reads and telemetry.timeouts > 0:
            if self._try_accept_slot_fallback(slot_pred, reads, best_crop, session_id,
                                              "PADDLE_TIMEOUT", dt_ms):
                return
            with self._stats_lock:
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "TIMEOUT")
            log.error(f"[OCR] TIMEOUT: {telemetry.summary()}, {dt_ms:.0f} ms. "
                      f"Crop saqlandi: {saved} (session #{session_id}).")
            self._emit_fail(session_id, "")
            return

        # --- Barcha tasklar native crash bo'lsa: worker qayta yaratilgan ---
        if not reads and telemetry.crashes > 0:
            if self._try_accept_slot_fallback(slot_pred, reads, best_crop, session_id,
                                               "PADDLE_CRASH", dt_ms):
                return
            with self._stats_lock:
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "CRASH")
            log.error(f"[OCR] Barcha {telemetry.crashes} variant taski OCR_ENGINE_CRASH — "
                      f"engine qayta yaratildi, natija yo'q. Crop saqlandi: {saved} "
                      f"(session #{session_id}).")
            self._emit_fail(session_id, "")
            return

        if fr.status == "NO_READ":
            if self._try_accept_slot_fallback(slot_pred, reads, best_crop, session_id,
                                              "NO_READ", dt_ms):
                return
            with self._stats_lock:
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "NO_READ")
            log.info(f"[OCR] NO_READ: {len(crops)} kadr, {len(reads)} o'qish, "
                     f"{telemetry.summary()}, {dt_ms:.0f} ms. Crop saqlandi: {saved} "
                     f"(session #{session_id}).")
            self._emit_fail(session_id, "")
            return

        if fr.status != "ACCEPT":
            if self._try_accept_slot_fallback(slot_pred, reads, best_crop, session_id,
                                              fr.status or "OCR_AMBIGUOUS", dt_ms):
                return
            # OCR_AMBIGUOUS — DB ga YOZILMAYDI, lekin crop vaqt+sessiya bilan saqlanadi.
            with self._stats_lock:
                self._ambiguous += 1
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "AMBIGUOUS",
                                           vin=fr.validated_vin or "")
            self._log_audit("OCR_AMBIGUOUS RAD ETILDI", fr, crops, reads,
                            telemetry, dt_ms, session_id)
            log.info(f"[OCR] AMBIGUOUS crop saqlandi: {saved} (session #{session_id}).")
            self._emit_fail(session_id, fr.validated_vin or "")
            return

        # --- Optional pos5 visual verifier (Section 8) ---
        if not self._pos5_verify(best_crop, fr):
            with self._stats_lock:
                self._ambiguous += 1
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "AMBIGUOUS_POS5",
                                           vin=fr.validated_vin or "")
            log.warning(f"[OCR] pos5 verifier past ishonch -> OCR_AMBIGUOUS "
                        f"(VIN='{fr.validated_vin}', crop saqlandi: {saved}, session #{session_id}).")
            self._emit_fail(session_id, fr.validated_vin or "")
            return

        vin = fr.validated_vin
        raw = self._plurality_raw(reads) or vin
        score = fr.final_score
        final_model = fr.model
        slot_action, score = self._slot_assist_paddle_accept(slot_pred, vin, score)
        if slot_action == "reject":
            with self._stats_lock:
                self._ambiguous += 1
                self._rejected += 1
            saved = self._save_unread_crop(best_crop, session_id, "AMBIGUOUS_SLOT_DISAGREE",
                                           vin=vin or "")
            log.warning(f"[VIN_SLOT] Paddle ACCEPT lekin slot model zid natija berdi -> "
                        f"OCR_AMBIGUOUS (crop saqlandi: {saved}, session #{session_id}).")
            self._emit_fail(session_id, vin or "")
            return
        with self._stats_lock:
            self._last_conf = score

        if self._is_duplicate(session_id, vin):
            log.info(f"DUPLICATE: '{vin}' shu sessiyada yaqinda o'qilgan — e'tiborsiz.")
            return

        with self._stats_lock:
            self._accepted += 1
        self._log_audit("VIN QABUL QILINDI", fr, crops, reads,
                        telemetry, dt_ms, session_id)
        try:
            self._deliver_result(OCRResult(
                session_id=session_id,
                frame_id=(job_meta.frame_id if job_meta is not None else None),
                vin=vin, confidence=score, model=final_model,
                completed_at=time.time(), attempt_count=len(reads),
                raw_vin=raw, crop=best_crop))
        except Exception as exc:
            log.error(f"on_result callback xatosi: {exc}")

    # ===============================================================
    # Audit + yordamchilar
    # ===============================================================
    @staticmethod
    def _slot_mode() -> str:
        mode = str(getattr(VIN_SLOT_RECOGNIZER, "mode", "assist") or "assist").strip().lower()
        return mode if mode in ("shadow", "assist", "enforce") else "assist"

    def _ensure_vin_slot_recognizer(self) -> bool:
        if not bool(getattr(VIN_SLOT_RECOGNIZER, "enabled", False)):
            return False
        if (self._vin_slot_recognizer is not None
                and getattr(self._vin_slot_recognizer, "ready", False)):
            return True
        if self._vin_slot_tried:
            return False
        try:
            self._vin_slot_tried = True
            from .vin_slot_recognizer import VinSlotRecognizer
            self._vin_slot_recognizer = VinSlotRecognizer.from_config(VIN_SLOT_RECOGNIZER)
            log.info(f"[VIN_SLOT] Model yuklandi: "
                     f"{getattr(VIN_SLOT_RECOGNIZER, 'model_path', '')} "
                     f"(device={self._vin_slot_recognizer.device}, mode={self._slot_mode()}).")
            return bool(getattr(self._vin_slot_recognizer, "ready", False))
        except Exception as exc:
            log.warning(f"[VIN_SLOT] Model yuklanmadi, PaddleOCR yo'li davom etadi: {exc}")
            self._vin_slot_recognizer = None
            return False

    def _vin_slot_predict(self, crop):
        if crop is None or getattr(crop, "size", 0) == 0:
            return None
        try:
            if not self._ensure_vin_slot_recognizer():
                return None
            pred = self._vin_slot_recognizer.predict(crop)
            usable, why = self._slot_usable(pred)
            log.info(f"[VIN_SLOT] pred={pred.vin} avg={pred.avg_confidence:.3f} "
                     f"min={pred.min_confidence:.3f} model={pred.model or '-'} "
                     f"valid={int(pred.valid_vin)} usable={int(usable)} "
                     f"reason={why} mode={self._slot_mode()}.")
            return pred
        except Exception as exc:
            log.warning(f"[VIN_SLOT] Model ishlamadi, PaddleOCR yo'li davom etadi: {exc}")
            return None

    @staticmethod
    def _slot_usable(pred) -> Tuple[bool, str]:
        if pred is None:
            return False, "no_prediction"
        if not bool(getattr(pred, "valid_vin", False)):
            return False, "invalid_vin"
        avg = float(getattr(pred, "avg_confidence", 0.0) or 0.0)
        minc = float(getattr(pred, "min_confidence", 0.0) or 0.0)
        min_avg = float(getattr(VIN_SLOT_RECOGNIZER, "min_avg_confidence", 0.80))
        min_char = float(getattr(VIN_SLOT_RECOGNIZER, "min_char_confidence", 0.35))
        if avg < min_avg:
            return False, f"avg_conf<{min_avg:.2f}"
        if minc < min_char:
            return False, f"min_char_conf<{min_char:.2f}"
        model = getattr(pred, "model", None)
        if bool(getattr(VIN, "require_known_model", True)) and not vin_rules.is_supported_model(model):
            return False, "unknown_model"
        if model and not vin_rules.validate(model, getattr(pred, "vin", ""))[0]:
            return False, "structure_mismatch"
        return True, "ok"

    def _slot_assist_paddle_accept(self, pred, paddle_vin: str, score: float) -> Tuple[str, float]:
        if pred is None:
            return "keep", score
        mode = self._slot_mode()
        usable, why = self._slot_usable(pred)
        if mode == "shadow":
            log.info(f"[VIN_SLOT] SHADOW: paddle={paddle_vin} slot={pred.vin} "
                     f"usable={int(usable)} reason={why}; production natija PaddleOCR.")
            return "keep", score
        if pred.vin == paddle_vin:
            bonus = float(getattr(VIN_SLOT_RECOGNIZER, "agree_bonus", 0.03) or 0.0)
            boosted = min(1.0, float(score) + max(0.0, bonus))
            log.info(f"[VIN_SLOT] AGREE: VIN={paddle_vin} score={score:.3f}->{boosted:.3f}.")
            return "keep", boosted
        if usable:
            if bool(getattr(VIN_SLOT_RECOGNIZER, "reject_on_disagree", False)) or mode == "enforce":
                log.warning(f"[VIN_SLOT] DISAGREE: paddle={paddle_vin} slot={pred.vin} "
                            f"avg={pred.avg_confidence:.3f} min={pred.min_confidence:.3f}; "
                            f"mode={mode} -> reject.")
                return "reject", score
            log.warning(f"[VIN_SLOT] DISAGREE: paddle={paddle_vin} slot={pred.vin} "
                        f"avg={pred.avg_confidence:.3f} min={pred.min_confidence:.3f}; "
                        f"PaddleOCR natijasi saqlandi.")
        else:
            log.info(f"[VIN_SLOT] Paddle ACCEPT, slot ignored: paddle={paddle_vin} "
                     f"slot={getattr(pred, 'vin', '-')} reason={why}.")
        return "keep", score

    @staticmethod
    def _slot_paddle_support(pred, reads: List[VariantRead]) -> Tuple[bool, str]:
        if not bool(getattr(VIN_SLOT_RECOGNIZER, "require_paddle_support", True)):
            return True, "support_not_required"
        needed = int(getattr(VIN_SLOT_RECOGNIZER, "min_paddle_exact_reads", 1) or 0)
        if needed <= 0:
            return True, "support_threshold_0"
        exact = [r for r in reads if getattr(r, "normalized_raw", "") == pred.vin]
        if len(exact) >= needed:
            crops = len({r.crop_index for r in exact})
            variants = ",".join(sorted({r.variant_name for r in exact})[:4])
            return True, f"paddle_exact_reads={len(exact)} crops={crops} variants={variants}"
        return False, f"paddle_exact_reads={len(exact)}<{needed}"

    def _try_accept_slot_fallback(self, pred, reads: List[VariantRead], crop,
                                  session_id, paddle_status: str, dt_ms: float) -> bool:
        if pred is None:
            return False
        mode = self._slot_mode()
        if mode == "shadow":
            log.info(f"[VIN_SLOT] SHADOW: Paddle {paddle_status}, slot={pred.vin}; "
                     f"production natija yozilmadi.")
            return False
        if mode not in ("assist", "enforce"):
            return False
        if not bool(getattr(VIN_SLOT_RECOGNIZER, "accept_on_paddle_fail", True)) and mode != "enforce":
            log.info(f"[VIN_SLOT] Paddle {paddle_status}, lekin fallback qabul o'chirilgan.")
            return False

        usable, why = self._slot_usable(pred)
        if not usable:
            log.info(f"[VIN_SLOT] Paddle {paddle_status}, slot fallback rad: "
                     f"slot={pred.vin} reason={why}.")
            return False
        supported, support_reason = self._slot_paddle_support(pred, reads)
        if not supported and mode != "enforce":
            log.info(f"[VIN_SLOT] Paddle {paddle_status}, slot fallback rad: "
                     f"slot={pred.vin} reason={support_reason}.")
            return False

        score = min(0.99, max(0.55, float(pred.avg_confidence)))
        final_model = pred.model or vin_rules.model_from_vin(pred.vin)

        if self._is_duplicate(session_id, pred.vin):
            log.info(f"DUPLICATE: '{pred.vin}' VIN_SLOT fallback orqali yaqinda o'qilgan.")
            return True

        with self._stats_lock:
            self._accepted += 1
            self._last_vin_read = pred.vin
            self._last_conf = score

        log.info(f"[VIN_SLOT] Paddle {paddle_status} -> SLOT ACCEPT: VIN={pred.vin} "
                 f"model={final_model} score={score:.3f} avg={pred.avg_confidence:.3f} "
                 f"min={pred.min_confidence:.3f} support={support_reason} "
                 f"{dt_ms:.0f}ms (session #{session_id}).")
        try:
            self.on_result(pred.vin, score, crop, pred.vin, final_model, session_id)
        except Exception as exc:
            log.error(f"on_result callback xatosi (VIN_SLOT): {exc}")
        return True

    def _log_audit(self, tag: str, fr, crops, reads,
                   telemetry: OCRReadTelemetry, dt_ms, session_id) -> None:
        risky = []
        for p in (1, 2, 4, 9, 10):
            if p < len(fr.decisions):
                d = fr.decisions[p]
                risky.append(f"p{p+1}={d.chosen_char}(m{d.margin:.2f},s{d.support_count})")
        gate_fail = ",".join(fr.reasons) if fr.reasons else "-"
        p5 = fr.pos5_audit
        log.info(
            f"[OCR AUDIT] {tag}: VIN={fr.validated_vin or '-'} model={fr.model} "
            f"score={fr.final_score:.3f} compliant={fr.fully_compliant} "
            f"crops={fr.n_crops} reads={fr.n_reads} {telemetry.summary()} "
            f"raw_support={fr.raw_support_ratio:.2f} risky=[{' '.join(risky)}] "
            f"pos5={{char={p5.get('chosen')}<-{p5.get('raw_direct')}, "
            f"crops={p5.get('support_crops')}, variants={p5.get('support_variants')}, "
            f"margin={p5.get('margin')}, struct_corr={p5.get('structure_corrected')}, "
            f"ok={p5.get('passed')}}} gate_fail=[{gate_fail}] {dt_ms:.0f}ms (session #{session_id})")

    @staticmethod
    def _plurality_raw(reads: List[VariantRead]) -> str:
        from collections import Counter
        c = Counter(r.normalized_raw for r in reads if len(r.normalized_raw) == 17)
        return c.most_common(1)[0][0] if c else ""

    @staticmethod
    def _best_crop(crops: list, crop_quality: List[float]) -> np.ndarray:
        if not crops:
            return None
        if crop_quality and len(crop_quality) == len(crops):
            bi = int(max(range(len(crops)), key=lambda i: crop_quality[i]))
            return crops[bi]
        return crops[0]

    def _save_unread_crop(self, crop: np.ndarray, session_id, status: str,
                          vin: str = "") -> Optional[str]:
        """
        OCR yaroqli VIN bermaganда ham (CRASH / NO_READ / OCR_AMBIGUOUS) crop rasmini
        VAQT + SESSIYA bo'yicha crops papkasiga saqlaydi (keyinchalik qo'lda tekshirish
        va dataset uchun). Fayl nomi: <STATUS>_<sess-SID>_<YYYYMMDD_HHMMSS_ms>[_<vin>].jpg.
        DB ga hech narsa yozilmaydi — bu faqat rasm arxivi. Qaytadi: nisbiy yo'l yoki None.
        """
        if crop is None or getattr(crop, "size", 0) == 0:
            return None
        ts = datetime.now()
        sid = "manual" if session_id is None else str(session_id)
        stamp = ts.strftime("%Y%m%d_%H%M%S_") + f"{ts.microsecond // 1000:03d}"
        vin_part = f"_{vin}" if vin else ""
        fname = f"{status}_sess-{sid}_{stamp}{vin_part}.jpg"
        try:
            Path(CROPS_DIR).mkdir(parents=True, exist_ok=True)
            fpath = Path(CROPS_DIR) / fname
            if not cv2.imwrite(str(fpath), crop):
                return None
            self._cleanup_crops()
            return f"crops/{fname}"
        except Exception as exc:
            log.error(f"O'qilmagan crop saqlanmadi: {exc}")
            return None

    def _cleanup_crops(self) -> None:
        """
        Disk to'lishining oldini oladi: har ~100 saqlashda bir marta CROPS_DIR ni
        tekshiradi va CROP_RETENTION_MAX dan oshsa eng eski fayllarni o'chiradi.
        (pipeline._maybe_cleanup_crops bilan bir xil siyosat; kritik emas.)
        """
        self._crop_save_counter += 1
        if CROP_RETENTION_MAX <= 0 or (self._crop_save_counter % 100) != 1:
            return
        try:
            files = [p for p in Path(CROPS_DIR).glob("*.jpg") if p.is_file()]
            if len(files) <= CROP_RETENTION_MAX:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            for p in files[:len(files) - CROP_RETENTION_MAX]:
                try:
                    p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    # ----- pos5 verifier (lazy, disabled by default) -----
    def _pos5_verify(self, crop, fr) -> bool:
        if not bool(getattr(POS5_VERIFIER, "enabled", False)):
            return True
        try:
            if not self._pos5_verifier_tried:
                self._pos5_verifier_tried = True
                from .pos5_verifier import Pos5Verifier
                self._pos5_verifier = Pos5Verifier.from_config(POS5_VERIFIER)
            if self._pos5_verifier is None or not self._pos5_verifier.ready:
                return True   # model yo'q -> o'tkazib yuboramiz (fail-open, disabled holat)
            ch = fr.validated_vin[4] if len(fr.validated_vin) == 17 else None
            ok, conf = self._pos5_verifier.verify(crop, expected_char=ch)
            if not ok:
                fr.pos5_audit["verifier_conf"] = round(float(conf), 3)
            return bool(ok)
        except Exception as exc:
            log.warning(f"pos5 verifier xatosi (o'tkazib yuborildi): {exc}")
            return True

    def _emit_fail(self, session_id, raw: str) -> None:
        cb = self.on_fail
        if cb is None:
            return
        try:
            cb(session_id, raw)
        except Exception as exc:
            log.error(f"on_fail callback xatosi: {exc}")

    def _is_duplicate(self, session_id, vin: str) -> bool:
        now = time.time()
        with self._dup_lock:
            same_session = (session_id == self._last_vin_session)
            if (self._last_vin == vin and same_session
                    and (now - self._last_vin_ts) < OCR.duplicate_window_sec):
                return True
            self._last_vin = vin
            self._last_vin_ts = now
            self._last_vin_session = session_id
            return False
