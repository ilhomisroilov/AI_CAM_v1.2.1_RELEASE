"""
============================================================
evaluate_ocr.py  —  OCR aniqlik + regressiya baholash (Section 9)
============================================================
Ishlab chiqarish OCR yo'lini (process-izolyatsiyalangan pool + preprocessing
variantlar + position-level consensus + accept gates) QO'LDA BELGILANGAN
ground-truth ga qarshi baholaydi.

HISOBOT:
  * exact VIN accuracy      (qabul qilingan va TO'G'RI VIN ulushi)
  * false accept rate       (qabul qilingan, ammo NOTO'G'RI VIN — eng muhim xavf)
  * NO_READ / OCR_AMBIGUOUS rate
  * per-position confusion matrix (gt_char -> pred_char)
  * position-5 C/D/B/G/H/J confusion

REGRESSIYA:
  * ilgari shubhali bo'lgan VINlar YA to'g'ri o'qilishi, YOKI OCR_AMBIGUOUS
    qaytishi kerak;
  * noto'g'ri VIN FAQAT strukturaviy yaroqli bo'lgani UCHUN qabul qilinmasligi kerak.

GROUND TRUTH:
  --csv FILE   (ustunlar: filename,vin[,note])  — TAVSIYA (qo'lda tekshirilgan).
  yoki --crops DIR dagi fayl nomidan olinadi (DIQQAT: fayl nomi TIZIM O'QIGAN VIN —
       unda xato bo'lishi mumkin, shuning uchun bu faqat qulaylik uchun).

Ishlatish:
  python tools/evaluate_ocr.py --crops "server data 06-iyul/data/crops" --csv gt.csv
  python tools/evaluate_ocr.py --crops data/crops --limit 50 --out report.csv

Eslatma: PaddleOCR o'rnatilgan bo'lishi kerak (ishlab chiqarish mashinasida).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_VIN_FN = re.compile(r"^([A-Z0-9]{17})")
POS5_CLASSES = set("BCDGHJ")


def _gt_from_filename(fn: str):
    base = os.path.splitext(os.path.basename(fn))[0]
    m = _VIN_FN.match(base.upper())
    return m.group(1) if m else None


def _load_csv_gt(path: str):
    gt = {}
    expected = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and any((x or "").strip().lower() in
                                     ("filename", "file", "image") for x in reader.fieldnames):
            fields = {x.strip().lower(): x for x in reader.fieldnames if x}
            fn_key = next((fields[x] for x in ("filename", "file", "image") if x in fields), None)
            vin_key = fields.get("vin") or fields.get("ground_truth")
            status_key = fields.get("expected_status")
            for row in reader:
                fn = (row.get(fn_key) or "").strip() if fn_key else ""
                if not fn:
                    continue
                base = os.path.basename(fn)
                gt[base] = (row.get(vin_key) or "").strip().upper() if vin_key else ""
                expected[base] = ((row.get(status_key) or "ACCEPT").strip().upper()
                                  if status_key else "ACCEPT")
        else:
            f.seek(0)
            for row in csv.reader(f):
                if not row:
                    continue
                fn = row[0].strip()
                vin = row[1].strip().upper() if len(row) > 1 else ""
                gt[os.path.basename(fn)] = vin
                expected[os.path.basename(fn)] = "ACCEPT"
    return gt, expected


def _find_default_crops():
    for cand in ("server data 06-iyul/data/crops", "data/crops"):
        if os.path.isdir(cand):
            return cand
    return "data/crops"


def _build_worker():
    """OCRWorker ni ishlab chiqarish yo'li bilan quradi (callbacksiz)."""
    from backend.ai.ocr_worker import OCRWorker
    from backend.config import OCR
    w = OCRWorker(on_result=lambda *a: None, on_fail=lambda *a: None)
    if not w._ensure_pool():
        raise RuntimeError("OCR process pool yaratilmadi (PaddleOCR o'rnatilganmi?)")
    w.preload()
    return w, OCR


