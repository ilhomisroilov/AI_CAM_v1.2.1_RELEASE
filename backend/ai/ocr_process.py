"""
============================================================
ocr_process.py  —  PROCESS-IZOLYATSIYALANGAN PaddleOCR worker pool
============================================================
Nima uchun (Section 1): PaddleOCR native (C++ / MKLDNN / oneDNN) crashi
Python `try/except` bilan TUTILMAYDI — u SIGSEGV / core dump beradi va
BUTUN jarayonni o'ldiradi. Yechim: OCR inference'ni ALOHIDA PROCESS(lar)da
ishlatish.

Kafolatlar:
  * Har worker process AYNAN BITTA lazy-init PaddleOCR nusxasiga ega.
  * Asosiy ilova crop/variant tasklarini navbat (Queue) orqali yuboradi.
  * Har taskда timeout va butun batch uchun alohida deadline bor.
  * Worker SIGSEGV/core-dump bilan o'lsa — ASOSIY ILOVA TIRIK QOLADI.
  * O'lgan worker AVTOMATIK qayta yaratiladi.
  * EMPTY / Python ERROR / native CRASH / TIMEOUT bir-biridan ajratiladi.
  * Fatal engine-init xatosi (masalan cuDNN yuklanmasligi) circuit breaker'ni
    ochadi; qolgan variantlarda bir xil engine qayta-qayta qurilmaydi.
  * Python thread ICHIDA parallel PaddleOCR YO'Q — parallellik faqat
    process worker soni + navbat orqali (ThreadPoolExecutor OLIB TASHLANDI).

MKLDNN: agar RuntimeError'da "could not execute a primitive" bo'lsa, worker
MKLDNN'ni O'CHIRIB bir marta qayta quradi. Haqiqiy SIGSEGV uchun esa process
izolyatsiyasiga tayanamiz (Python istisno emas).

Bu modul asosiy jarayonда PaddleOCR'ni IMPORT QILMAYDI — u faqat worker
process ichида import qilinadi (parent'да torch/YOLO bilan ziddiyat bo'lmasin).
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue as _queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# --- Natija statuslari ---
OK = "OK"
EMPTY = "EMPTY"                 # OCR ishladi, lekin belgi topilmadi
OCR_ENGINE_CRASH = "OCR_ENGINE_CRASH"   # worker process native tarzda o'ldi
OCR_ENGINE_TIMEOUT = "OCR_ENGINE_TIMEOUT"  # task yoki umumiy batch deadline
ERROR = "ERROR"                # Python istisnosi (tutildi, engine tirik)
ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"  # fatal init/runtime dependency xatosi


_FATAL_ENGINE_MARKERS = (
    "cannot load cudnn", "cudnn_dso_handle", "cudnngetversion",
    "cannot load cublas", "cannot load cuda", "cuda driver version is insufficient",
    "libcudnn", "libcublas", "no kernel image is available",
)


def _safe_error(exc: object, limit: int = 1000) -> str:
    """Queue/log telemetry uchun bitta qatordagi cheklangan xato matni."""
    text = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    text = " ".join(text.split())
    return text[:limit]


def _fatal_engine_error(exc: object) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _FATAL_ENGINE_MARKERS)


def _constructor_compat_error(exc: BaseException) -> bool:
    """Faqat PaddleOCR API-signature farqida boshqa constructorni sinaymiz."""
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "unexpected keyword", "unknown argument", "unrecognized argument",
        "invalid keyword", "got multiple values for argument",
    ))


class _EngineUnavailableError(RuntimeError):
    """Paddle engine'ni bu process/environmentda qurib bo'lmaydi."""

    def __init__(self, cause: object, phase: str = "engine_init") -> None:
        self.phase = phase
        self.detail = _safe_error(cause)
        super().__init__(self.detail)


@dataclass
class OCRTaskResult:
    """Bitta variant OCR taskining natijasi (main jarayonда)."""
    status: str  # OK | EMPTY | ERROR | OCR_ENGINE_CRASH | OCR_ENGINE_TIMEOUT | ENGINE_UNAVAILABLE
    fragments: List[Tuple[str, float]] = field(default_factory=list)  # chapdan-o'ngga (text, conf)
    meta: dict = field(default_factory=dict)     # engine ma'lumoti / xato matni

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def crashed(self) -> bool:
        return self.status == OCR_ENGINE_CRASH

    @property
    def timed_out(self) -> bool:
        return self.status == OCR_ENGINE_TIMEOUT


@dataclass
class OCRBatchTelemetry:
    """Oxirgi pool batchidagi task statuslari; observability uchun barqaror schema."""

    total: int = 0
    ok: int = 0
    empty: int = 0
    errors: int = 0
    crashes: int = 0
    timeouts: int = 0
    engine_unavailable: int = 0
    elapsed_ms: float = 0.0
    deadline_exceeded: bool = False

    @classmethod
    def from_results(cls, results, elapsed_ms: float = 0.0) -> "OCRBatchTelemetry":
        out = cls(total=len(results), elapsed_ms=float(elapsed_ms))
        for result in results:
            status = getattr(result, "status", ERROR) if result is not None else ERROR
            if status == OK:
                out.ok += 1
            elif status == EMPTY:
                out.empty += 1
            elif status == OCR_ENGINE_CRASH:
                out.crashes += 1
            elif status == OCR_ENGINE_TIMEOUT:
                out.timeouts += 1
                out.deadline_exceeded = True
            elif status == ENGINE_UNAVAILABLE:
                out.engine_unavailable += 1
            else:
                out.errors += 1
        return out

    def as_dict(self) -> dict:
        return {
            "total": self.total, "ok": self.ok, "empty": self.empty,
            "errors": self.errors, "crashes": self.crashes,
            "timeouts": self.timeouts,
            "engine_unavailable": self.engine_unavailable,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "deadline_exceeded": self.deadline_exceeded,
        }


# ==================================================================
# WORKER TARAF (alohida process ichida ishlaydi)
# ==================================================================
def _apply_thread_env(cpu_threads: int) -> None:
    """
    Paddle/PaddleOCR IMPORT'idan OLDIN process-daraja thread env'larini o'rnatadi.
    oneDNN/OMP/MKL ni cheklash SIGSEGV va CPU haddan tashqari yuklanish xavfini
    kamaytiradi (Section 1).
    """
    n = str(max(1, int(cpu_threads or 1)))
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[var] = n
    # oneDNN primitive cache'ini kichraytirish (barqarorlik)
    os.environ.setdefault("DNNL_PRIMITIVE_CACHE_CAPACITY", "1")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")


class _WorkerEngine:
    """
    Worker process ICHIDAGI PaddleOCR wrapper (bitta nusxa, lazy). read(img, det)
    -> [(text, conf), ...] chapdan-o'ngga tartiblangan.
    Bu klass FAQAT worker process'да yaratiladi (paddleocr import shu yerда).
    """
    _MKLDNN_MARKERS = ("could not execute a primitive", "primitive",
                       "mkldnn", "onednn", "dnnl")

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._mkldnn = False
        self._ocr = None
        self._api_v3 = False
        self._build(disable_mkldnn=not bool(cfg.get("enable_mkldnn", False)))

    def _build(self, disable_mkldnn: bool = True) -> None:
        from paddleocr import PaddleOCR  # IMPORT worker process ICHIDA
        cfg = self.cfg
        gpu = bool(cfg.get("use_gpu", False))
        lang = cfg.get("lang", "en")
        use_cls = bool(cfg.get("use_angle_cls", False))
        dev = "gpu" if gpu else "cpu"
        cpu_opts: dict = {}
        self._mkldnn = False
        if not gpu:
            if bool(cfg.get("enable_mkldnn", False)) and not disable_mkldnn:
                cpu_opts["enable_mkldnn"] = True
                self._mkldnn = True
            ct = int(cfg.get("cpu_threads", 1) or 1)
            if ct > 0:
                cpu_opts["cpu_threads"] = ct
        # Self-contained model papkalari (repo ichidagi models/paddle/*).
        # They are mandatory: no constructor attempt is allowed to omit these
        # paths and silently fall back to ~/.paddleocr or a download cache.
        model_dirs: dict = {}
        from ..config import PADDLE_CLS_DIR, PADDLE_DET_DIR, PADDLE_REC_DIR
        configured_defaults = {
            "det_model_dir": PADDLE_DET_DIR,
            "rec_model_dir": PADDLE_REC_DIR,
            "cls_model_dir": PADDLE_CLS_DIR,
        }
        for _k in ("det_model_dir", "rec_model_dir", "cls_model_dir"):
            _v = cfg.get(_k) or configured_defaults[_k]
            _p = os.path.abspath(os.fspath(_v))
            if not os.path.isdir(_p):
                raise _EngineUnavailableError(
                    f"required project-local Paddle directory not found: {_k}={_p}",
                    phase="engine_init",
                )
            model_dirs[_k] = _p
        attempts = [
            dict(use_angle_cls=use_cls, use_gpu=gpu, show_log=False, lang=lang, **model_dirs, **cpu_opts),
            dict(use_angle_cls=use_cls, use_gpu=gpu, lang=lang, **model_dirs, **cpu_opts),
            dict(use_angle_cls=use_cls, use_gpu=gpu, lang=lang, **model_dirs),
        ]
        last_err = None
        used = {}
        for kw in attempts:
            try:
                self._ocr = PaddleOCR(**kw)
                used = kw
                break
            except Exception as exc:
                last_err = exc
                self._ocr = None
                # Old/new PaddleOCR versiyalaridagi constructor argument farqi
                # uchungina keyingi signature'ni sinash mumkin. cuDNN/CUDA/model
                # init xatosini olti marta takrorlash real serverda 16x re-init va
                # ~22s NO_READ keltirib chiqargan.
                if not _constructor_compat_error(exc):
                    raise _EngineUnavailableError(exc, phase="engine_init") from exc
        if self._ocr is None:
            raise _EngineUnavailableError(
                last_err or "PaddleOCR constructor signature mos kelmadi",
                phase="engine_init",
            )
        self._api_v3 = ("device" in used or "use_textline_orientation" in used)

    def meta(self) -> dict:
        return {"mkldnn": self._mkldnn, "api_v3": self._api_v3}

    @staticmethod
    def _frag_left_x(box) -> float:
        try:
            return float(min(pt[0] for pt in box))
        except Exception:
            return 0.0

    @staticmethod
    def _serializable_box(box):
        try:
            return np.asarray(box).tolist()
        except Exception:
            return box

    def _parse_predict(self, res) -> List[Tuple[float, str, float, object]]:
        out = []
        for item in (res or []):
            try:
                d = item
                texts = d["rec_texts"]
                scores = d["rec_scores"]
                polys = d.get("rec_polys") or d.get("dt_polys") or []
            except Exception:
                continue
            for i, t in enumerate(texts):
                sc = float(scores[i]) if i < len(scores) else 0.0
                box = polys[i] if i < len(polys) else [[i, 0]]
                out.append((self._frag_left_x(box), str(t), sc,
                            self._serializable_box(box)))
        return out

    def _parse(self, res) -> List[Tuple[float, str, float, object]]:
        out = []
        if not res:
            return out
        page = res[0] if isinstance(res, (list, tuple)) and len(res) > 0 else res
        if not page:
            return out
        for idx, line in enumerate(page):
            try:
                if (isinstance(line, (list, tuple)) and len(line) == 2
                        and isinstance(line[0], (list, tuple)) and line[0]
                        and isinstance(line[0][0], (list, tuple))):
                    box = line[0]
                    text, score = line[1][0], line[1][1]
                    x = self._frag_left_x(box)
                else:
                    text, score = line[0], line[1]
                    x = float(idx)
                    box = [[idx, 0]]
                out.append((x, str(text), float(score),
                            self._serializable_box(box)))
            except Exception:
                continue
        return out

    def _infer(self, img: np.ndarray, det: bool) -> List[Tuple[float, str, float, object]]:
        use_cls = bool(self.cfg.get("use_angle_cls", False))
        if self._api_v3 and hasattr(self._ocr, "predict"):
            try:
                return self._parse_predict(self._ocr.predict(img))
            except Exception:
                pass
        try:
            res = self._ocr.ocr(img, det=det, cls=use_cls)
        except TypeError:
            res = self._ocr.ocr(img)
        except Exception:
            if hasattr(self._ocr, "predict"):
                return self._parse_predict(self._ocr.predict(img))
            raise
        return self._parse(res)

    def read(self, img: np.ndarray, det: bool):
        import cv2
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        try:
            frags = self._infer(img, det)
        except RuntimeError as exc:
            if self._mkldnn and any(m in str(exc).lower() for m in self._MKLDNN_MARKERS):
                # MKLDNN'ni O'CHIRIB bir marta qayta quramiz (Section 1)
                self._build(disable_mkldnn=True)
                frags = self._infer(img, det)
            else:
                raise
        drop = float(self.cfg.get("drop_score", 0.30))
        frags = [(x, t, c, box) for (x, t, c, box) in frags
                 if c >= drop and t.strip()]
        frags.sort(key=lambda r: r[0])
        payload = [
            {"text": t, "confidence": float(c), "box": box}
            for (_x, t, c, box) in frags
        ]
        return [(t, c) for (_x, t, c, _box) in frags], payload


def _worker_main(cfg: dict, in_q, out_q) -> None:
    """
    Worker process kirish nuqtasi. Navbatdan (task_id, img, det) oladi, OCR qiladi,
    natijani (task_id, status, fragments, meta) qaytaradi. `None` -> to'xtash.
    Engine LAZY quriladi (birinchi task). Har qanday Python istisnosi TUTILADI va
    ERROR sifatida qaytariladi (native SIGSEGV esa process'ni o'ldiradi -> main
    tomon buni exitcode orqali aniqlaydi).
    """
    _apply_thread_env(int(cfg.get("cpu_threads", 1) or 1))
    engine: Optional[_WorkerEngine] = None
    while True:
        try:
            item = in_q.get()
        except (EOFError, OSError):
            break
        if item is None:
            break
        task_id, img, det = item
        try:
            if engine is None:
                engine = _WorkerEngine(cfg)      # lazy: model shu yerда yuklanadi
            frags, raw_payload = engine.read(img, bool(det))
            status = OK if frags else EMPTY
            try:
                out_q.put((task_id, status, frags, {
                    **engine.meta(),
                    "raw_payload": raw_payload,
                    "boxes": [item.get("box") for item in raw_payload],
                }))
            except Exception:
                pass
        except _EngineUnavailableError as exc:
            try:
                out_q.put((task_id, ENGINE_UNAVAILABLE, [], {
                    "err": exc.detail,
                    "reason": "engine_unavailable",
                    "phase": exc.phase,
                    "fatal": True,
                }))
            except Exception:
                pass
            engine = None
        except Exception as exc:                 # Python istisnosi -> ERROR (crash emas)
            fatal = _fatal_engine_error(exc)
            status = ENGINE_UNAVAILABLE if fatal else ERROR
            try:
                out_q.put((task_id, status, [], {
                    "err": _safe_error(exc),
                    "reason": "engine_unavailable" if fatal else "python_exception",
                    "phase": "inference",
                    "fatal": fatal,
                }))
            except Exception:
                pass
            # read() istisnosidan keyin keyingi task eski native engine'ni ishlatmasin.
            engine = None


# ==================================================================
# MAIN TARAF (asosiy ilova process'да)
# ==================================================================
class _Worker:
    """Bitta worker process + unga bog'langan navbatlar (main tarafida boshqaruv)."""

    def __init__(self, ctx, cfg: dict, target=_worker_main) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._target = target
        self.in_q = ctx.Queue()
        self.out_q = ctx.Queue()
        self.proc: Optional[mp.process.BaseProcess] = None
        self.warmed = False
        self.restarts = 0

    def start(self) -> None:
        self.in_q = self._ctx.Queue()
        self.out_q = self._ctx.Queue()
        self.proc = self._ctx.Process(
            target=self._target, args=(self._cfg, self.in_q, self.out_q), daemon=True)
        self.proc.start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def exitcode(self):
        return self.proc.exitcode if self.proc is not None else None

    def submit(self, task_id: int, img: np.ndarray, det: bool) -> None:
        self.in_q.put((task_id, img, det))

    def poll(self, block_sec: float = 0.02):
        try:
            return self.out_q.get(timeout=block_sec)
        except _queue.Empty:
            return None

    def kill(self) -> None:
        p = self.proc
        if p is None:
            return
        try:
            if p.is_alive():
                p.terminate()
                p.join(timeout=2.0)
            if p.is_alive():
                # terminate ishlamasa (native kod ichida) — kill
                try:
                    p.kill()
                except Exception:
                    pass
                p.join(timeout=2.0)
        except Exception:
            pass
        self.proc = None
        self.warmed = False


