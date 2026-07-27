"""
============================================================
pos5_verifier.py  —  Optional VISUAL verifier for VIN position 5 (Section 8)
============================================================
Ikkinchi bosqich himoya: VIN position 5 (C/D/B/G/H/J) eng xavfli belgi. Bu modul
VIN matn bandini 17 katakka bo'lib, POZITSIYA-5 katagini alohida MODEL bilan
tekshiradi (HOG+SVM yoki kichik CNN). Verifier ishonchi past bo'lsa VIN
OCR_AMBIGUOUS deb rad etiladi.

MUHIM: model O'QITILMAGUNCHA bu verifier O'CHIQ (POS5_VERIFIER.enabled=false) va
`ready=False` — u FAIL-OPEN ishlaydi (ya'ni oqimga ta'sir qilmaydi). Modelni
tools/train_pos5_verifier.py bilan o'qitib, config'да enabled=true qiling.

Bog'liqliklar: cv2 + numpy (majburiy). sklearn/joblib (HOG+SVM uchun, ixtiyoriy),
onnxruntime yoki torch (CNN uchun, ixtiyoriy). Ular yo'q bo'lsa -> ready=False.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import cv2
import numpy as np

# pos5 da ruxsat etilgan belgilar (spec) — verifier faqat shularni ajratadi.
POS5_CLASSES = ("B", "C", "D", "G", "H", "J")
_HOG_SIZE = (32, 48)          # (kenglik, balandlik) — katak normal o'lchami


def split_char_cells(band_bgr: np.ndarray, n: int = 17,
                     margin_frac: float = 0.04) -> List[np.ndarray]:
    """
    VIN matn bandini (tight crop) n ta teng katakka bo'ladi (chapdan-o'ngga).
    Har katak atrofида kichik margin qoldiriladi. Qaytadi: n ta BGR katak.
    """
    if band_bgr is None or band_bgr.size == 0:
        return []
    h, w = band_bgr.shape[:2]
    cell_w = w / float(n)
    mx = int(cell_w * margin_frac)
    cells: List[np.ndarray] = []
    for i in range(n):
        x0 = max(0, int(round(i * cell_w)) - mx)
        x1 = min(w, int(round((i + 1) * cell_w)) + mx)
        cells.append(band_bgr[:, x0:x1])
    return cells


def pos5_cell(band_bgr: np.ndarray) -> Optional[np.ndarray]:
    """VIN bandidan FAQAT position-5 (index 4) katagini qaytaradi."""
    cells = split_char_cells(band_bgr, 17)
    return cells[4] if len(cells) >= 5 else None


def _hog_features(cell_bgr: np.ndarray) -> np.ndarray:
    """Katakdan HOG xususiyatlari (o'lcham-normal). SVM kirishi uchun 1D vektor."""
    gray = cv2.cvtColor(cell_bgr, cv2.COLOR_BGR2GRAY) if cell_bgr.ndim == 3 else cell_bgr
    gray = cv2.resize(gray, _HOG_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(gray)
    hog = cv2.HOGDescriptor(_winSize=(_HOG_SIZE[0], _HOG_SIZE[1]),
                            _blockSize=(16, 16), _blockStride=(8, 8),
                            _cellSize=(8, 8), _nbins=9)
    return hog.compute(gray).reshape(-1)


class Pos5Verifier:
    """
    Position-5 vizual verifier. Model yo'q bo'lsa ready=False (fail-open).
    verify(band_crop, expected_char) -> (ok, confidence).
    """

    def __init__(self, backend: str = "hog_svm", model_path: str = "",
                 min_confidence: float = 0.60, save_dir: str = "") -> None:
        self.backend = backend
        self.model_path = model_path or ""
        self.min_confidence = float(min_confidence)
        self.save_dir = save_dir or ""
        self._model = None
        self._classes: Tuple[str, ...] = POS5_CLASSES
        self.ready = False
        self._load()

    @classmethod
    def from_config(cls, cfg) -> "Pos5Verifier":
        save_dir = getattr(cfg, "char_crops_dir", "") or ""
        return cls(
            backend=getattr(cfg, "backend", "hog_svm"),
            model_path=getattr(cfg, "model_path", "") or "",
            min_confidence=float(getattr(cfg, "min_confidence", 0.60)),
            save_dir=save_dir,
        )

    def _load(self) -> None:
        if not self.model_path or not os.path.exists(self.model_path):
            self.ready = False
            return
        try:
            if self.backend == "hog_svm":
                import joblib
                obj = joblib.load(self.model_path)
                # obj: dict(model=sklearn_clf, classes=[...]) yoki to'g'ridan clf
                if isinstance(obj, dict):
                    self._model = obj.get("model")
                    self._classes = tuple(obj.get("classes", POS5_CLASSES))
                else:
                    self._model = obj
                self.ready = self._model is not None
            elif self.backend == "cnn":
                import onnxruntime as ort
                self._model = ort.InferenceSession(self.model_path)
                self.ready = True
            else:
                self.ready = False
        except Exception:
            self._model = None
            self.ready = False

    def verify(self, band_bgr: np.ndarray,
               expected_char: Optional[str] = None) -> Tuple[bool, float]:
        """
        pos5 katagini tekshiradi. Qaytadi (ok, confidence).
        ready=False bo'lsa (model yo'q) -> (True, 1.0) FAIL-OPEN (oqimga ta'sir yo'q).
        """
        if not self.ready or self._model is None:
            return True, 1.0
        cell = pos5_cell(band_bgr)
        if cell is None or cell.size == 0:
            return True, 1.0                    # katak ajratilmadi -> fail-open
        try:
            pred_char, conf = self._predict(cell)
        except Exception:
            return True, 1.0
        if expected_char and pred_char != expected_char:
            return False, float(conf)
        return (conf >= self.min_confidence), float(conf)

    def _predict(self, cell_bgr: np.ndarray) -> Tuple[str, float]:
        if self.backend == "hog_svm":
            feat = _hog_features(cell_bgr).reshape(1, -1)
            if hasattr(self._model, "predict_proba"):
                proba = self._model.predict_proba(feat)[0]
                idx = int(np.argmax(proba))
                classes = getattr(self._model, "classes_", self._classes)
                return str(classes[idx]), float(proba[idx])
            pred = self._model.predict(feat)[0]
            return str(pred), 1.0
        # cnn (onnx)
        gray = cv2.cvtColor(cell_bgr, cv2.COLOR_BGR2GRAY) if cell_bgr.ndim == 3 else cell_bgr
        x = cv2.resize(gray, _HOG_SIZE).astype(np.float32)[None, None] / 255.0
        out = self._model.run(None, {self._model.get_inputs()[0].name: x})[0][0]
        e = np.exp(out - np.max(out)); p = e / e.sum()
        idx = int(np.argmax(p))
        return str(self._classes[idx]), float(p[idx])

    def save_cell(self, band_bgr: np.ndarray, label: str = "unknown") -> Optional[str]:
        """
        pos5 katagini training uchun diskка saqlaydi (save_char_crops=true bo'lganda).
        Qaytadi: saqlangan fayl yo'li yoki None.
        """
        if not self.save_dir:
            return None
        cell = pos5_cell(band_bgr)
        if cell is None:
            return None
        d = os.path.join(self.save_dir, label)
        os.makedirs(d, exist_ok=True)
        import time
        fp = os.path.join(d, f"pos5_{int(time.time()*1000)}.png")
        try:
            cv2.imwrite(fp, cell)
            return fp
        except Exception:
            return None
