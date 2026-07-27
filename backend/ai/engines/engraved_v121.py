"""
engraved_v121.py — Engraved OCR v1.2.1 recognizer (real, trained model).

Loads the trained MobileNetV3-Small character classifier (ONNX) + metadata, runs
adaptive character segmentation, classifies each glyph, and applies a per-character
UNKNOWN/REJECT confidence gate. It NEVER invents or substitutes a character to complete
a sequence: a gated-out position is reported UNKNOWN, not guessed.

Conforms to backend.ai.ocr_contract.OCRRecognizer. Runtime validation refuses to become
ready if the model output dimension / charset length / files / checksum do not match
metadata. G and V are structurally impossible outputs (not in the 21-class charset).
"""
from __future__ import annotations
import json, os, time, hashlib
from typing import List, Optional
import numpy as np
from .. import ocr_contract as C

UNKNOWN_CHAR = "?"


def _canonical(model_dir: str):
    return {k: os.path.join(model_dir, v) for k, v in {
        "onnx": "model.onnx", "meta": "model_metadata.json",
        "charset": "charset.txt", "thresholds": "confidence_thresholds.json",
        "checksum": "checksum.sha256"}.items()}


class EngravedV121Recognizer(C.OCRRecognizer):
    engine_id = "ENGRAVED_V121"

    def __init__(self, model_dir: str, expected_length: int = 17,
                 default_threshold: float = 0.90) -> None:
        self.model_dir = model_dir
        self.expected_length = expected_length
        self.default_threshold = default_threshold
        self._sess = None
        self._meta = {}
        self._charset = ""
        self._thresholds = {}
        self._ready = False
        self._last_error = None
        self._img = 64

    # ---- lifecycle ----
    def initialize(self) -> None:
        p = _canonical(self.model_dir)
        try:
            for k in ("onnx", "meta", "charset"):
                if not os.path.isfile(p[k]):
                    raise FileNotFoundError(p[k])
            self._meta = json.load(open(p["meta"], encoding="utf-8"))
            self._charset = open(p["charset"], encoding="utf-8").read().strip()
            self._img = int(self._meta.get("input_size", 64))
            # runtime contract validation (spec sec.14)
            if self._meta.get("charset_len") != len(self._charset):
                raise ValueError(f"charset_len {self._meta.get('charset_len')} != {len(self._charset)}")
            if self._meta.get("output_dimension") != len(self._charset) + 1:
                raise ValueError(f"output_dimension {self._meta.get('output_dimension')} != charset+reject")
            # checksum
            want = {}
            if os.path.isfile(p["checksum"]):
                for line in open(p["checksum"]):
                    parts = line.split()
                    if len(parts) == 2:
                        want[parts[0]] = parts[1]
            got = hashlib.sha256(open(p["onnx"], "rb").read()).hexdigest()
            if want.get("model.onnx") and want["model.onnx"] != got:
                raise ValueError("model.onnx checksum mismatch")
            if os.path.isfile(p["thresholds"]):
                self._thresholds = json.load(open(p["thresholds"], encoding="utf-8"))
            import onnxruntime as ort
            self._sess = ort.InferenceSession(p["onnx"], providers=["CPUExecutionProvider"])
            self._ready = True
            self._last_error = None
        except Exception as exc:
            self._ready = False
            self._last_error = f"{type(exc).__name__}: {exc}"

    # ---- segmentation (adaptive; NOT equal-width) ----
    def _to_gray(self, img):
        a = np.asarray(img)
        if a.ndim == 3:
            a = (0.114*a[..., 0] + 0.587*a[..., 1] + 0.299*a[..., 2])
        return a.astype(np.float32)

    def _segment(self, gray: np.ndarray, n: int):
        col = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1])).sum(axis=0)
        col += np.abs(gray - np.median(gray, axis=0, keepdims=True)).sum(axis=0)
        k = max(3, gray.shape[1] // 120)
        col = np.convolve(col, np.ones(k)/k, mode="same")
        thr = 0.12 * col.max() if col.max() > 0 else 0
        act = np.where(col > thr)[0]
        x0, x1 = (int(act[0]), int(act[-1]) + 1) if len(act) >= n else (0, len(col))
        span = max(1, x1 - x0); w = span / n
        cuts = [x0]
        for i in range(1, n):
            init = int(x0 + i*w); lo = max(x0+1, int(init-0.45*w)); hi = min(x1-1, int(init+0.45*w))
            cuts.append(lo + int(np.argmin(col[lo:hi])) if hi > lo else init)
        cuts.append(x1)
        boxes = []
        H = gray.shape[0]
        for i in range(n):
            cx0, cx1 = cuts[i], cuts[i+1]
            boxes.append((cx0, 0, max(1, cx1-cx0), H))
        return boxes

    def _prep(self, gray, box):
        x, y, w, h = box
        crop = gray[y:y+h, x:x+w]
        if crop.size == 0:
            crop = np.zeros((self._img, self._img), np.float32)
        from PIL import Image
        im = Image.fromarray(np.clip(crop, 0, 255).astype(np.uint8))
        ww, hh = im.size; s = self._img/max(ww, hh)
        nw, nh = max(1, int(round(ww*s))), max(1, int(round(hh*s)))
        im = im.resize((nw, nh))
        canvas = Image.new("L", (self._img, self._img), 0)
        canvas.paste(im.convert("L"), ((self._img-nw)//2, (self._img-nh)//2))
        arr = (np.asarray(canvas, np.float32)/255.0 - 0.5)/0.5
        return np.repeat(arr[None, None], 3, axis=1)   # 1x3xHxW

    def _classify(self, batch):
        logits = self._sess.run(None, {"input": batch})[0]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        probs = e / e.sum(axis=1, keepdims=True)
        return probs

    # ---- recognize ----
    def recognize(self, request: C.OCRRecognitionRequest) -> C.OCRRecognitionResult:
        t0 = time.perf_counter()
        if not self._ready:
            return C.make_result(request, engine_id=self.engine_id, engine_version="1.2.1",
                                 engine_status=C.ENGINE_UNAVAILABLE, charset_id="engraved_v121_21",
                                 warnings=[f"engine not ready: {self._last_error}"],
                                 failure_type=C.ENGINE_UNAVAILABLE)
        crops = [c.image for c in request.crops if getattr(c, "image", None) is not None]
        if not crops:
            return C.make_result(request, engine_id=self.engine_id, engine_version="1.2.1",
                                 engine_status=C.EMPTY, charset_id="engraved_v121_21", warnings=["no crops"])
        gray = self._to_gray(max(crops, key=lambda a: np.asarray(a).size))
        n = self.expected_length
        boxes = self._segment(gray, n)
        batch = np.concatenate([self._prep(gray, b) for b in boxes], axis=0).astype(np.float32)
        probs = self._classify(batch)
        reject_idx = len(self._charset)
        char_preds: List[C.OCRCharacterPrediction] = []
        raw = []; gated = []; warnings = []
        for i in range(n):
            order = np.argsort(probs[i])[::-1]
            top1 = int(order[0]); conf = float(probs[i][top1]); top2 = float(probs[i][order[1]])
            margin = conf - top2
            ch = UNKNOWN_CHAR if top1 == reject_idx else self._charset[top1]
            raw.append(ch if ch != UNKNOWN_CHAR else UNKNOWN_CHAR)
            thr = float(self._thresholds.get(ch, self.default_threshold)) if ch != UNKNOWN_CHAR else 1.1
            if top1 == reject_idx:
                gated.append(UNKNOWN_CHAR); warnings.append(f"pos{i+1}:REJECT")
            elif conf < thr or margin < 0.10:
                gated.append(UNKNOWN_CHAR); warnings.append(f"pos{i+1}:low_conf({conf:.2f}/m{margin:.2f})")
            else:
                gated.append(ch)
            alts = [(self._charset[int(j)] if int(j) != reject_idx else "REJECT", float(probs[i][int(j)]))
                    for j in order[:3]]
            char_preds.append(C.OCRCharacterPrediction(position=i, char=(ch if ch != UNKNOWN_CHAR else ""),
                                                       confidence=conf, alternatives=alts))
        raw_seq = "".join(raw); gated_seq = "".join(gated)
        status = C.OK if UNKNOWN_CHAR not in gated_seq else C.EMPTY
        seq_conf = float(np.mean([cp.confidence for cp in char_preds])) if char_preds else 0.0
        return C.make_result(
            request, engine_id=self.engine_id, engine_version="1.2.1",
            engine_status=status, charset_id="engraved_v121_21",
            engine_model_id="engraved_ocr_v1.2.1",
            engine_model_version=str(self._meta.get("model_version", "1.2.1")),
            raw_sequence=raw_seq,
            normalized_sequence=(gated_seq if status == C.OK else ""),
            sequence_confidence=seq_conf, char_predictions=char_preds,
            crop_evidence=[C.CropEvidenceRef(crop_index=0, variant_name="engraved_adaptive",
                                             evidence_group=0, ocr_conf=seq_conf, raw_text=raw_seq)],
            latency_ms=(time.perf_counter()-t0)*1000.0,
            warnings=warnings,
            failure_type=None if status == C.OK else "ENGRAVED_LOW_CONFIDENCE_OR_UNKNOWN")

    def health(self) -> C.EngineHealth:
        return C.EngineHealth(engine_id=self.engine_id, ready=self._ready,
                              status="ready" if self._ready else "not_ready",
                              detail=f"model_dir={self.model_dir}", last_error=self._last_error,
                              last_check_at=time.time())

    def metadata(self) -> C.EngineMetadata:
        return C.EngineMetadata(engine_id=self.engine_id, engine_version="1.2.1",
                                engine_model_id="engraved_ocr_v1.2.1",
                                engine_model_version=str(self._meta.get("model_version", "1.2.1")),
                                charset_id="engraved_v121_21", supports_per_char_confidence=True,
                                supports_alternatives=True, max_batch_size=1, device="cpu")

    def close(self) -> None:
        self._sess = None; self._ready = False