class OCRProcessPool:
    """
    Process-izolyatsiyalangan PaddleOCR worker pool. run_batch() variant rasmlarini
    workerlarga taqsimlaydi, timeout/crash'larni boshqaradi va tartiblangan
    natijalar ro'yxatini qaytaradi. ThreadPoolExecutor ISHLATILMAYDI.
    """

    def __init__(self, cfg: dict, n_workers: int = 1, task_timeout_sec: float = 12.0,
                 start_timeout_sec: float = 60.0, max_restarts: int = 50,
                 batch_timeout_sec: Optional[float] = 15.0,
                 logger=None, worker_target=_worker_main) -> None:
        self.cfg = dict(cfg)
        self.n_workers = max(1, int(n_workers))
        self.task_timeout = float(task_timeout_sec)
        self.start_timeout = float(start_timeout_sec)
        self.batch_timeout = (None if batch_timeout_sec is None
                              else max(0.001, float(batch_timeout_sec)))
        self.max_restarts = int(max_restarts)
        self._log = logger
        self._worker_target = worker_target
        # spawn: parent'да torch/YOLO/CUDA yuklangan — fork XAVFLI. spawn toza process.
        try:
            self._ctx = mp.get_context("spawn")
        except ValueError:
            self._ctx = mp.get_context()
        self.workers: List[_Worker] = []
        self._total_restarts = 0
        self._started = False
        self._engine_unavailable = False
        self._engine_unavailable_meta: dict = {}
        self.last_batch_telemetry = OCRBatchTelemetry()

    # ---- logging yordamchisi ----
    def _info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    # ---- hayotiy sikl ----
    def start(self) -> None:
        if self._started:
            return
        for _ in range(self.n_workers):
            w = _Worker(self._ctx, self.cfg, target=self._worker_target)
            w.start()
            self.workers.append(w)
        self._started = True
        self._info(f"OCR process pool ishga tushdi ({self.n_workers} izolyatsiyalangan "
                   f"worker, mkldnn={self.cfg.get('enable_mkldnn')}, "
                   f"cpu_threads={self.cfg.get('cpu_threads')}).")

    def preload(self) -> bool:
        """
        Har workerни kichik dummy rasm bilan "isitadi" (PaddleOCR modelini oldindan
        yuklaydi). Birinchi haqiqiy VIN so'rovi sekin bo'lmasligi uchun.
        """
        if not self._started:
            self.start()
        dummy = np.full((48, 160, 3), 255, dtype=np.uint8)
        results = self.run_batch([(dummy, False)] * self.n_workers,
                                 timeout_override=self.start_timeout,
                                 batch_timeout_sec=self.start_timeout)
        ok = any(r is not None and r.status in (OK, EMPTY) for r in results)
        if ok:
            self._info("OCR pool preload: OK.")
            return True

        failed = next((r for r in results if r is not None), None)
        meta = dict(getattr(failed, "meta", {}) or {})
        meta.setdefault("reason", "preload_failed")
        meta.setdefault("err", "PaddleOCR preload natija bermadi")
        self._mark_engine_unavailable(meta)
        self._warn(
            "OCR pool preload: ENGINE_UNAVAILABLE — "
            f"{meta.get('err') or meta.get('reason')}"
        )
        return False

    @property
    def engine_status(self) -> str:
        if self._engine_unavailable:
            return "engine_unavailable"
        if any(w.warmed for w in self.workers):
            return "ready"
        return "not_ready"

    @property
    def engine_unavailable_meta(self) -> dict:
        return dict(self._engine_unavailable_meta)

    def _mark_engine_unavailable(self, meta: Optional[dict]) -> None:
        clean = dict(meta or {})
        clean.setdefault("reason", "engine_unavailable")
        clean["fatal"] = True
        self._engine_unavailable = True
        self._engine_unavailable_meta = clean

    def get_telemetry(self) -> dict:
        return {
            "engine_status": self.engine_status,
            "engine_unavailable_meta": self.engine_unavailable_meta,
            "last_batch": self.last_batch_telemetry.as_dict(),
        }

    def _restart_worker(self, wi: int, reason: str) -> None:
        w = self.workers[wi]
        w.kill()
        if self._total_restarts >= self.max_restarts:
            self._warn(f"OCR worker #{wi} qayta yaratish chegarasi ({self.max_restarts}) — "
                       f"qayta yaratilmadi ({reason}).")
            return
        self._total_restarts += 1
        w.restarts += 1
        try:
            w.start()
            self._warn(f"OCR worker #{wi} QAYTA YARATILDI ({reason}; "
                       f"jami restart={self._total_restarts}).")
        except Exception as exc:
            self._warn(f"OCR worker #{wi} qayta yaratilmadi: {exc}")

    def run_batch(self, tasks: Sequence[Tuple[np.ndarray, bool]],
                  timeout_override: Optional[float] = None,
                  batch_deadline: Optional[float] = None,
                  batch_timeout_sec: Optional[float] = None
                  ) -> List[Optional[OCRTaskResult]]:
        """Variantlarni bajaradi; per-task timeout va umumiy batch deadline mustaqil."""
        started_at = time.monotonic()
        if not self._started:
            self.start()
        n = len(tasks)
        results: List[Optional[OCRTaskResult]] = [None] * n

        def _finish() -> List[Optional[OCRTaskResult]]:
            # None observability'da yo'qolmasin: u ham alohida ERROR.
            for idx, result in enumerate(results):
                if result is None:
                    results[idx] = OCRTaskResult(
                        ERROR, [], {"reason": "missing_result", "err": "worker natija qaytarmadi"})
            self.last_batch_telemetry = OCRBatchTelemetry.from_results(
                results, elapsed_ms=(time.monotonic() - started_at) * 1000.0)
            return results

        if n == 0:
            return _finish()
        if self._engine_unavailable:
            meta = {**self._engine_unavailable_meta, "reason": "engine_unavailable_circuit_open"}
            results[:] = [OCRTaskResult(ENGINE_UNAVAILABLE, [], dict(meta)) for _ in range(n)]
            return _finish()

        duration = self.batch_timeout if batch_timeout_sec is None else float(batch_timeout_sec)
        deadlines = []
        if duration is not None and duration > 0:
            deadlines.append(started_at + duration)
        if batch_deadline is not None:
            deadlines.append(float(batch_deadline))
        effective_batch_deadline = min(deadlines) if deadlines else None

        pending = deque(range(n))
        # worker_index -> (task_index, per-task deadline)
        inflight: Dict[int, Tuple[int, float]] = {}

        def _task_deadline(w: _Worker) -> float:
            base = timeout_override if timeout_override is not None else self.task_timeout
            if not w.warmed:
                base = max(base, self.start_timeout)   # modelning birinchi yuklanish budjeti
            return time.monotonic() + base

        def _expire_batch() -> None:
            # In-flight native call'ni processni qayta yaratmasdan bekor qilib bo'lmaydi.
            for wi, (ti, _deadline) in list(inflight.items()):
                results[ti] = OCRTaskResult(
                    OCR_ENGINE_TIMEOUT, [], {"reason": "batch_deadline_inflight"})
                self._restart_worker(wi, "umumiy OCR batch deadline")
            inflight.clear()
            while pending:
                ti = pending.popleft()
                results[ti] = OCRTaskResult(
                    OCR_ENGINE_TIMEOUT, [], {"reason": "batch_deadline_not_started"})

        while pending or inflight:
            if (effective_batch_deadline is not None
                    and time.monotonic() >= effective_batch_deadline):
                _expire_batch()
                break

            # 1) Bo'sh (va tirik) workerlarga task beramiz.
            for wi, w in enumerate(self.workers):
                if wi in inflight or not pending:
                    continue
                if not w.alive():
                    self._restart_worker(wi, "topildi: o'lik (assign oldidan)")
                    if not w.alive():
                        continue
                ti = pending.popleft()
                img, det = tasks[ti]
                try:
                    w.submit(ti, img, det)
                    inflight[wi] = (ti, _task_deadline(w))
                except Exception as exc:
                    results[ti] = OCRTaskResult(
                        ERROR, [], {"reason": "submit_error", "err": _safe_error(exc)})

            # 2) Natija / timeout / o'lim tekshiruvi.
            progressed = False
            circuit_opened = False
            for wi in list(inflight.keys()):
                w = self.workers[wi]
                ti, deadline = inflight[wi]
                msg = w.poll(block_sec=0.02)
                if msg is not None:
                    task_id, status, frags, meta = msg
                    idx = task_id if 0 <= task_id < n else ti
                    clean_meta = dict(meta or {})
                    results[idx] = OCRTaskResult(status, list(frags or []), clean_meta)
                    # ERROR'da worker engine=None bo'ladi; uni isitilgan deb belgilash
                    # keyingi task timeoutini noto'g'ri qisqartirardi.
                    w.warmed = status in (OK, EMPTY)
                    del inflight[wi]
                    progressed = True

                    fatal = (status == ENGINE_UNAVAILABLE
                             or bool(clean_meta.get("fatal"))
                             or _fatal_engine_error(clean_meta.get("err", "")))
                    if fatal:
                        self._mark_engine_unavailable(clean_meta)
                        circuit_meta = {
                            **self._engine_unavailable_meta,
                            "reason": "engine_unavailable_circuit_open",
                            "source_task": idx,
                        }
                        while pending:
                            pi = pending.popleft()
                            results[pi] = OCRTaskResult(
                                ENGINE_UNAVAILABLE, [], dict(circuit_meta))
                        # Boshqa workerda parallel boshlangan bir xil fatal initni kutmaymiz.
                        for other_wi, (other_ti, _d) in list(inflight.items()):
                            self.workers[other_wi].kill()
                            results[other_ti] = OCRTaskResult(
                                ENGINE_UNAVAILABLE, [], dict(circuit_meta))
                        inflight.clear()
                        circuit_opened = True
                        break
                    continue
                # process o'ldimi? (SIGSEGV -> exitcode manfiy / core dump)
                if not w.alive():
                    ec = w.exitcode()
                    results[ti] = OCRTaskResult(
                        OCR_ENGINE_CRASH, [], {"exitcode": ec, "reason": "worker_died"})
                    self._restart_worker(wi, f"process o'ldi (exitcode={ec}, ehtimol SIGSEGV)")
                    del inflight[wi]
                    progressed = True
                    continue
                # Per-task timeout umumiy batch deadline'dan alohida klassifikatsiya.
                if time.monotonic() > deadline:
                    results[ti] = OCRTaskResult(
                        OCR_ENGINE_TIMEOUT, [], {"reason": "task_timeout"})
                    self._restart_worker(wi, "task timeout (native osilish)")
                    del inflight[wi]
                    progressed = True
                    continue
            if circuit_opened:
                break
            if not progressed:
                time.sleep(0.005)
        return _finish()

    def shutdown(self) -> None:
        for w in self.workers:
            try:
                w.in_q.put(None)
            except Exception:
                pass
        t0 = time.monotonic()
        for w in self.workers:
            p = w.proc
            if p is not None:
                p.join(timeout=max(0.1, 2.0 - (time.monotonic() - t0)))
        for w in self.workers:
            w.kill()
        self.workers = []
        self._started = False
        self._engine_unavailable = False
        self._engine_unavailable_meta = {}
        self._info("OCR process pool to'xtatildi (shutdown).")


