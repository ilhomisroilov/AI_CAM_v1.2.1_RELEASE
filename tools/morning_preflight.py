"""
============================================================
morning_preflight.py — go/no-go before the 24h observation
============================================================
One command, run each morning on the production host BEFORE starting the 24-hour
observation. It certifies that the frozen v1.2.1 build, models, database, runtime,
GPU and hardware endpoints are all ready — so that once
`python run.py --observe-hours 24` starts, nothing needs to change.

    python tools/morning_preflight.py --require-gpu

Prints a per-check table and ends with **PREFLIGHT PASS** or the exact blockers.
Exit code 0 = PASS, non-zero = blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED_VERSION = "1.2.1"
EXPECTED_TZ = "Asia/Tashkent"


class Check:
    def __init__(self):
        self.rows = []  # (name, ok, detail, hard)

    def add(self, name, ok, detail="", hard=True):
        self.rows.append((name, bool(ok), str(detail), bool(hard)))

    def blockers(self):
        return [r for r in self.rows if r[3] and not r[1]]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tcp(host: str, port: int, timeout=2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _git(args):
    try:
        return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def run(require_gpu: bool, require_hardware: bool) -> Check:
    c = Check()

    # --- version / git ---
    try:
        from backend.version import APP_VERSION, API_VERSION
        c.add("version == 1.2.1", API_VERSION == EXPECTED_VERSION, f"{APP_VERSION}")
    except Exception as exc:
        c.add("version == 1.2.1", False, str(exc))
    head = _git(["rev-parse", "HEAD"])
    dirty = _git(["status", "--porcelain"])
    c.add("git working tree clean", dirty == "", "dirty" if dirty else "clean")
    c.add("git commit resolved", bool(head), head[:12])

    # --- config + model hashes ---
    try:
        from backend.config import (BASE_DIR, DB_PATH, DATA_DIR, RUNTIME_DIR, MODELS_DIR,
                                     TRAINED_MODEL_PATH, PADDLE_DET_DIR, PADDLE_REC_DIR,
                                     PADDLE_CLS_DIR, CAMERA, PLC, RFID, SERVER)
        cfgf = BASE_DIR / "config" / "settings.yaml"
        c.add("config hash", cfgf.is_file(), _sha(cfgf)[:16] if cfgf.is_file() else "missing")
        eng = MODELS_DIR / "engraved_ocr_v1.2.1" / "model.onnx"
        model_files = {
            "yolo": TRAINED_MODEL_PATH, "engraved_onnx": eng,
            "paddle_det": PADDLE_DET_DIR / "inference.pdmodel",
            "paddle_rec": PADDLE_REC_DIR / "inference.pdmodel",
            "paddle_cls": PADDLE_CLS_DIR / "inference.pdmodel",
        }
        for name, p in model_files.items():
            c.add(f"model {name}", p.is_file(), (_sha(p)[:16] if p.is_file() else f"MISSING {p}"))
    except Exception as exc:
        c.add("config/models", False, str(exc))
        CAMERA = PLC = RFID = SERVER = None
        DB_PATH = RUNTIME_DIR = DATA_DIR = None

    # --- database + runtime writable ---
    try:
        if DATA_DIR is not None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            probe = DATA_DIR / ".preflight_write"
            probe.write_text("ok", encoding="utf-8"); probe.unlink()
            c.add("runtime/data writable", True, str(DATA_DIR))
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH)); conn.execute("PRAGMA user_version"); conn.close()
        c.add("database openable", True, str(DB_PATH))
    except Exception as exc:
        c.add("database/runtime writable", False, str(exc))

    # --- disk ---
    try:
        free_gb = shutil.disk_usage(str(RUNTIME_DIR or ROOT)).free / (1024 ** 3)
        c.add("disk space >= 5 GiB", free_gb >= 5.0, f"{free_gb:.1f} GiB free")
    except Exception as exc:
        c.add("disk space", False, str(exc))

    # --- observation dir ---
    try:
        obs = (RUNTIME_DIR or ROOT / "runtime") / "audit"
        obs.mkdir(parents=True, exist_ok=True)
        (ROOT / "reports").mkdir(parents=True, exist_ok=True)
        c.add("observation dirs writable", True, str(obs))
    except Exception as exc:
        c.add("observation dirs writable", False, str(exc))

    # --- timezone ---
    tz = ""
    try:
        tz = datetime.now().astimezone().tzname() or ""
        env_tz = os.environ.get("TZ", "")
        ok_tz = (EXPECTED_TZ in env_tz) or tz in ("+05", "UZT", "TASHKENT") or ("+05" in datetime.now().astimezone().strftime("%z")[:3])
        c.add("timezone Asia/Tashkent", ok_tz, f"tz={tz} TZ={env_tz}", hard=False)
    except Exception as exc:
        c.add("timezone", False, str(exc), hard=False)

    # --- GPU ---
    torch_ok = paddle_ok = yolo_ok = False
    try:
        import torch
        if torch.cuda.is_available():
            x = torch.tensor([2.0], device="cuda")
            torch_ok = float((x * 3).sum().cpu()) == 6.0
        c.add("Torch CUDA compute", torch_ok, f"cuda={torch.cuda.is_available()}", hard=require_gpu)
    except Exception as exc:
        c.add("Torch CUDA compute", False, str(exc), hard=require_gpu)
    try:
        import paddle
        paddle_ok = bool(paddle.device.is_compiled_with_cuda()) and paddle.device.cuda.device_count() > 0
        c.add("Paddle GPU", paddle_ok, f"cuda_compiled={paddle.device.is_compiled_with_cuda()}", hard=require_gpu)
    except Exception as exc:
        c.add("Paddle GPU", False, str(exc), hard=require_gpu)
    try:
        if TRAINED_MODEL_PATH.is_file():
            from ultralytics import YOLO
            m = YOLO(str(TRAINED_MODEL_PATH))
            dev = "cuda:0" if torch_ok else "cpu"
            m.to(dev)
            yolo_ok = True
            c.add(f"YOLO load ({dev})", yolo_ok, dev, hard=require_gpu)
    except Exception as exc:
        c.add("YOLO load", False, str(exc), hard=require_gpu)

    # --- ports / hardware ---
    if SERVER is not None:
        port_free = not _tcp("127.0.0.1", int(getattr(SERVER, "port", 8080)))
        c.add(f"server port {getattr(SERVER,'port',8080)} free", port_free,
              "free" if port_free else "IN USE — stale server?")
    if CAMERA is not None:
        cam_ip = getattr(CAMERA, "ip", "") or getattr(CAMERA, "host", "")
        for label, port in (("camera CoLa 2111", 2111), ("camera BLOB 2113", 2113)):
            c.add(label, _tcp(cam_ip, port) if cam_ip else False, cam_ip, hard=require_hardware)
    if PLC is not None:
        c.add("PLC 5003", _tcp(getattr(PLC, "ip", ""), getattr(PLC, "port", 5003)),
              getattr(PLC, "ip", ""), hard=require_hardware)
    if RFID is not None:
        c.add("RFID 80", _tcp(getattr(RFID, "ip", ""), getattr(RFID, "port", 80)),
              getattr(RFID, "ip", ""), hard=require_hardware)

    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AI_CAM morning preflight")
    ap.add_argument("--require-gpu", action="store_true", help="GPU checks are hard blockers")
    ap.add_argument("--require-hardware", action="store_true",
                    help="camera/PLC/RFID reachability are hard blockers")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    started = time.time()
    c = run(args.require_gpu, args.require_hardware)
    blockers = c.blockers()
    passed = not blockers

    if args.json:
        print(json.dumps({"passed": passed,
                          "checks": [{"name": n, "ok": ok, "detail": d, "hard": h}
                                     for n, ok, d, h in c.rows],
                          "blockers": [n for n, *_ in blockers]}, indent=2))
    else:
        print(f"\nAI_CAM morning preflight — {datetime.now().isoformat(timespec='seconds')}\n" + "-" * 60)
        for name, ok, detail, hard in c.rows:
            mark = "PASS" if ok else ("FAIL" if hard else "warn")
            print(f"  [{mark:>4}] {name:<28} {detail}")
        print("-" * 60)
        if passed:
            print(f"PREFLIGHT PASS ({time.time()-started:.1f}s) — ready for "
                  f"`python run.py --observe-hours 24`.")
        else:
            print("PREFLIGHT BLOCKED:")
            for n, *_ in blockers:
                print(f"  - {n}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
