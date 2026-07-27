"""
Train a fixed-17-position VIN recognizer.

Unlike a general OCR CTC model, this model is tailored to VIN crops where the
output is always exactly 17 characters. The CNN produces 17 horizontal slots and
classifies one VIN character per slot.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import time
from pathlib import Path
from typing import List

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

from train_vin_recognizer import (
    ALPHABET,
    load_rows,
    preprocess_image,
    strict_vin,
)


CHAR_TO_SLOT = {c: i for i, c in enumerate(ALPHABET)}
SLOT_TO_CHAR = {i: c for i, c in enumerate(ALPHABET)}


class SlotDataset(Dataset):
    def __init__(self, dataset_dir: Path, rows, height: int, width: int, augment: bool, gold_weight: int = 1):
        self.dataset_dir = dataset_dir
        expanded = []
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
        x = preprocess_image(self.dataset_dir / r.image, self.height, self.width, self.augment)
        y = torch.tensor([CHAR_TO_SLOT[c] for c in r.label], dtype=torch.long)
        return x, y, r


def collate(batch):
    xs, ys, rows = zip(*batch)
    return torch.stack(xs, 0), torch.stack(ys, 0), list(rows)


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
        y = self.pool(y).squeeze(2)  # B,C,17
        y = self.classifier(y)       # B,V,17
        return y.permute(0, 2, 1)    # B,17,V


def decode(logits: torch.Tensor) -> List[str]:
    idxs = logits.argmax(dim=2).detach().cpu().numpy()
    return ["".join(SLOT_TO_CHAR[int(i)] for i in row) for row in idxs]


@torch.no_grad()
def evaluate(model, loader, criterion, device):
    model.eval()
    total_loss = 0.0
    total = 0
    exact = 0
    valid = 0
    char_ok = 0
    char_total = 0
    predictions = []
    for xs, ys, rows in loader:
        xs = xs.to(device)
        ys = ys.to(device)
        logits = model(xs)
        loss = criterion(logits.reshape(-1, logits.size(-1)), ys.reshape(-1))
        total_loss += float(loss.item()) * xs.size(0)
        preds = decode(logits)
        labels = [r.label for r in rows]
        for pred, label, row in zip(preds, labels, rows):
            total += 1
            exact += int(pred == label)
            valid += int(strict_vin(pred))
            char_ok += sum(1 for a, b in zip(pred, label) if a == b)
            char_total += 17
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
        "char_acc": char_ok / max(char_total, 1),
        "n": total,
        "predictions": predictions,
    }


def write_csv(path: Path, rows: List[dict]) -> None:
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
    ap.add_argument("--out", default="models/vin_slot_recognizer_pilot")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--height", type=int, default=64)
    ap.add_argument("--width", type=int, default=340)
    ap.add_argument("--lr", type=float, default=8e-4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--gold-weight", type=int, default=5)
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
    by_split = {
        "train": [r for r in rows if r.split == "train"],
        "val": [r for r in rows if r.split == "val"],
        "test": [r for r in rows if r.split == "test"],
    }
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
    print(json.dumps(manifest["counts"], indent=2), flush=True)

    train_loader = DataLoader(
        SlotDataset(dataset_dir, by_split["train"], args.height, args.width, True, args.gold_weight),
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=collate,
    )
    val_loader = DataLoader(
        SlotDataset(dataset_dir, by_split["val"], args.height, args.width, False),
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
        collate_fn=collate,
    )
    test_loader = DataLoader(
        SlotDataset(dataset_dir, by_split["test"], args.height, args.width, False),
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
        collate_fn=collate,
    )

    model = VinSlotCNN(vocab_size=len(ALPHABET)).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))

    best_score = -1.0
    metrics = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        train_loss = 0.0
        train_n = 0
        for xs, ys, _rows in train_loader:
            xs = xs.to(device)
            ys = ys.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(xs)
            loss = criterion(logits.reshape(-1, logits.size(-1)), ys.reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            train_loss += float(loss.item()) * xs.size(0)
            train_n += xs.size(0)
        scheduler.step()

        val = evaluate(model, val_loader, criterion, device)
        row = {
            "epoch": epoch,
            "lr": optimizer.param_groups[0]["lr"],
            "train_loss": train_loss / max(train_n, 1),
            "val_loss": val["loss"],
            "val_exact": val["exact"],
            "val_char_acc": val["char_acc"],
            "val_valid": val["valid"],
            "val_n": val["n"],
        }
        metrics.append(row)
        score = val["exact"] * 10.0 + val["char_acc"] - val["loss"] * 0.01
        if score > best_score:
            best_score = score
            torch.save({
                "model_state": model.state_dict(),
                "alphabet": ALPHABET,
                "height": args.height,
                "width": args.width,
                "epoch": epoch,
                "val": {k: v for k, v in val.items() if k != "predictions"},
                "manifest": manifest,
            }, out_dir / "best.pt")
            write_csv(out_dir / "val_predictions_best.csv", val["predictions"])
        torch.save({
            "model_state": model.state_dict(),
            "alphabet": ALPHABET,
            "height": args.height,
            "width": args.width,
            "epoch": epoch,
            "manifest": manifest,
        }, out_dir / "last.pt")
        write_csv(out_dir / "metrics.csv", metrics)
        print(
            f"epoch {epoch:03d}/{args.epochs} "
            f"train_loss={row['train_loss']:.4f} "
            f"val_loss={row['val_loss']:.4f} "
            f"val_exact={row['val_exact']:.3f} "
            f"val_char={row['val_char_acc']:.3f}",
            flush=True,
        )

    ckpt = torch.load(out_dir / "best.pt", map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state"])
    test = evaluate(model, test_loader, criterion, device)
    write_csv(out_dir / "test_predictions_best.csv", test["predictions"])
    (out_dir / "test_metrics.json").write_text(
        json.dumps({k: v for k, v in test.items() if k != "predictions"}, indent=2),
        encoding="utf-8",
    )
    print("test " + json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in test.items() if k != "predictions"}), flush=True)
    print(f"best_checkpoint={out_dir / 'best.pt'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
