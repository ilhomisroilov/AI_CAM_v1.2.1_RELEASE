"""
============================================================
run.py  —  AI_CAM ishga tushirish nuqtasi (LAN ga ochiq)
============================================================
Ishlatish:
    python run.py
    python run.py --vin-shadow
    python run.py --vin-shadow-once

Server 0.0.0.0 ga bog'lanadi — ya'ni shu kompyuterning BARCHA tarmoq
interfeyslarida tinglaydi. Zavod tarmog'idagi boshqa kompyuterlar
brauzerda quyidagini ochib kirishadi:
    http://<SHU_KOMPYUTER_IP>:8080/dashboard

Ishga tushganda server aniq ulanish manzilini chop etadi.
Eslatma: boshqa kompyuterlar ulana olishi uchun Windows Firewall da
8080-port (TCP) uchun kiruvchi ruxsat ochilishi kerak (pastdagi izohga qarang).
"""
from __future__ import annotations

import argparse
import csv
import os
import socket
import threading
import time
import json
import multiprocessing
import signal
import site
import sys
from datetime import datetime
from pathlib import Path

# Canonical root: all legacy relative paths are anchored here, regardless of the
# shell's current working directory. In PyInstaller ONEDIR the executable itself
# is the release root.
PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("PYTORCH_NVML_BASED_CUDA_CHECK", "1")
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
if getattr(sys, "frozen", False):
    # Paddle 2.6 expects a string USER_SITE and uses it to locate
    # <release-root>/paddle/libs inside a PyInstaller ONEDIR build.
    site.USER_SITE = str(PROJECT_ROOT)

from backend.native_runtime import configure_native_library_path

# Must happen before the self-check or OCR worker subprocess is created.
configure_native_library_path()

from backend.config import SERVER
from backend.release_startup import StartupGateError
from backend.version import APP_VERSION

