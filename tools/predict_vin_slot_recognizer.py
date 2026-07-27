"""
Run inference with the fixed-17-position VIN recognizer.

Examples:
  .\\.venv\\Scripts\\python.exe tools\\predict_vin_slot_recognizer.py ^
      --checkpoint models\\vin_slot_recognizer_pilot\\best.pt ^
      --dataset data\\vin_ocr_dataset ^
      --labels data\\vin_ocr_dataset\\labels\\labels_review.csv ^
      --out models\\vin_slot_recognizer_pilot\\predictions_verified.csv ^
      --verified-only

  .\\.venv\\Scripts\\python.exe tools\\predict_vin_slot_recognizer.py ^
      --checkpoint models\\vin_slot_recognizer_pilot\\best.pt ^
      --image data\\vin_ocr_dataset\\images\\img_000001.jpg

  .\\.venv\\Scripts\\python.exe tools\\predict_vin_slot_recognizer.py ^
      --checkpoint models\\vin_slot_recognizer_pilot\\best.pt ^
      --images-dir data\\crops ^
      --out models\\vin_slot_recognizer_pilot\\live_crop_predictions.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import torch

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from train_vin_recognizer import preprocess_image, strict_vin  # noqa: E402
from train_vin_slot_recognizer import SLOT_TO_CHAR, VinSlotCNN  # noqa: E402


def norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def load_model(checkpoint_path: Path, device: torch.device):
    ckpt = torch.load(checkpoint_path, map_location=device, weights_only=False)
    alphabet = ckpt.get("alphabet")
    if not alphabet:
        raise RuntimeError("Checkpoint has no alphabet")
    model = VinSlotCNN(vocab_size=len(alphabet)).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    return model, ckpt


@torch.no_grad()
def predict_one(model, ckpt, image_path: Path, device: torch.device) -> str:
    x = preprocess_image(
        image_path,
        int(ckpt.get("height", 64)),
        int(ckpt.get("width", 340)),
        augment=False,
    ).unsqueeze(0).to(device)
    logits = model(x)
    idxs = logits.argmax(dim=2).detach().cpu().numpy()[0]
    return "".join(SLOT_TO_CHAR[int(i)] for i in idxs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", default="models/vin_slot_recognizer_pilot/best.pt")
    ap.add_argument("--dataset", default="data/vin_ocr_dataset")
    ap.add_argument("--labels", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--images-dir", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--verified-only", action="store_true")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else ("cpu" if args.device == "auto" else args.device))
    model, ckpt = load_model(Path(args.checkpoint), device)

    if args.image:
        pred = predict_one(model, ckpt, Path(args.image), device)
        print(json.dumps({"image": args.image, "prediction": pred, "valid_vin": strict_vin(pred)}, ensure_ascii=False))
        return 0

    if args.images_dir:
        images_dir = Path(args.images_dir)
        paths = sorted(
            p for p in images_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
        )
        rows = []
        for p in paths:
            pred = predict_one(model, ckpt, p, device)
            rows.append({
                "image": str(p),
                "prediction": pred,
                "valid_prediction": int(strict_vin(pred)),
            })
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["image", "prediction", "valid_prediction"])
                writer.writeheader()
                writer.writerows(rows)
        metrics = {
            "rows": len(rows),
            "valid_prediction_rate": (sum(r["valid_prediction"] for r in rows) / len(rows)) if rows else None,
            "out": args.out,
        }
        print(json.dumps(metrics, indent=2))
        return 0

    labels_path = Path(args.labels or Path(args.dataset) / "labels" / "labels_review.csv")
    dataset_dir = Path(args.dataset)
    rows = []
    with labels_path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            status = (r.get("verification_status") or "").strip().lower()
            if args.verified_only and status != "verified":
                continue
            image_rel = r.get("dataset_image") or ""
            if not image_rel:
                continue
            label = ""
            if status == "verified":
                label = norm(r.get("verified_vin", ""))
            pred = predict_one(model, ckpt, dataset_dir / image_rel, device)
            rows.append({
                "id": r.get("id", ""),
                "dataset_image": image_rel,
                "source_name": r.get("source_name", ""),
                "verification_status": status,
                "label": label,
                "prediction": pred,
                "exact": int(bool(label) and pred == label),
                "valid_prediction": int(strict_vin(pred)),
            })

    total_labeled = sum(1 for r in rows if r["label"])
    exact = sum(int(r["exact"]) for r in rows)
    valid = sum(int(r["valid_prediction"]) for r in rows)
    char_ok = 0
    char_total = 0
    for r in rows:
        if r["label"]:
            char_ok += sum(1 for a, b in zip(r["prediction"], r["label"]) if a == b)
            char_total += 17

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"])
            writer.writeheader()
            writer.writerows(rows)

    metrics = {
        "rows": len(rows),
        "labeled_rows": total_labeled,
        "exact": exact / total_labeled if total_labeled else None,
        "char_acc": char_ok / char_total if char_total else None,
        "valid_prediction_rate": valid / len(rows) if rows else None,
        "out": args.out,
    }
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