# ==================================================================
# SESSION-JOB SUPERVISOR COMPATIBILITY
# ==================================================================
# The production variant pool above isolates every Paddle task. The session state
# machine/tests also use this higher-level one-job supervisor contract for hard
# timeout and restart verification. Keeping both APIs prevents a release merge
# from silently dropping hang/crash protection.
_SUP_STARTED = "__STARTED__"
_SUP_ERROR = "__ERROR__"


def _supervisor_worker_entry(kind: str, job_q, result_q, params: dict) -> None:
    if kind == "real":
        # Daemon supervisor ichidan OCRProcessPool workerlarini yana spawn qilib
        # bo'lmaydi. Eski yo'l OCRWorker.compute_result(_engine=None) -> None
        # qaytarib, testni 120s poll'da qoldirardi. Production/real integration
        # to'g'ridan-to'g'ri OCRProcessPool ishlatadi; bu legacy mode endi tez va
        # aniq terminal error qaytaradi.
        while True:
            job = job_q.get()
            if job is None:
                return
            result_q.put(_SUP_STARTED)
            result_q.put((_SUP_ERROR,
                          "worker_kind='real' nested supervisor qo'llanmaydi; "
                          "real inference uchun OCRProcessPool ishlating"))

    from .ocr_worker import OCRResult
    behavior = params.get("behavior", "normal")
    slow_ms = int(params.get("slow_ms", 0) or 0)
    vin = params.get("vin", "NSTFA814ATJ100000")
    hang_session = params.get("hang_session")
    while True:
        job = job_q.get()
        if job is None:
            return
        result_q.put(_SUP_STARTED)
        effective = behavior
        if hang_session is not None:
            effective = "hang" if job.session_id == hang_session else "normal"
        if effective == "crash":
            os._exit(71)
        if effective == "hang":
            while True:
                time.sleep(3600)
        if effective == "exception":
            result_q.put((_SUP_ERROR, "simulated worker exception"))
            continue
        if effective == "slow" and slow_ms > 0:
            time.sleep(slow_ms / 1000.0)
        result_q.put(OCRResult(
            session_id=job.session_id, frame_id=job.frame_id, vin=vin,
            confidence=0.95, model="QY", completed_at=time.time(),
            attempt_count=1, raw_vin=vin, crop=None))


