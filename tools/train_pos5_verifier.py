"""
============================================================
train_pos5_verifier.py  —  Position-5 vizual verifier (HOG+SVM) o'qitish
============================================================
Section 8 ning ikkinchi bosqichi. VIN position-5 belgilarini (B/C/D/G/H/J)
ajratuvchi kichik klassifikator o'qitadi.

DATASET tuzilishi (har sinf uchun papka, ichida pos5 katak croplari):
    <dataset_dir>/
        B/  *.png
        C/  *.png
        D/  *.png
        G/  *.png
        H/  *.png
        J/  *.png

Katak croplarni yig'ish: config/settings.yaml da
    pos5_verifier.save_char_crops: true
qo'ying — tizim har qabul qilingan VIN uchun pos5 katagini
data/pos5_crops/<chosen_char>/ ga saqlaydi. So'ng ularni QO'LDA to'g'ri
sinf papkalariga saralang (yoki tasdiqlang).

Ishlatish:
    python tools/train_pos5_verifier.py --data data/pos5_crops --out models/pos5_hog_svm.joblib

So'ng config/settings.yaml da:
    pos5_verifier:
      enabled: true
      backend: "hog_svm"
      model_path: "models/pos5_hog_svm.joblib"
      min_confidence: 0.60

Bog'liqliklar: scikit-learn, joblib (pip install scikit-learn joblib).
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.ai.pos5_verifier import POS5_CLASSES, _hog_features  # noqa: E402


def _load_dataset(data_dir: str):
    import cv2
    X, y = [], []
    classes = []
    for cls in sorted(os.listdir(data_dir)):
        cdir = os.path.join(data_dir, cls)
        if not os.path.isdir(cdir):
            continue
        classes.append(cls)
        for fn in os.listdir(cdir):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                continue
            img = cv2.imread(os.path.join(cdir, fn))
            if img is None:
                continue
            try:
                X.append(_hog_features(img))
                y.append(cls)
            except Exception:
                continue
    return np.array(X), np.array(y), classes


def main() -> int:
    ap = argparse.ArgumentParser(description="Train pos5 HOG+SVM verifier")
    ap.add_argument("--data", required=True, help="dataset dir (per-class subfolders)")
    ap.add_argument("--out", default="models/pos5_hog_svm.joblib")
    ap.add_argument("--C", type=float, default=1.0)
    args = ap.parse_args()

    try:
        from sklearn.svm import SVC
        from sklearn.model_selection import cross_val_score
        import joblib
    except ImportError:
        print("scikit-learn/joblib kerak: pip install scikit-learn joblib")
        return 2

    X, y, classes = _load_dataset(args.data)
    if len(X) == 0:
        print(f"Dataset bo'sh: {args.data} (har sinf uchun papka + PNG kutiladi)")
        return 1
    print(f"Namunalar: {len(X)}, sinflar: {sorted(set(y))}, xususiyat o'lchami: {X.shape[1]}")

    clf = SVC(C=args.C, kernel="rbf", probability=True, class_weight="balanced")
    if len(set(y)) > 1 and len(X) >= 10:
        scores = cross_val_score(clf, X, y, cv=min(5, len(X) // len(set(y)) or 2))
        print(f"Cross-val aniqlik: {scores.mean():.3f} +/- {scores.std():.3f}")
    clf.fit(X, y)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    joblib.dump({"model": clf, "classes": list(clf.classes_)}, args.out)
    print(f"Saqlandi: {args.out}")
    print("Endi config/settings.yaml da pos5_verifier.enabled=true va model_path ni bering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