# LAN kirish uchun har doim barcha interfeyslarni tinglaymiz
HOST = "0.0.0.0"
PORT = SERVER.port
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def get_lan_ip() -> str:
    """
    Shu kompyuterning asosiy LAN IP manzilini aniqlaydi (masalan 192.168.x.x).
    Internet TALAB QILINMAYDI — paket yuborilmaydi, faqat marshrut tanlanadi.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Tashqi manzilga "ulanish" — OS to'g'ri chiquvchi interfeysni tanlaydi
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"          # tarmoq yo'q bo'lsa zaxira
    finally:
        s.close()
    return ip


def _print_banner(lan_ip: str) -> None:
    line = "=" * 56
    print("\n" + line)
    print(f"  AI_CAM {APP_VERSION} server ishga tushdi — LAN ga ochiq (host 0.0.0.0)")
    print(line)
    print(f"  Shu kompyuterda:     http://localhost:{PORT}/dashboard")
    print(f"  Tarmoqdagi boshqalar: http://{lan_ip}:{PORT}/dashboard   <-- ULASHING")
    print(line)
    print("  Eslatma: boshqa PC ulana olmasa, Windows Firewall da")
    print(f"  {PORT}-port (TCP, Inbound) uchun ruxsat oching (quyidagi izohga qarang).")
    print(line + "\n")


def _iter_images(crops_dir: Path):
    if not crops_dir.exists():
        return []
    return sorted(
        p for p in crops_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def _load_vin_shadow_model(checkpoint: Path):
    import torch
    from tools.predict_vin_slot_recognizer import load_model, predict_one, strict_vin

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, ckpt = load_model(checkpoint, device)
    return model, ckpt, device, predict_one, strict_vin


def _append_shadow_rows(out_csv: Path, rows: list[dict]) -> None:
    if not rows:
        return
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    exists = out_csv.exists() and out_csv.stat().st_size > 0
    fields = ["timestamp", "image", "prediction", "valid_prediction", "error"]
    with out_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def run_vin_shadow_once(checkpoint: Path, crops_dir: Path, out_csv: Path) -> int:
    """data/crops ichidagi rasmlarga VIN slot model prediction chiqaradi."""
    checkpoint = checkpoint.resolve()
    crops_dir = crops_dir.resolve()
    out_csv = out_csv.resolve()
    if not checkpoint.exists():
        print(f"[VIN_SHADOW] Model topilmadi: {checkpoint}")
        return 2

    print(f"[VIN_SHADOW] Model yuklanmoqda: {checkpoint}")
    model, ckpt, device, predict_one, strict_vin = _load_vin_shadow_model(checkpoint)
    print(f"[VIN_SHADOW] Device: {device} | crops: {crops_dir}")

    rows = []
    for p in _iter_images(crops_dir):
        try:
            pred = predict_one(model, ckpt, p, device)
            rows.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "image": str(p),
                "prediction": pred,
                "valid_prediction": int(strict_vin(pred)),
                "error": "",
            })
        except Exception as exc:
            rows.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "image": str(p),
                "prediction": "",
                "valid_prediction": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    if out_csv.exists():
        out_csv.unlink()
    _append_shadow_rows(out_csv, rows)
    ok = sum(int(r["valid_prediction"]) for r in rows)
    print(f"[VIN_SHADOW] Yozildi: {out_csv} | rows={len(rows)} | valid={ok}")
    return 0


def start_vin_shadow_watcher(
    checkpoint: Path,
    crops_dir: Path,
    out_csv: Path,
    interval_sec: float,
    min_file_age_sec: float = 2.0,
) -> None:
    """
    Server bilan birga fon thread ochadi. Yangi crop tushsa, DB ga tegmasdan
    prediction CSV ga yozadi. Bu production OCR o'rnini bosmaydi.
    """
    checkpoint = checkpoint.resolve()
    crops_dir = crops_dir.resolve()
    out_csv = out_csv.resolve()

    def _loop() -> None:
        if not checkpoint.exists():
            print(f"[VIN_SHADOW] Model topilmadi, shadow o'chiq: {checkpoint}")
            return
        try:
            print(f"[VIN_SHADOW] Fon model yuklanmoqda: {checkpoint}")
            model, ckpt, device, predict_one, strict_vin = _load_vin_shadow_model(checkpoint)
            print(f"[VIN_SHADOW] Shadow yoqildi | device={device} | crops={crops_dir} | out={out_csv}")
        except Exception as exc:
            print(f"[VIN_SHADOW] Model yuklanmadi: {type(exc).__name__}: {exc}")
            return

        seen: set[str] = set()
        while True:
            batch = []
            now = time.time()
            for p in _iter_images(crops_dir):
                key = str(p.resolve())
                if key in seen:
                    continue
                try:
                    st = p.stat()
                    if now - st.st_mtime < min_file_age_sec:
                        continue
                    pred = predict_one(model, ckpt, p, device)
                    batch.append({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "image": key,
                        "prediction": pred,
                        "valid_prediction": int(strict_vin(pred)),
                        "error": "",
                    })
                    seen.add(key)
                except Exception as exc:
                    batch.append({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "image": key,
                        "prediction": "",
                        "valid_prediction": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    seen.add(key)
            _append_shadow_rows(out_csv, batch)
            if batch:
                print(f"[VIN_SHADOW] {len(batch)} ta yangi crop prediction qilindi -> {out_csv}")
            time.sleep(max(1.0, float(interval_sec)))

    threading.Thread(target=_loop, name="vin-shadow-watcher", daemon=True).start()


def parse_args():
    ap = argparse.ArgumentParser(description="AI_CAM server launcher")
    ap.add_argument("--vin-shadow", action="store_true",
                    help="server bilan birga VIN slot model shadow prediction CSV yozadi")
    ap.add_argument("--vin-shadow-once", action="store_true",
                    help="serverni ochmasdan crop papkani bir marta prediction qiladi")
    ap.add_argument("--vin-shadow-model", default="models/vin_slot_recognizer_pilot/best.pt")
    ap.add_argument("--vin-shadow-crops", default="data/crops")
    ap.add_argument("--vin-shadow-out", default="models/vin_slot_recognizer_pilot/live_crop_predictions.csv")
    ap.add_argument("--vin-shadow-interval", type=float, default=5.0)
    ap.add_argument("--self-check", action="store_true",
                    help="serverni ochmasdan release/model/dependency diagnostikasini bajaradi")
    ap.add_argument("--dry-run", action="store_true",
                    help="hardware/serverni ochmasdan DB migration va real OCR init/inference qiladi")
    ap.add_argument("--deep", action="store_true",
                    help="--self-check bilan real Torch/Paddle tensor amallarini bajaradi")
    ap.add_argument("--require-gpu", action="store_true",
                    help="--self-check GPU serverda ikkala engine CUDA ishlatmasa xato qaytaradi")
    args = ap.parse_args()
    if args.self_check and args.dry_run:
        ap.error("--self-check va --dry-run bir vaqtda ishlatilmaydi")
    if args.require_gpu and not args.self_check:
        ap.error("--require-gpu faqat --self-check bilan ishlatiladi")
    if args.deep and not args.self_check:
        ap.error("--deep faqat --self-check bilan ishlatiladi")
    return args


def main() -> int:
    multiprocessing.freeze_support()
    args = parse_args()
    try:
        from backend.release_startup import (
            prepare_normal_startup,
            run_dry_run,
            run_self_check,
        )

        if args.self_check:
            report = run_self_check(
                deep=bool(args.deep or args.require_gpu),
                require_gpu=bool(args.require_gpu),
            )
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            return 0
        if args.dry_run:
            report = run_dry_run()
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            return 0
        if args.vin_shadow_once:
            return run_vin_shadow_once(
                PROJECT_ROOT / args.vin_shadow_model,
                PROJECT_ROOT / args.vin_shadow_crops,
                PROJECT_ROOT / args.vin_shadow_out,
            )

        startup = prepare_normal_startup()
        print(json.dumps({
            "startup": "ready",
            "version": APP_VERSION,
            "paths": startup.get("paths", {}),
            "database": startup.get("database", {}),
        }, indent=2, sort_keys=True, default=str))
        if args.vin_shadow:
            start_vin_shadow_watcher(
                PROJECT_ROOT / args.vin_shadow_model,
                PROJECT_ROOT / args.vin_shadow_crops,
                PROJECT_ROOT / args.vin_shadow_out,
                args.vin_shadow_interval,
            )
        _print_banner(get_lan_ip())
        import uvicorn
        # reload=False — fon threadlari (kamera/OCR) bilan ziddiyat bo'lmasligi uchun
        uvicorn_config = uvicorn.Config(
            "backend.server:app",
            host=HOST,
            port=PORT,
            reload=False,
            log_level="info",
        )
        uvicorn_server = uvicorn.Server(uvicorn_config)
        # Windows smoke/service runners use CTRL_BREAK for a process group.
        # Uvicorn handles SIGINT/SIGTERM itself but not SIGBREAK; translating
        # it into should_exit preserves the ASGI lifespan shutdown and exit 0.
        if hasattr(signal, "SIGBREAK"):
            signal.signal(
                signal.SIGBREAK,
                lambda _signum, _frame: setattr(uvicorn_server, "should_exit", True),
            )
        uvicorn_server.run()
        return 0
    except StartupGateError as exc:
        print(f"[AI_CAM STARTUP GATE] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[AI_CAM] graceful operator shutdown")
        return 0
    except Exception as exc:
        print(
            f"[AI_CAM FATAL] {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