class OCRProcessSupervisor:
    """One session job/process with soft/hard timeout and automatic respawn."""

    def __init__(self, on_result: Callable, worker_kind: str = "real",
                 worker_params: Optional[dict] = None,
                 soft_timeout_ms: Optional[int] = None,
                 hard_timeout_ms: Optional[int] = None) -> None:
        self.on_result = on_result
        self._kind = worker_kind
        self._params = dict(worker_params or {})
        self._soft = max(0.001, float(soft_timeout_ms or 2000) / 1000.0)
        self._hard = max(self._soft, float(hard_timeout_ms or 15000) / 1000.0)
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._job_q = None
        self._result_q = None
        self._inbox = _queue.Queue(maxsize=int(self._params.get("inbox_max", 50)))
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self.metrics = {
            "ocr_soft_timeout_total": 0,
            "ocr_hard_timeout_total": 0,
            "ocr_worker_restart_total": 0,
            "ocr_worker_crash_total": 0,
            "ocr_worker_error_total": 0,
            "late_ocr_result_total": 0,
            "processed_total": 0,
        }
        self.last_error = ""

    def _spawn(self) -> None:
        with self._lock:
            self._job_q = self._ctx.Queue()
            self._result_q = self._ctx.Queue()
            self._proc = self._ctx.Process(
                target=_supervisor_worker_entry,
                args=(self._kind, self._job_q, self._result_q, self._params),
                daemon=True)
            self._proc.start()

    def _kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.5)
            if proc.is_alive():
                try:
                    proc.kill()
                except Exception:
                    pass
        with self._lock:
            self._proc = None

    def _respawn(self) -> None:
        self._kill()
        self.metrics["ocr_worker_restart_total"] += 1
        if self._running:
            self._spawn()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._spawn()
        self._thread = threading.Thread(target=self._loop, name="ocr-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._inbox.put_nowait(None)
        except Exception:
            pass
        self._kill()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def submit_job(self, job) -> None:
        if not self._running or job is None:
            return
        try:
            self._inbox.put_nowait(job)
        except _queue.Full:
            try:
                self._inbox.get_nowait()
                self._inbox.put_nowait(job)
            except Exception:
                pass

    def _loop(self) -> None:
        while self._running:
            try:
                job = self._inbox.get(timeout=0.2)
            except _queue.Empty:
                continue
            if job is None:
                return
            self._run_job(job)

    def _run_job(self, job) -> None:
        with self._lock:
            proc, job_q, result_q = self._proc, self._job_q, self._result_q
        if proc is None:
            self._respawn()
            return
        job_q.put(job)
        dispatch_at = time.monotonic()
        started_at = None
        soft_logged = False
        while self._running:
            now = time.monotonic()
            if not proc.is_alive():
                self.metrics["ocr_worker_crash_total"] += 1
                self.last_error = f"worker process o'ldi (exitcode={proc.exitcode})"
                self._respawn()
                return
            if started_at is not None:
                elapsed = now - started_at
                if elapsed >= self._hard:
                    self.metrics["ocr_hard_timeout_total"] += 1
                    self.last_error = f"worker hard timeout ({elapsed:.3f}s)"
                    self._respawn()
                    return
                if not soft_logged and elapsed >= self._soft:
                    soft_logged = True
                    self.metrics["ocr_soft_timeout_total"] += 1
            elif now - dispatch_at >= max(5.0, self._hard):
                self.metrics["ocr_hard_timeout_total"] += 1
                self.last_error = "worker startup timeout"
                self._respawn()
                return
            try:
                msg = result_q.get(timeout=0.05)
            except _queue.Empty:
                continue
            if msg == _SUP_STARTED:
                started_at = time.monotonic()
                continue
            self.metrics["processed_total"] += 1
            if (isinstance(msg, tuple) and len(msg) == 2 and msg[0] == _SUP_ERROR):
                self.metrics["ocr_worker_error_total"] += 1
                self.last_error = str(msg[1])[:1000]
                return
            if msg is None:
                self.metrics["ocr_worker_error_total"] += 1
                self.last_error = "worker natijasiz terminal None qaytardi"
                return
            self.on_result(msg)
            return

    def get_stats(self) -> dict:
        with self._lock:
            alive = self._proc is not None and self._proc.is_alive()
        return {"running": self._running, "worker_alive": alive,
                "queue": self._inbox.qsize(), "last_error": self.last_error,
                **self.metrics}
