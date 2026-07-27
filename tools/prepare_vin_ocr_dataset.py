"""
Prepare a review-first VIN OCR dataset from saved crop images.

This script does not claim labels are ground truth. It creates AI/OCR
suggestions, quality metrics, review CSV files, contact sheets, and pending
training manifests. Rows become training-safe only after a human sets
verification_status=verified.

Example:
  .\\.venv\\Scripts\\python.exe tools\\prepare_vin_ocr_dataset.py ^
      --input data\\crops --out data\\vin_ocr_dataset --run-ocr

After review:
  .\\.venv\\Scripts\\python.exe tools\\prepare_vin_ocr_dataset.py ^
      --finalize-review data\\vin_ocr_dataset\\labels\\labels_review.csv ^
      --out data\\vin_ocr_dataset
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
VIN_STRICT_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
VIN_LOOSE_RE = re.compile(r"[A-Z0-9]{17}")
SESSION_RE = re.compile(r"sess-(\d+)", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"(20\d{6}_\d{6}(?:_\d{3})?)")


@dataclass
class OcrPrediction:
    status: str = "NOT_RUN"
    suggested_vin: str = ""
    raw_best: str = ""
    score: float = 0.0
    model: str = ""
    n_reads: int = 0
    crashes: int = 0
    reasons: str = ""


def norm_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def valid_vin(value: str) -> bool:
    return bool(VIN_STRICT_RE.fullmatch(norm_text(value)))


def model_from_vin(value: str) -> str:
    v = norm_text(value)
    if v.startswith("NSTF"):
        return "QY"
    if v.startswith("NSTH"):
        return "BL7M"
    return ""


def list_images(root: Path) -> List[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_filename_vin(path: Path) -> str:
    # Keep separator boundaries. If we normalize the whole stem first, timestamps
    # can join with the VIN and create false 17-character matches.
    tokens = re.findall(r"[A-Z0-9]+", path.stem.upper())
    strict: List[str] = []
    loose: List[str] = []
    for token in tokens:
        strict.extend(VIN_STRICT_RE.findall(token))
        loose.extend(VIN_LOOSE_RE.findall(token))
    if strict:
        return strict[-1]
    return loose[-1] if loose else ""


def extract_session(path: Path) -> str:
    m = SESSION_RE.search(path.stem)
    return m.group(1) if m else ""


def extract_timestamp(path: Path) -> str:
    m = TIMESTAMP_RE.search(path.stem)
    return m.group(1) if m else ""


def image_quality(path: Path) -> dict:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return {
            "width": 0, "height": 0, "brightness": 0.0, "contrast": 0.0,
            "blur_laplacian": 0.0, "dark_ratio": 0.0, "bright_ratio": 0.0,
            "saturated_ratio": 0.0, "lighting": "unreadable",
            "quality": "reject",
        }

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    dark_ratio = float(np.mean(gray < 40))
    bright_ratio = float(np.mean(gray > 215))
    saturated_ratio = float(np.mean(gray > 248))

    if saturated_ratio > 0.025 and bright_ratio > 0.12:
        lighting = "glare"
    elif brightness < 65:
        lighting = "dark"
    elif brightness > 190:
        lighting = "bright"
    elif contrast < 22:
        lighting = "low_contrast"
    else:
        lighting = "normal"

    if h < 18 or w < 80 or blur < 12 or contrast < 8:
        quality = "reject"
    elif blur < 35 or contrast < 16 or dark_ratio > 0.55 or saturated_ratio > 0.08:
        quality = "poor"
    elif blur < 80 or contrast < 25 or lighting in {"glare", "dark", "bright", "low_contrast"}:
        quality = "medium"
    else:
        quality = "good"

    return {
        "width": int(w),
        "height": int(h),
        "brightness": round(brightness, 2),
        "contrast": round(contrast, 2),
        "blur_laplacian": round(blur, 2),
        "dark_ratio": round(dark_ratio, 4),
        "bright_ratio": round(bright_ratio, 4),
        "saturated_ratio": round(saturated_ratio, 4),
        "lighting": lighting,
        "quality": quality,
    }


def split_for_group(group_key: str) -> str:
    digest = hashlib.sha1(group_key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    if bucket < 70:
        return "train"
    if bucket < 85:
        return "val"
    return "test"


def ensure_dirs(out_dir: Path) -> dict:
    dirs = {
        "images": out_dir / "images",
        "labels": out_dir / "labels",
        "reports": out_dir / "reports",
        "sheets": out_dir / "reports" / "review_sheets",
        "splits": out_dir / "splits",
        "paddle": out_dir / "paddle_rec",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


class ProductionOcrRunner:
    def __init__(self, preset: str = "balanced") -> None:
        from backend.ai.ocr_worker import OCRWorker
        from backend.config import OCR

        self.OCR = OCR
        self._old_values = {}
        self._apply_preset(preset)
        self.worker = OCRWorker(on_result=lambda *args: None, on_fail=lambda *args: None)
        if not self.worker._ensure_pool():
            raise RuntimeError("OCR process pool could not be created")
        self.worker.preload()

    def _set_ocr_value(self, name: str, value) -> None:
        if name not in self._old_values:
            self._old_values[name] = getattr(self.OCR, name, None)
        setattr(self.OCR, name, value)

    def _apply_preset(self, preset: str) -> None:
        preset = (preset or "balanced").lower()
        if preset == "full":
            return
        if preset == "fast":
            self._set_ocr_value("parallel_engines", 4)
            self._set_ocr_value("paddle_det", True)
            self._set_ocr_value("det_fallback_enabled", False)
            self._set_ocr_value("max_ocr_attempts", 2)
            self._set_ocr_value("variants_enabled", ("raw_resized", "clahe_unsharp"))
            self._set_ocr_value("variant_rotations", (0.0,))
            self._set_ocr_value("variant_deskew", False)
            return
        if preset == "balanced":
            self._set_ocr_value("parallel_engines", 4)
            self._set_ocr_value("paddle_det", True)
            self._set_ocr_value("det_fallback_enabled", False)
            self._set_ocr_value("max_ocr_attempts", 3)
            self._set_ocr_value("variants_enabled", ("raw_resized", "clahe_unsharp", "blackhat_relief"))
            self._set_ocr_value("variant_rotations", (0.0,))
            self._set_ocr_value("variant_deskew", False)
            return
        raise ValueError(f"Unknown OCR preset: {preset}")

    def close(self) -> None:
        self.worker.shutdown()
        for name, value in self._old_values.items():
            setattr(self.OCR, name, value)

    def predict(self, img: np.ndarray) -> OcrPrediction:
        from backend.ai.vin_fusion import fuse

        tasks, crop_quality = self.worker._build_tasks([img])
        det_primary = bool(getattr(self.OCR, "paddle_det", False))
        reads, crashes = self.worker._read_pool(tasks, crop_quality, det=det_primary)

        if (not det_primary and bool(getattr(self.OCR, "det_fallback_enabled", True))
                and len({r.normalized_raw for r in reads if len(r.normalized_raw) == 17}) < 2):
            det_tasks = [
                (ci, v) for (ci, v) in tasks
                if v.variant_name in ("raw_resized", "clahe_unsharp")
            ][:1]
            if det_tasks:
                extra_reads, extra_crashes = self.worker._read_pool(det_tasks, crop_quality, det=True)
                reads.extend(extra_reads)
                crashes += extra_crashes

        fr = fuse(reads, **self.worker._fuse_params())
        raw_counter = Counter(
            r.normalized_raw for r in reads
            if r.normalized_raw and len(r.normalized_raw) >= 10
        )
        raw_best = raw_counter.most_common(1)[0][0] if raw_counter else ""
        suggested = fr.validated_vin or (raw_best if len(raw_best) == 17 else "")

        return OcrPrediction(
            status=str(fr.status),
            suggested_vin=suggested,
            raw_best=raw_best,
            score=round(float(fr.final_score or 0.0), 4),
            model=str(fr.model or model_from_vin(suggested)),
            n_reads=int(fr.n_reads),
            crashes=int(crashes),
            reasons="; ".join(fr.reasons or []),
        )


def choose_best_suggestion(filename_guess: str, pred: OcrPrediction) -> str:
    candidates = [
        pred.suggested_vin if valid_vin(pred.suggested_vin) else "",
        pred.raw_best if valid_vin(pred.raw_best) else "",
        filename_guess if valid_vin(filename_guess) else "",
        norm_text(pred.suggested_vin)[:17] if len(norm_text(pred.suggested_vin)) >= 17 else "",
        norm_text(filename_guess)[:17] if len(norm_text(filename_guess)) >= 17 else "",
    ]
    for c in candidates:
        if c and len(c) == 17:
            return c
    return ""


def review_priority(row: dict) -> int:
    if not row["best_suggestion"] or not valid_vin(row["best_suggestion"]):
        return 1
    if row["ocr_status"] != "ACCEPT":
        return 2
    if row["filename_guess"] and row["filename_guess"] != row["ocr_suggested_vin"]:
        return 3
    if row["quality"] in {"reject", "poor"}:
        return 4
    return 5


def write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_pending_manifests(out_dir: Path, rows: List[dict]) -> None:
    dirs = ensure_dirs(out_dir)
    by_split = defaultdict(list)
    for row in rows:
        label = row.get("best_suggestion", "")
        if valid_vin(label):
            by_split[row["split"]].append((row["dataset_image"], label))

    for split in ("train", "val", "test"):
        split_csv = dirs["splits"] / f"pending_{split}.csv"
        with split_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["image", "suggested_vin"])
            writer.writerows(by_split.get(split, []))

        rec_txt = dirs["paddle"] / f"pending_{split}.txt"
        with rec_txt.open("w", encoding="utf-8") as f:
            for img, label in by_split.get(split, []):
                f.write(f"{img}\t{label}\n")

    readme = dirs["paddle"] / "README.txt"
    readme.write_text(
        "pending_*.txt files are AI-suggested labels for review only.\n"
        "Do not train production models from them until labels_review.csv rows are\n"
        "manually checked and verification_status is set to verified.\n",
        encoding="utf-8",
    )


def load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_text_fit(draw: ImageDraw.ImageDraw, xy: tuple, text: str, font, fill, max_width: int) -> None:
    text = str(text)
    if not text:
        return
    if draw.textlength(text, font=font) <= max_width:
        draw.text(xy, text, font=font, fill=fill)
        return
    ellipsis = "..."
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    draw.text(xy, text + ellipsis, font=font, fill=fill)


def make_contact_sheets(out_dir: Path, rows: List[dict], per_page: int = 20) -> None:
    dirs = ensure_dirs(out_dir)
    for old in dirs["sheets"].glob("page_*.jpg"):
        old.unlink()

    sorted_rows = sorted(rows, key=lambda r: (int(r["review_priority"]), r["id"]))
    thumb_w, thumb_h = 360, 92
    cell_w, cell_h = 430, 190
    cols = 4
    rows_per_page = math.ceil(per_page / cols)
    page_w, page_h = cell_w * cols, cell_h * rows_per_page
    title_font = load_font(18)
    small_font = load_font(14)

    for page_index in range(0, len(sorted_rows), per_page):
        page_rows = sorted_rows[page_index:page_index + per_page]
        canvas = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(canvas)
        for i, row in enumerate(page_rows):
            col = i % cols
            rr = i // cols
            x = col * cell_w
            y = rr * cell_h
            draw.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], outline=(220, 220, 220))
            img_path = out_dir / row["dataset_image"]
            try:
                im = Image.open(img_path).convert("RGB")
                im = ImageOps.contain(im, (thumb_w, thumb_h), method=Image.Resampling.BICUBIC)
                px = x + 10 + (thumb_w - im.width) // 2
                py = y + 8 + (thumb_h - im.height) // 2
                draw.rectangle([x + 10, y + 8, x + 10 + thumb_w, y + 8 + thumb_h], fill=(12, 12, 12))
                canvas.paste(im, (px, py))
            except Exception:
                draw.rectangle([x + 10, y + 8, x + 10 + thumb_w, y + 8 + thumb_h], fill=(30, 30, 30))
                draw.text((x + 18, y + 42), "IMAGE ERROR", font=title_font, fill="white")

            label = row["best_suggestion"] or "NO_SUGGESTION"
            status = f"{row['ocr_status']} {row['ocr_score']}"
            draw_text_fit(draw, (x + 10, y + 108), f"#{row['id']}  {label}", title_font, (0, 0, 0), cell_w - 20)
            draw_text_fit(draw, (x + 10, y + 132), f"{status} | p{row['review_priority']} | {row['quality']}/{row['lighting']}",
                          small_font, (55, 55, 55), cell_w - 20)
            draw_text_fit(draw, (x + 10, y + 152), f"file: {row['filename_guess']}", small_font, (80, 80, 80), cell_w - 20)
            draw_text_fit(draw, (x + 10, y + 170), Path(row["source_image"]).name, small_font, (110, 110, 110), cell_w - 20)

        page_no = page_index // per_page + 1
        canvas.save(dirs["sheets"] / f"page_{page_no:03d}.jpg", quality=92)


def summarize(out_dir: Path, rows: List[dict]) -> None:
    dirs = ensure_dirs(out_dir)
    summary = {
        "total_images": len(rows),
        "ocr_status": dict(Counter(r["ocr_status"] for r in rows)),
        "quality": dict(Counter(r["quality"] for r in rows)),
        "lighting": dict(Counter(r["lighting"] for r in rows)),
        "review_priority": dict(Counter(str(r["review_priority"]) for r in rows)),
        "splits": dict(Counter(r["split"] for r in rows)),
        "filename_ocr_agree": sum(1 for r in rows if r["filename_guess"] and r["filename_guess"] == r["ocr_suggested_vin"]),
        "needs_manual_review": len(rows),
    }
    (dirs["reports"] / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    lines = [
        "# VIN OCR Dataset Review",
        "",
        f"Total images: {summary['total_images']}",
        "",
        "These labels are AI suggestions, not ground truth.",
        "Edit labels/labels_review.csv, correct verified_vin, and set verification_status=verified.",
        "",
        "OCR status:",
    ]
    for k, v in sorted(summary["ocr_status"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "Quality:"]
    for k, v in sorted(summary["quality"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "Lighting:"]
    for k, v in sorted(summary["lighting"].items()):
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "Review priority: 1 is highest risk, 5 is lowest risk.",
        "Contact sheets are in reports/review_sheets/.",
    ]
    (out_dir / "README_DATASET.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare(args: argparse.Namespace) -> None:
    input_dir = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    dirs = ensure_dirs(out_dir)
    images = list_images(input_dir)
    if not images:
        raise SystemExit(f"No images found in {input_dir}")

    ocr_runner: Optional[ProductionOcrRunner] = None
    if args.run_ocr:
        ocr_runner = ProductionOcrRunner(args.ocr_preset)

    rows: List[dict] = []
    try:
        for idx, src in enumerate(images, start=1):
            ext = ".jpg" if src.suffix.lower() == ".jpeg" else src.suffix.lower()
            dst_rel = Path("images") / f"img_{idx:06d}{ext}"
            dst = out_dir / dst_rel
            shutil.copy2(src, dst)

            q = image_quality(src)
            filename_guess = extract_filename_vin(src)
            pred = OcrPrediction()
            if ocr_runner is not None:
                img = cv2.imread(str(src), cv2.IMREAD_COLOR)
                if img is not None:
                    pred = ocr_runner.predict(img)
                else:
                    pred = OcrPrediction(status="IMAGE_ERROR")

            best = choose_best_suggestion(filename_guess, pred)
            group_key = extract_session(src) or filename_guess or src.stem
            split = split_for_group(group_key)
            row = {
                "id": idx,
                "dataset_image": dst_rel.as_posix(),
                "source_image": str(src),
                "source_name": src.name,
                "file_sha1": sha1_file(src),
                "session_id": extract_session(src),
                "captured_at": extract_timestamp(src),
                "filename_guess": filename_guess,
                "filename_model": model_from_vin(filename_guess),
                "ocr_suggested_vin": pred.suggested_vin,
                "ocr_status": pred.status,
                "ocr_score": pred.score,
                "ocr_model": pred.model,
                "ocr_raw_best": pred.raw_best,
                "ocr_n_reads": pred.n_reads,
                "ocr_crashes": pred.crashes,
                "ocr_reasons": pred.reasons,
                "agreement": "yes" if filename_guess and filename_guess == pred.suggested_vin else "no",
                "best_suggestion": best,
                "verified_vin": best,
                "verification_status": "needs_review",
                "split": split,
                "note": "",
                **q,
            }
            row["review_priority"] = review_priority(row)
            rows.append(row)
            if args.progress and (idx == 1 or idx % args.progress == 0 or idx == len(images)):
                print(f"[{idx}/{len(images)}] {src.name} -> {row['ocr_status']} {row['best_suggestion']}", flush=True)
    finally:
        if ocr_runner is not None:
            ocr_runner.close()

    rows.sort(key=lambda r: int(r["id"]))
    write_csv(dirs["labels"] / "labels_review.csv", rows)
    write_csv(dirs["reports"] / "quality_report.csv", rows)
    write_pending_manifests(out_dir, rows)
    make_contact_sheets(out_dir, rows, per_page=args.sheet_size)
    summarize(out_dir, rows)
    print(f"Prepared {len(rows)} images in {out_dir}")


def finalize(args: argparse.Namespace) -> None:
    out_dir = Path(args.out).resolve()
    dirs = ensure_dirs(out_dir)
    review_csv = Path(args.finalize_review).resolve()
    rows = []
    with review_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status = (row.get("verification_status") or "").strip().lower()
            label = norm_text(row.get("verified_vin", ""))
            if status in {"verified", "ok", "train"} and valid_vin(label):
                row["verified_vin"] = label
                rows.append(row)

    by_split = defaultdict(list)
    for row in rows:
        by_split[row.get("split") or split_for_group(row.get("session_id") or row["dataset_image"])].append(
            (row["dataset_image"], row["verified_vin"])
        )

    for split in ("train", "val", "test"):
        path = dirs["paddle"] / f"verified_{split}.txt"
        with path.open("w", encoding="utf-8") as f:
            for img, label in by_split.get(split, []):
                f.write(f"{img}\t{label}\n")

    verified_csv = dirs["labels"] / "labels_verified.csv"
    write_csv(verified_csv, rows)
    print(f"Finalized {len(rows)} verified labels into {dirs['paddle']}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Prepare VIN OCR review dataset")
    ap.add_argument("--input", default="data/crops", help="input crop image directory")
    ap.add_argument("--out", default="data/vin_ocr_dataset", help="output dataset directory")
    ap.add_argument("--run-ocr", action="store_true", help="run current production OCR for label suggestions")
    ap.add_argument("--ocr-preset", default="balanced", choices=("fast", "balanced", "full"),
                    help="OCR suggestion preset: fast/balanced are rec-only for review; full uses production fallback")
    ap.add_argument("--progress", type=int, default=25, help="print progress every N images; 0 disables")
    ap.add_argument("--sheet-size", type=int, default=20, help="images per review contact sheet")
    ap.add_argument("--finalize-review", default="", help="review CSV to convert verified rows into training manifests")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.finalize_review:
        finalize(args)
    else:
        prepare(args)


if __name__ == "__main__":
    main()
