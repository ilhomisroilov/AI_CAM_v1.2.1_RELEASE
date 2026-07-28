"""
ocr_collector.py — non-blocking active-learning character collector (v1.2.1).

Saves original frame, normalized line, and per-position character crops (adaptive
segmentation) for hard/uncertain/target cases, plus a collection DB row and metadata.json.
Trusted labels come ONLY from the session-owned expected VIN (never OCR/filename). Errors
are logged + counted and NEVER propagate into the production OCR path.
"""
from __future__ import annotations
import hashlib, json, os, queue, threading, time, uuid
from datetime import datetime
from typing import Optional
import numpy as np

try:
    from PIL import Image
except Exception:
    Image = None

TARGET_DEFAULT = "V,G,A,D,H,B,5,6,7,0,2,3"
SUPPORTED = set("0123456789ABCDEFHJNST")


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _adaptive_boxes(gray: np.ndarray, n: int):
    col = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1])).sum(axis=0)
    col = col + np.abs(gray - np.median(gray, axis=0, keepdims=True)).sum(axis=0)
    k = max(3, gray.shape[1] // 120)
    col = np.convolve(col, np.ones(k) / k, mode="same")
    thr = 0.12 * col.max() if col.max() > 0 else 0
    act = np.where(col > thr)[0]
    x0, x1 = (int(act[0]), int(act[-1]) + 1) if len(act) >= n else (0, len(col))
    w = max(1, (x1 - x0)) / n
    cuts = [x0]
    for i in range(1, n):
        init = int(x0 + i * w); lo = max(x0 + 1, int(init - 0.45 * w)); hi = min(x1 - 1, int(init + 0.45 * w))
        cuts.append(lo + int(np.argmin(col[lo:hi])) if hi > lo else init)
    cuts.append(x1)
    H = gray.shape[0]
    return [(cuts[i], 0, max(1, cuts[i + 1] - cuts[i]), H) for i in range(n)]


class ActiveLearningCollector:
    def __init__(self, root: str, store_fn=None, model_version: str = "1.2.1",
                 target_chars: str = TARGET_DEFAULT, enabled: bool = True, logger=None) -> None:
        self.root = root
        self.store_fn = store_fn        # callable(item_dict) -> bool (False if dup); may be None
        self.model_version = model_version
        self.targets = set((target_chars or "").replace(" ", "").split(",")) - {""}
        self.enabled = enabled
        self._log = logger
        self.errors = 0
        self.collected = 0
        self._q: "queue.Queue" = queue.Queue(maxsize=256)
        self._inflight = 0
        self._stop = False
        self._thread = threading.Thread(target=self._worker, name="ocr-collector", daemon=True)
        self._thread.start()

    def submit(self, job: dict) -> None:
        """Non-blocking: enqueue and return immediately. Never raises."""
        if not self.enabled:
            return
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self.errors += 1
        except Exception:
            self.errors += 1

    def _worker(self) -> None:
        while not self._stop:
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._inflight += 1
            try:
                self._process(job)
            except Exception as exc:
                self.errors += 1
                if self._log: self._log.warning(f"[OCR_COLLECTOR] error (isolated): {exc}")
            finally:
                self._inflight -= 1

    def flush(self, timeout: float = 5.0) -> None:
        end = time.time() + timeout
        while (not self._q.empty() or self._inflight > 0) and time.time() < end:
            time.sleep(0.02)

    def close(self, timeout: float = 5.0) -> None:
        self.flush(timeout=max(0.0, float(timeout)))
        self._stop = True
        if (self._thread.is_alive()
                and self._thread is not threading.current_thread()):
            self._thread.join(timeout=max(0.0, float(timeout)))

    # -- core processing (also directly callable in tests) --
    def _process(self, job: dict) -> int:
        if Image is None:
            return 0
        session_id = job["session_id"]
        expected_vin = job.get("expected_vin") or ""
        trusted = bool(job.get("trusted_label"))          # only True when VIN is session-owned trusted
        line = job.get("normalized_line")                 # np.ndarray (grayscale)
        frame = job.get("frame")                          # np.ndarray or None
        per_char = job.get("per_char", [])                # list of dicts: char, conf, margin, reason, position
        paddle_raw = job.get("paddle_raw", [])
        paddle_enh = job.get("paddle_enhanced", [])
        if line is None:
            return 0
        gray = np.asarray(line).astype(np.float32)
        if gray.ndim == 3:
            gray = gray.mean(axis=2)
        n = max(1, len(per_char) or (len(expected_vin) or 17))
        boxes = _adaptive_boxes(gray, n)
        stamp = datetime.now().strftime("%Y-%m-%d")
        sess_dir = os.path.join(self.root, stamp, f"SESSION_{session_id}")
        src_dir = os.path.join(sess_dir, "source"); chars_dir = os.path.join(sess_dir, "chars")
        os.makedirs(src_dir, exist_ok=True); os.makedirs(chars_dir, exist_ok=True)
        # save source line + frame (atomic-ish: write temp then rename)
        line_path = os.path.join(src_dir, "normalized_line.png")
        self._atomic_save(Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8)), line_path)
        src_hash = _sha_bytes(open(line_path, "rb").read())
        frame_path = ""
        if frame is not None:
            frame_path = os.path.join(src_dir, "frame.png")
            fa = np.asarray(frame)
            self._atomic_save(Image.fromarray(np.clip(fa, 0, 255).astype(np.uint8)), frame_path)
        meta_positions = []
        saved = 0
        for pc in per_char:
            pos = int(pc.get("position", 0))
            reasons = pc.get("reasons") or ([pc["reason"]] if pc.get("reason") else [])
            exp = expected_vin[pos] if (trusted and 0 <= pos < len(expected_vin)) else None
            mc = pc.get("char") or ""
            # trigger check
            trig = set(reasons)
            if exp and exp not in SUPPORTED: trig.add("UNSUPPORTED_EXPECTED_CLASS")
            if exp and exp in self.targets: trig.add("TARGET_CHAR")
            if not trig:
                continue
            box = boxes[pos] if 0 <= pos < len(boxes) else (0, 0, gray.shape[1], gray.shape[0])
            x, y, w, h = box
            crop = np.clip(gray[y:y+h, x:x+w], 0, 255).astype(np.uint8)
            label_part = exp if exp else "UNKNOWN"
            crop_path = os.path.join(chars_dir, f"pos_{pos+1:02d}_expected_{label_part}.png")
            self._atomic_save(Image.fromarray(crop), crop_path)
            crop_hash = _sha_bytes(open(crop_path, "rb").read())
            model_char = mc if mc else "UNKNOWN_CHAR"
            if exp and exp not in SUPPORTED:
                model_char = "UNKNOWN_CHAR"     # never rename an unsupported (V/G) as a known class
            label_status = "TRUSTED_LABEL" if exp else "UNLABELED"
            item = {
                "collection_id": uuid.uuid4().hex, "session_id": session_id,
                "expected_vin": expected_vin if trusted else "", "expected_character": exp,
                "character_position": pos + 1, "model_character": model_char,
                "model_confidence": float(pc.get("conf", 0.0)),
                "paddle_raw_character": (paddle_raw[pos] if pos < len(paddle_raw) else None),
                "paddle_enhanced_character": (paddle_enh[pos] if pos < len(paddle_enh) else None),
                "collection_reason": ";".join(sorted(trig)),
                "source_frame_path": frame_path, "normalized_line_path": line_path,
                "character_crop_path": crop_path, "crop_box_json": json.dumps({"x":x,"y":y,"w":w,"h":h}),
                "source_hash": src_hash, "crop_hash": crop_hash,
                "label_status": label_status, "review_status": "PENDING",
                "model_version": self.model_version,
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            is_new = True
            if self.store_fn is not None:
                try:
                    is_new = self.store_fn(item)      # dedup enforced by DB unique key
                except Exception:
                    self.errors += 1; is_new = True
            if is_new:
                saved += 1; self.collected += 1
                meta_positions.append({"position": pos+1, "expected": exp, "model": model_char,
                                       "reasons": sorted(trig), "crop": os.path.basename(crop_path)})
        json.dump({"session_id": session_id, "expected_vin": expected_vin if trusted else None,
                   "trusted": trusted, "model_version": self.model_version,
                   "positions": meta_positions,
                   "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")},
                  open(os.path.join(sess_dir, "metadata.json"), "w"), indent=2)
        return saved

    @staticmethod
    def _atomic_save(img, path):
        tmp = path + ".tmp"
        img.save(tmp, format="PNG")     # explicit format: tmp name has no image extension
        os.replace(tmp, path)
