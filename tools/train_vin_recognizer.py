"""
Train a pilot VIN-specific OCR recognizer from reviewed crop labels.

This is a small CRNN + CTC model intended for active-learning and A/B testing,
not a final production release by itself. It uses:
  * gold labels: verification_status=verified
  * optional silver labels: OCR ACCEPT + filename/OCR agreement

Example:
  .\\.venv\\Scripts\\python.exe tools\\train_vin_recognizer.py ^
      --dataset data\\vin_ocr_dataset ^
      --out models\\vin_recognizer_pilot ^
      --epochs 40 --include-silver
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
ALPHABET = "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"
CHAR_TO_IDX = {c: i + 1 for i, c in enumerate(ALPHABET)}  # 0 is CTC blank
IDX_TO_CHAR = {i + 1: c for i, c in enumerate(ALPHABET)}


@dataclass
class LabelRow:
    id: str
    image: str
    label: str
    split: str
    source_type: str  # gold | silver
    source_name: str


def norm_vin(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def strict_vin(value: str) -> bool:
    return bool(VIN_RE.fullmatch(norm_vin(value)))


def load_rows(dataset_dir: Path, include_silver: bool) -> List[LabelRow]:
    csv_path = dataset_dir / "labels" / "labels_review.csv"
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    rows: List[LabelRow] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            status = (r.get("verification_status") or "").strip().lower()
            split = (r.get("split") or "train").strip().lower()
            if split not in {"train", "val", "test"}:
                split = "train"
            if status == "verified":
                label = norm_vin(r.get("verified_vin", ""))
                if strict_vin(label):
                    rows.append(LabelRow(
                        id=r.get("id", ""),
                        image=r.get("dataset_image", ""),
                        label=label,
                        split=split,
                        source_type="gold",
                        source_name=r.get("source_name", ""),
                    ))
            elif include_silver:
                label = norm_vin(r.get("best_suggestion", ""))
                if (r.get("ocr_status") == "ACCEPT"
                        and (r.get("agreement") or "").strip().lower() == "yes"
                        and strict_vin(label)):
                    rows.append(LabelRow(
                        id=r.get("id", ""),
                        image=r.get("dataset_image", ""),
                        label=label,
                        split=split,
                        source_type="silver",
                        source_name=r.get("source_name", ""),
                    ))
    return rows


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = cur
    return prev[-1]


def preprocess_image(path: Path, height: int, width: int, augment: bool) -> torch.Tensor:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Could not read image: {path}")

    if augment:
        if random.random() < 0.70:
            alpha = random.uniform(0.75, 1.35)
            beta = random.uniform(-28, 28)
            img = np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)
        if random.random() < 0.30:
            gamma = random.uniform(0.65, 1.55)
            lut = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)], dtype=np.uint8)
            img = cv2.LUT(img, lut)
        if random.random() < 0.20:
            k = random.choice([3, 5])
            img = cv2.GaussianBlur(img, (k, k), 0)
        if random.random() < 0.25:
            noise = np.random.normal(0, random.uniform(3, 12), img.shape).astype(np.float32)
            img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        if random.random() < 0.15:
            kernel = np.ones((2, 2), np.uint8)
            img = cv2.erode(img, kernel, iterations=1) if random.random() < 0.5 else cv2.dilate(img, kernel, iterations=1)

    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError(f"Bad image shape: {path}")
    scale = height / float(h)
    new_w = max(1, int(round(w * scale)))
    if new_w > width:
        scale = width / float(w)
        new_w = width
        new_h = max(1, int(round(h * scale)))
    else:
        new_h = height
    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA)

    canvas = np.full((height, width), 0, dtype=np.uint8)
    y = max(0, (height - img.shape[0]) // 2)
    x = 0
    canvas[y:y + img.shape[0], x:x + img.shape[1]] = img[:, :width]

    arr = canvas.astype(np.float32) / 255.0
    arr = (arr - 0.5) / 0.5
    return torch.from_numpy(arr).unsqueeze(0)


class VinDataset(Dataset):
    def __init__(
        self,
        dataset_dir: Path,
        rows: Sequence[LabelRow],
        height: int,
        width: int,
        augment: bool,
        gold_weight: int = 1,
    ) -> None:
        self.dataset_dir = dataset_dir
        expanded: List[LabelRow] = []
        for r in rows:
            repeat = max(1, int(gold_weight)) if r.source_type == "gold" else 1
            expanded.extend([r] * repeat)
        self.rows = expanded
        self.height = height
        self.width = width
        self.augment = augment

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int):
        r = self.rows[idx]
        image_path = self.dataset_dir / r.image
        x = preprocess_image(image_path, self.height, self.width, self.augment)
        target = torch.tensor([CHAR_TO_IDX[c] for c in r.label], dtype=torch.long)
        return x, target, r


def collate_batch(batch):
    images, targets, rows = zip(*batch)
    images_t = torch.stack(images, dim=0)
    target_lengths = torch.tensor([len(t) for t in targets], dtype=torch.long)
    targets_t = torch.cat(targets)
    return images_t, targets_t, target_lengths, list(rows)


class VinCRNN(nn.Module):
    def __init__(self, vocab_size: int, hidden: int = 128) -> None:
        super().__init__()
        self.cnn = nn.Sequential(
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
            nn.MaxPool2d((2, 1), (2, 1)),
        )
        self.rnn = nn.GRU(
            input_size=256,
            hidden_size=hidden,
            num_layers=2,
            bidirectional=True,
            dropout=0.10,
        )
        self.fc = nn.Linear(hidden * 2, vocab_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.cnn(x)          # B,C,H,W
        y = y.mean(dim=2)        # B,C,W
        y = y.permute(2, 0, 1)   # T,B,C
        y, _ = self.rnn(y)
        y = self.fc(y)
        return y.log_softmax(dim=2)


def decode_greedy(log_probs: torch.Tensor) -> List[str]:
    # log_probs: T,B,C
    pred = log_probs.argmax(dim=2).detach().cpu().numpy().T
    decoded = []
    for seq in pred:
        chars = []
        prev = 0
        for idx in seq:
            idx = int(idx)
            if idx != 0 and idx != prev:
                chars.append(IDX_TO_CHAR.get(idx, ""))
            prev = idx
        decoded.append("".join(chars))
    return decoded


@torch.no_grad()
def evaluate(model, loader, criterion, device) -> dict:
    model.eval()
    total_loss = 0.0
    total = 0
    exact = 0
    valid = 0
    char_scores = []
    predictions = []
    for images, targets, target_lengths, rows in loader:
        images = images.to(device)
        targets = targets.to(device)
        target_lengths = target_lengths.to(device)
        log_probs = model(images)
        input_lengths = torch.full((images.size(0),), log_probs.size(0), dtype=torch.long, device=device)
        loss = criterion(log_probs, targets, input_lengths, target_lengths)
        total_loss += float(loss.item()) * images.size(0)

        preds = decode_greedy(log_probs)
        labels = [r.label for r in rows]
        for pred, label, row in zip(preds, labels, rows):
            total += 1
            exact += int(pred == label)
            valid += int(strict_vin(pred))
            dist = edit_distance(label, pred)
            char_scores.append(max(0.0, 1.0 - dist / max(len(label), len(pred), 1)))
            predictions.append({
                "id": row.id,
                "split": row.split,
                "source_type": row.source_type,
                "source_name": row.source_name,
                "label": label,
                "prediction": pred,
                "exact": int(pred == label),
                "valid_prediction": int(strict_vin(pred)),
            })
    return {
        "loss": total_loss / max(total, 1),
        "exact": exact / max(total, 1),
        "valid": valid / max(total, 1),
        "char_acc": sum(char_scores) / max(len(char_scores), 1),
        "n": total,
        "predictions": predictions,
    }


def write_rows_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/vin_ocr_dataset")
    ap.add_argument("--out", default="models/vin_recognizer_pilot")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--height", type=int, default=48)
    ap.add_argument("--width", type=int, default=256)
    ap.add_argument("--hidden", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--gold-weight", type=int, default=4)
    ap.add_argument("--include-silver", action="store_true")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))

    dataset_dir = Path(args.dataset).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else ("cpu" if args.device == "auto" else args.device))
    rows = load_rows(dataset_dir, include_silver=args.include_silver)
    if not rows:
        raise SystemExit("No labels selected. Verify labels or use --include-silver.")

    by_split = {
        "train": [r for r in rows if r.split == "train"],
        "val": [r for r in rows if r.split == "val"],
        "test": [r for r in rows if r.split == "test"],
    }
    if len(by_split["train"]) < 5:
        raise SystemExit("Too few train rows.")
    if not by_split["val"]:
        by_split["val"] = by_split["test"] or by_split["train"][: max(1, len(by_split["train"]) // 5)]

    manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": str(dataset_dir),
        "alphabet": ALPHABET,
        "device": str(device),
        "counts": {
            split: {
                "total": len(items),
                "gold": sum(1 for r in items if r.source_type == "gold"),
                "silver": sum(1 for r in items if r.source_type == "silver"),
            }
            for split, items in by_split.items()
        },
        "args": vars(args),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    train_ds = VinDataset(dataset_dir, by_split["train"], args.height, args.width, augment=True, gold_weight=args.gold_weight)
    val_ds = VinDataset(dataset_dir, by_split["val"], args.height, args.width, augment=False)
    test_ds = VinDataset(dataset_dir, by_split["test"], args.height, args.width, augment=False) if by_split["test"] else None
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=0, collate_fn=collate_batch)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False, num_workers=0, collate_fn=collate_batch)
    test_loader = DataLoader(test_ds, batch_size=args.batch_size, shuffle=False, num_workers=0, collate_fn=collate_batch) if test_ds else None

    model = VinCRNN(vocab_size=len(ALPHABET) + 1, hidden=args.hidden).to(device)
    criterion = nn.CTCLoss(blank=0, zero_infinity=True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))

    metrics_rows: List[dict] = []
    best_score = -1.0
    best_path = out_dir / "best.pt"
    last_path = out_dir / "last.pt"

    print(json.dumps(manifest["counts"], indent=2), flush=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        train_loss = 0.0
        train_n = 0
        for images, targets, target_lengths, _rows in train_loader:
            images = images.to(device)
            targets = targets.to(device)
            target_lengths = target_lengths.to(device)
            optimizer.zero_grad(set_to_none=True)
            log_probs = model(images)
            input_lengths = torch.full((images.size(0),), log_probs.size(0), dtype=torch.long, device=device)
            loss = criterion(log_probs, targets, input_lengths, target_lengths)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            train_loss += float(loss.item()) * images.size(0)
            train_n += images.size(0)
        scheduler.step()

        val = evaluate(model, val_loader, criterion, device)
        metric = {
            "epoch": epoch,
            "lr": optimizer.param_groups[0]["lr"],
            "train_loss": train_loss / max(train_n, 1),
            "val_loss": val["loss"],
            "val_exact": val["exact"],
            "val_char_acc": val["char_acc"],
            "val_valid": val["valid"],
            "val_n": val["n"],
        }
        metrics_rows.append(metric)
        score = val["exact"] * 10.0 + val["char_acc"] - val["loss"] * 0.01
        if score > best_score:
            best_score = score
            torch.save({
                "model_state": model.state_dict(),
                "alphabet": ALPHABET,
                "height": args.height,
                "width": args.width,
                "hidden": args.hidden,
                "epoch": epoch,
                "val": {k: v for k, v in val.items() if k != "predictions"},
                "manifest": manifest,
            }, best_path)
            write_rows_csv(out_dir / "val_predictions_best.csv", val["predictions"])
        torch.save({
            "model_state": model.state_dict(),
            "alphabet": ALPHABET,
            "height": args.height,
            "width": args.width,
            "hidden": args.hidden,
            "epoch": epoch,
            "manifest": manifest,
        }, last_path)
        write_rows_csv(out_dir / "metrics.csv", metrics_rows)
        print(
            f"epoch {epoch:03d}/{args.epochs} "
            f"train_loss={metric['train_loss']:.4f} "
            f"val_loss={metric['val_loss']:.4f} "
            f"val_exact={metric['val_exact']:.3f} "
            f"val_char={metric['val_char_acc']:.3f}",
            flush=True,
        )

    checkpoint = torch.load(best_path, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["model_state"])
    if test_loader is not None:
        test = evaluate(model, test_loader, criterion, device)
        write_rows_csv(out_dir / "test_predictions_best.csv", test["predictions"])
        (out_dir / "test_metrics.json").write_text(
            json.dumps({k: v for k, v in test.items() if k != "predictions"}, indent=2),
            encoding="utf-8",
        )
        print("test " + json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in test.items() if k != "predictions"}), flush=True)
    print(f"best_checkpoint={best_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