def _run_one(worker, OCR, img):
    """Bitta rasm uchun to'liq OCR yo'li -> FusionResult."""
    from backend.ai.vin_fusion import fuse
    tasks, cq = worker._build_tasks([img])
    det_primary = bool(getattr(OCR, "paddle_det", False))
    reads, crashes = worker._read_pool(tasks, cq, det=det_primary)
    if (not det_primary and bool(getattr(OCR, "det_fallback_enabled", True))
            and len({r.normalized_raw for r in reads if len(r.normalized_raw) == 17}) < 2):
        det_tasks = [(ci, v) for (ci, v) in tasks
                     if v.variant_name in ("raw_resized", "clahe_unsharp")][:1]
        if det_tasks:
            dr, dc = worker._read_pool(det_tasks, cq, det=True)
            reads += dr
            crashes += dc
    return fuse(reads, **worker._fuse_params()), crashes, reads


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate OCR accuracy + regression")
    ap.add_argument("--crops", default=None, help="crops dir (ground truth images)")
    ap.add_argument("--csv", default=None, help="ground-truth CSV: filename,vin[,note]")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--match", default="", help="faqat filename ichida shu matn bor crop")
    ap.add_argument("--out", default="ocr_eval_report.csv")
    ap.add_argument("--trace-reads", action="store_true",
                    help="har variantning raw OCR natijasini konsolga chiqarish")
    args = ap.parse_args()

    import cv2
    crops_dir = args.crops or _find_default_crops()
    if not os.path.isdir(crops_dir):
        print(f"Crops papkasi topilmadi: {crops_dir}")
        return 2
    csv_gt, expected_status = _load_csv_gt(args.csv) if args.csv else ({}, {})
    if not csv_gt:
        print("[OGOHLANTIRISH] CSV berilmadi — ground truth fayl nomidan olinadi "
              "(tizim o'qigan VIN; xato bo'lishi mumkin).")

    if csv_gt:
        # CSV — qo'lda tasdiqlangan test manifesti. Papkadagi boshqa yuzlab cropni
        # tasodifan filename-as-truth bilan aralashtirmaymiz.
        files = sorted(fn for fn in csv_gt if os.path.isfile(os.path.join(crops_dir, fn)))
    else:
        files = sorted(fn for fn in os.listdir(crops_dir)
                       if fn.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")))
    if args.limit:
        files = files[:args.limit]
    if args.match:
        files = [fn for fn in files if args.match.lower() in fn.lower()]
    if not files:
        print(f"Rasm topilmadi: {crops_dir}")
        return 1

    worker, OCR = _build_worker()

    n = 0
    exact = 0
    false_accept = 0
    accepted = 0
    no_read = 0
    ambiguous = 0
    pos_conf = defaultdict(Counter)          # position -> Counter[(gt,pred)]
    pos5_conf = Counter()                    # (gt5, pred5)
    rows = []

    for fn in files:
        gt = csv_gt.get(fn) or _gt_from_filename(fn)
        img = cv2.imread(os.path.join(crops_dir, fn))
        if img is None:
            continue
        n += 1
        fr, crashes, variant_reads = _run_one(worker, OCR, img)
        pred = fr.validated_vin or ""
        status = fr.status
        if status == "ACCEPT":
            accepted += 1
            if gt and pred == gt:
                exact += 1
            elif gt:
                false_accept += 1
        elif status == "NO_READ":
            no_read += 1
        else:
            ambiguous += 1

        # confusion (faqat gt mavjud va uzunlik 17 bo'lsa)
        if gt and len(gt) == 17 and len(pred) == 17:
            for i in range(17):
                if gt[i] != pred[i]:
                    pos_conf[i + 1][(gt[i], pred[i])] += 1
            if gt[4] in POS5_CLASSES or pred[4] in POS5_CLASSES:
                if gt[4] != pred[4]:
                    pos5_conf[(gt[4], pred[4])] += 1
        rows.append((fn, gt or "", pred, status, round(fr.final_score, 3),
                     fr.n_crops, fr.n_reads, crashes,
                     ";".join(fr.reasons) if fr.reasons else ""))
        if args.trace_reads:
            print(f"{fn}: truth={gt or '-'} expected={expected_status.get(fn, 'ACCEPT')} "
                  f"pred={pred or '-'} status={status} score={fr.final_score:.3f} "
                  f"reasons={fr.reasons}")
            for read in variant_reads:
                print(f"  crop={read.crop_index} evidence={getattr(read, 'evidence_group', read.crop_index)} "
                      f"variant={read.variant_name:<24} conf={read.ocr_conf:.3f} "
                      f"raw={read.normalized_raw}")

    worker.shutdown()

    # --- Hisobot ---
    def pct(x):
        return f"{100.0 * x / n:.1f}%" if n else "0%"

    print("\n================ OCR EVALUATION ================")
    print(f"Rasmlar: {n}   (crops: {crops_dir})")
    print(f"Exact VIN accuracy : {exact}/{n} ({pct(exact)})")
    print(f"Accepted (jami)    : {accepted}/{n} ({pct(accepted)})")
    print(f"FALSE ACCEPT       : {false_accept}/{n} ({pct(false_accept)})  <-- 0 bo'lishi kerak")
    print(f"OCR_AMBIGUOUS      : {ambiguous}/{n} ({pct(ambiguous)})")
    print(f"NO_READ            : {no_read}/{n} ({pct(no_read)})")

    print("\n--- Per-position confusion (gt -> pred), eng ko'p 15 ---")
    all_conf = Counter()
    for pos, c in pos_conf.items():
        for (g, p), cnt in c.items():
            all_conf[(pos, g, p)] += cnt
    for (pos, g, p), cnt in all_conf.most_common(15):
        print(f"  pos{pos:>2}: {g} -> {p}   x{cnt}")

    print("\n--- Position-5 confusion (C/D/B/G/H/J) ---")
    if pos5_conf:
        for (g, p), cnt in pos5_conf.most_common():
            print(f"  {g} -> {p}   x{cnt}")
    else:
        print("  (pos5 xatolik yo'q yoki ground truth mos emas)")

    # per-image CSV
    try:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            wcsv = csv.writer(f)
            wcsv.writerow(["filename", "ground_truth", "predicted", "status",
                           "final_score", "n_crops", "n_reads", "crashes", "gate_fail"])
            wcsv.writerows(rows)
        print(f"\nPer-image natija: {args.out}")
    except Exception as exc:
        print(f"CSV yozilmadi: {exc}")

    if false_accept > 0:
        print("\n[XATO] FALSE ACCEPT > 0 — noto'g'ri VIN(lar) qabul qilindi. "
              "Gate/pos5 parametrlarini qattiqlashtiring.")
        return 1
    print("\n[OK] Hech qanday noto'g'ri VIN qabul qilinmadi (false_accept=0).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
