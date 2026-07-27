"""
============================================================
ocr_variants.py  —  OCR preprocessing variantlari (task diversitysi)
============================================================
Maqsad (Section 3): YOLO bergan har bir VIN crop uchun BIR NECHTA nomli
preprocessing varianti yaratish. Har variant ALOHIDA OCR taski sifatida
ishlaydi va natijalari position-level consensus (vin_fusion.py) bilan
birlashtiriladi.

MUHIM prinsip: filtrlar OCR engine ICHIGA hardcode QILINMAYDI — ular
tashqi, nomli variantlar. Shu sabab har OCR natijasi qaysi variantdan
kelganini (variant_name) audit qilib bo'ladi.

Variantlar (embossed/engraved metall VIN uchun):
  * raw_resized      — asl crop, balandligi normallashtirilgan (96/128 px).
  * clahe_unsharp    — grayscale -> CLAHE -> unsharp mask -> BGR.
  * blackhat_relief  — grayscale -> morfologik blackhat -> normalize -> CLAHE.
  * tophat_relief    — grayscale -> morfologik tophat -> normalize -> CLAHE.
  * adaptive_binary  — grayscale -> CLAHE -> adaptiv threshold -> kichik morfologiya.
  * inverted_clahe   — teskari (invert) CLAHE varianti (chuqur o'yilgan belgilar).
  + burilish/deskew  — 0, -3, +3, -6, +6 gradus (variant_rotations).

Bu modul FAQAT OpenCV/Numpy ishlatadi (backend.config ni import QILMAYDI) —
shuning uchun mustaqil test qilinadi va parametrlar tashqaridan beriladi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

import cv2
import numpy as np

# Har filtr uchun bazaviy ishonch og'irligi (variant_weight). Ishonchli
# (kam artefaktli) filtrlar yuqoriroq. adaptive_binary eng "shovqinli" ->
# past og'irlik (u faqat qo'shimcha ovoz sifatida hisobga olinadi).
BASE_WEIGHTS = {
    "raw_resized": 1.00,
    "clahe_unsharp": 1.00,
    "blackhat_relief": 0.90,
    "tophat_relief": 0.90,
    "inverted_clahe": 0.80,
    "adaptive_binary": 0.70,
}

# Burilish og'irlik jazosi: 0° eng ishonchli, burchak oshgan sari kamayadi.
def _rotation_weight(deg: float) -> float:
    a = abs(float(deg))
    if a < 0.5:
        return 1.0
    if a <= 3.5:
        return 0.90
    if a <= 6.5:
        return 0.80
    return 0.70


@dataclass
class Variant:
    """Bitta OCR taski: nomli, tayyorlangan rasm + ishonch og'irligi."""
    variant_name: str        # mas. "clahe_unsharp" yoki "clahe_unsharp@+3"
    image: np.ndarray        # BGR (PaddleOCR 3-kanal kutadi)
    weight: float            # variant_weight (0..1) — consensus og'irligi
    rotation: float = 0.0    # qo'llangan burilish (gradus, audit uchun)


# ------------------------------------------------------------------
# Past darajali yordamchilar
# ------------------------------------------------------------------
def _to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _to_bgr(gray: np.ndarray) -> np.ndarray:
    if gray.ndim == 3:
        return gray
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def resize_to_height(img: np.ndarray, height: int) -> np.ndarray:
    """Rasmni berilgan balandlikka keltiradi (kichik etched belgilar uchun)."""
    if not height or img.shape[0] <= 0:
        return img
    h = img.shape[0]
    if h == height:
        return img
    scale = float(height) / float(h)
    interp = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=interp)


def _clahe(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    c = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(int(grid), int(grid)))
    return c.apply(gray)


def _normalize(gray: np.ndarray) -> np.ndarray:
    return cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _kernel(h: int, ksize: int = 0) -> np.ndarray:
    """Morfologiya yadrosi — rasm balandligiga moslashtiriladi (belgi qalinligi)."""
    if ksize <= 0:
        ksize = max(3, int(round(h / 8.0)) | 1)   # toq son
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))


