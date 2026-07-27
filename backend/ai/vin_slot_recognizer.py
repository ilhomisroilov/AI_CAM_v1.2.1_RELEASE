"""
VIN slot recognizer used by the production OCR worker.

The model is intentionally small and specialized: every VIN crop is decoded as
exactly 17 character slots. PaddleOCR remains the primary OCR engine; this
module gives OCRWorker a local fallback/assistant when Paddle returns NO_READ
or an ambiguous result.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import cv2
import numpy as np
import torch
import torch.nn as nn

from ..config import BASE_DIR
from . import vin_rules


ALPHABET = "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"
SLOT_TO_CHAR = {i: c for i, c in enumerate(ALPHABET)}
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


@dataclass(frozen=True)
class SlotVinPrediction:
    vin: str
    avg_confidence: float
    min_confidence: float
    char_confidences: Sequence[float]
    valid_vin: bool
    model: Optional[str]
    device: str


class VinSlotCNN(nn.Module):
    def __init__(self, vocab_size: int) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),
            nn.Conv2d(32, 64, 3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),
            nn.Conv2d(64, 128, 3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d((2, 1), (2, 1)),
            nn.Conv2d(128, 256, 3, padding=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, 3, padding=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
        )
        self.pool = nn.AdaptiveAvgPool2d((1, 17))
        self.classifier = nn.Sequential(
            nn.Dropout(0.15),
            nn.Conv1d(256, 256, 1),
            nn.ReLU(inplace=True),
            nn.Dropout(0.10),
            nn.Conv1d(256, vocab_size, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.features(x)
        y = self.pool(y).squeeze(2)
        y = self.classifier(y)
        return y.permute(0, 2, 1)


def _resolve_path(path_value: str) -> Path:
    p = Path(path_value or "")
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


def _select_device(value: str) -> torch.device:
    requested = (value or "auto").strip().lower()
    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if requested.startswith("cuda") and not torch.cuda.is_available():
        return torch.device("cpu")
    return torch.device(requested)


def _preprocess_crop(crop_bgr: np.ndarray, height: int, width: int) -> torch.Tensor:
    if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
        raise ValueError("empty VIN crop")

    if crop_bgr.ndim == 2:
        img = crop_bgr
    elif crop_bgr.ndim == 3 and crop_bgr.shape[2] == 4:
        img = cv2.cvtColor(crop_bgr, cv2.COLOR_BGRA2GRAY)
    elif crop_bgr.ndim == 3:
        img = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError(f"unsupported crop shape: {crop_bgr.shape}")

    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError(f"bad crop shape: {img.shape}")

    scale = height / float(h)
    new_w = max(1, int(round(w * scale)))
    if new_w > width:
        scale = width / float(w)
        new_w = width
        new_h = max(1, int(round(h * scale)))
    else:
        new_h = height

    interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
    resized = cv2.resize(img, (new_w, new_h), interpolation=interpolation)

    canvas = np.full((height, width), 0, dtype=np.uint8)
    y = max(0, (height - resized.shape[0]) // 2)
    canvas[y:y + resized.shape[0], :resized.shape[1]] = resized[:, :width]

    arr = canvas.astype(np.float32) / 255.0
    arr = (arr - 0.5) / 0.5
    return torch.from_numpy(arr).unsqueeze(0).unsqueeze(0)


class VinSlotRecognizer:
    def __init__(self, model_path: str, device: str = "auto") -> None:
        self.model_path = _resolve_path(model_path)
        self.device = _select_device(device)
        self.height = 64
        self.width = 340
        self.ready = False
        self._model: Optional[VinSlotCNN] = None
        self._lock = threading.Lock()
        self._load()

    @classmethod
    def from_config(cls, cfg) -> "VinSlotRecognizer":
        return cls(
            model_path=getattr(cfg, "model_path", ""),
            device=getattr(cfg, "device", "auto"),
        )

    def _load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(str(self.model_path))
        ckpt = torch.load(self.model_path, map_location=self.device, weights_only=False)
        alphabet = ckpt.get("alphabet") or ALPHABET
        if alphabet != ALPHABET:
            raise RuntimeError(f"unsupported VIN alphabet in checkpoint: {alphabet!r}")
        self.height = int(ckpt.get("height", self.height))
        self.width = int(ckpt.get("width", self.width))
        model = VinSlotCNN(vocab_size=len(ALPHABET)).to(self.device)
        model.load_state_dict(ckpt["model_state"])
        model.eval()
        self._model = model
        self.ready = True

    @torch.inference_mode()
    def predict(self, crop_bgr: np.ndarray) -> SlotVinPrediction:
        if not self.ready or self._model is None:
            raise RuntimeError("VIN slot recognizer is not ready")

        x = _preprocess_crop(crop_bgr, self.height, self.width).to(self.device)
        with self._lock:
            logits = self._model(x)
            probs = torch.softmax(logits, dim=2)[0]
        confs, idxs = probs.max(dim=1)
        idx_list = idxs.detach().cpu().tolist()
        conf_list = [float(v) for v in confs.detach().cpu().tolist()]
        vin = "".join(SLOT_TO_CHAR[int(i)] for i in idx_list)
        return SlotVinPrediction(
            vin=vin,
            avg_confidence=float(sum(conf_list) / max(1, len(conf_list))),
            min_confidence=float(min(conf_list) if conf_list else 0.0),
            char_confidences=conf_list,
            valid_vin=bool(VIN_RE.fullmatch(vin)),
            model=vin_rules.model_from_vin(vin),
            device=str(self.device),
        )