# ------------------------------------------------------------------
# Filtr generatorlari (grayscale kirish -> tayyor grayscale chiqish)
# ------------------------------------------------------------------
def f_raw_resized(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    """Asl (filtrlanmagan) — faqat balandlik normallashtirilgan."""
    return gray


def f_clahe_unsharp(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    g = _clahe(gray, clip, grid)
    blur = cv2.GaussianBlur(g, (0, 0), 1.0)
    return cv2.addWeighted(g, 1.6, blur, -0.6, 0)


def f_blackhat_relief(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    """Blackhat: yorug' fonda QORONG'I belgilarni ajratadi (bosilgan/o'yilgan)."""
    k = _kernel(gray.shape[0])
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    return _clahe(_normalize(bh), clip, grid)


def f_tophat_relief(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    """Tophat: qorong'i fonda YORUG' belgilarni ajratadi (ko'tarilgan relief)."""
    k = _kernel(gray.shape[0])
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k)
    return _clahe(_normalize(th), clip, grid)


def f_adaptive_binary(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    g = _clahe(gray, clip, grid)
    bs = max(11, (int(round(gray.shape[0] / 3.0)) | 1))   # toq blok o'lchami
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, bs, 8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, k)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)
    return bw


def f_inverted_clahe(gray: np.ndarray, clip: float, grid: int) -> np.ndarray:
    """Teskari CLAHE — belgilar fondan qorong'iroq bo'lgan holatlar uchun."""
    return _clahe(cv2.bitwise_not(gray), clip, grid)


_FILTERS = {
    "raw_resized": f_raw_resized,
    "clahe_unsharp": f_clahe_unsharp,
    "blackhat_relief": f_blackhat_relief,
    "tophat_relief": f_tophat_relief,
    "adaptive_binary": f_adaptive_binary,
    "inverted_clahe": f_inverted_clahe,
}


def _rotate(img: np.ndarray, deg: float) -> np.ndarray:
    if abs(deg) < 0.05:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _deskew_angle(gray: np.ndarray, max_angle: float = 20.0) -> float:
    """minAreaRect asosida matn qiyaligini baholaydi (deskew varianti uchun)."""
    try:
        thr = cv2.threshold(gray, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        coords = np.column_stack(np.where(thr > 0))
        if coords.shape[0] < 50:
            return 0.0
        ang = cv2.minAreaRect(coords.astype(np.float32))[-1]
        if ang < -45:
            ang = 90.0 + ang
        elif ang > 45:
            ang = ang - 90.0
        if abs(ang) < 0.5 or abs(ang) > max_angle:
            return 0.0
        return float(ang)
    except Exception:
        return 0.0


# ------------------------------------------------------------------
# Asosiy: bitta crop uchun variant ro'yxati (prioritetlangan)
# ------------------------------------------------------------------
def build_variants(
    crop_bgr: np.ndarray,
    *,
    variant_names: Sequence[str] = tuple(BASE_WEIGHTS.keys()),
    rotations: Sequence[float] = (0.0, -3.0, 3.0, -6.0, 6.0),
    deskew: bool = True,
    upscale_height: int = 96,
    clahe_clip: float = 3.0,
    clahe_grid: int = 8,
    max_variants: int = 0,
) -> List[Variant]:
    """
    Bitta crop uchun nomli OCR variantlarini yaratadi (PRIORITETLANGAN tartibda).

    Tartib strategiyasi (diversity + arzonlik):
      1. Har filtr @ 0° (deskew qilingan bazaviy) — turli filtrlar eng ko'p
         mustaqil ma'lumot beradi.
      2. So'ng eng ishonchli filtrlarning burilish variantlari (±3, ±6, deskew).
    max_variants > 0 bo'lsa ro'yxat shu songa qisqartiriladi.

    Qaytadi: Variant ro'yxati (variant_name, BGR image, weight, rotation).
    """
    gray0 = _to_gray(crop_bgr)
    gray0 = resize_to_height(gray0, upscale_height)

    # Deskew: kichik qiyalikni to'g'rilab, keyin barcha filtrlarga asos qilamiz.
    base = gray0
    desk = 0.0
    if deskew:
        desk = _deskew_angle(gray0)
        if abs(desk) > 0.5:
            base = _rotate(gray0, desk)

    names = [n for n in variant_names if n in _FILTERS]
    if not names:
        names = list(_FILTERS.keys())

    out: List[Variant] = []

    # --- 1-bosqich: har filtr, deskew qilingan bazaviy (0° effektiv) ---
    for name in names:
        try:
            g = _FILTERS[name](base, clahe_clip, clahe_grid)
        except Exception:
            continue
        out.append(Variant(variant_name=name, image=_to_bgr(g),
                            weight=BASE_WEIGHTS.get(name, 0.7), rotation=desk))

    # --- 2-bosqich: burilish variantlari (eng ishonchli 2 filtrga) ---
    rot_filters = [n for n in ("clahe_unsharp", "raw_resized", "blackhat_relief")
                   if n in names]
    for deg in rotations:
        if abs(deg) < 0.5:
            continue                       # 0° allaqachon 1-bosqichda
        rimg = _rotate(base, deg)
        for name in rot_filters:
            try:
                g = _FILTERS[name](rimg, clahe_clip, clahe_grid)
            except Exception:
                continue
            w = BASE_WEIGHTS.get(name, 0.7) * _rotation_weight(deg)
            sign = "+" if deg >= 0 else ""
            out.append(Variant(variant_name=f"{name}@{sign}{deg:g}",
                                image=_to_bgr(g), weight=w, rotation=float(deg)))

    if max_variants and len(out) > max_variants:
        out = out[:max_variants]
    return out


def interleave_variants(per_crop: Sequence[Sequence[Variant]],
                        max_total: int) -> List[Tuple[int, Variant]]:
    """
    Bir nechta cropning variant ro'yxatlarini ARALASHTIRIB (round-robin) yagona
    prioritetlangan ro'yxatga aylantiradi va max_total gacha qisqartiradi.

    Round-robin muhim: agar bitta crop ko'p variant bersa ham, har crop dan
    kamida bittadan variant OCR ga kirishi ta'minlanadi (ko'p-crop consensusi).

    Qaytadi: [(crop_index, Variant), ...] — jami <= max_total.
    """
    out: List[Tuple[int, Variant]] = []
    if not per_crop:
        return out
    depth = max(len(v) for v in per_crop)
    for d in range(depth):
        for ci, variants in enumerate(per_crop):
            if d < len(variants):
                out.append((ci, variants[d]))
                if max_total and len(out) >= max_total:
                    return out
    return out
