"""Run a short hardware-free server smoke test and request graceful shutdown."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _process_is_alive(process) -> bool:
    try:
        return bool(process.is_running() and process.status() != "zombie")
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "launcher",
        nargs="?",
        type=Path,
        default=Path(sys.executable),
        help="Python executable (with run.py) or packaged AI_CAM.exe",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("runtime/logs/startup-smoke.log"),
    )
    args = parser.parse_args()

    launcher = args.launcher.resolve()
    is_python = launcher.name.lower().startswith("python")
    command = (
        [str(launcher), "run.py", "--no-hardware"]
        if is_python else [str(launcher), "--no-hardware"]
    )
    env = os.environ.copy()
    env.update(
        {
            "AI_CAM_AUTH_PASSWORD": "Local-Smoke-Only-v1.2.1!",
            "AI_CAM_PLC_ENABLED": "0",
            "AI_CAM_RFID_ENABLED": "0",
            "AI_CAM_SERVER_PORT": str(args.port),
        }
    )
    args.log.parent.mkdir(parents=True, exist_ok=True)
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )

    with args.log.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        health = None
        deadline = time.monotonic() + args.timeout
        try:
            while time.monotonic() < deadline:
                code = proc.poll()
                if code is not None:
                    raise RuntimeError(f"server exited before health check: {code}")
                try:
                    with urlopen(
                        f"http://127.0.0.1:{args.port}/health", timeout=2.0
                    ) as response:
                        health = json.loads(response.read().decode("utf-8"))
                        if response.status == 200:
                            break
                except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
                    pass
                time.sleep(0.5)
            if not health or health.get("status") != "ok":
                raise RuntimeError(f"health check did not become ready: {health!r}")

            child_processes = []
            try:
                import psutil

                child_processes = psutil.Process(proc.pid).children(recursive=True)
            except Exception:
                pass
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            code = proc.wait(timeout=30)
            if code != 0:
                raise RuntimeError(f"graceful server exit returned {code}")
            child_deadline = time.monotonic() + 10.0
            remaining_children = child_processes
            while remaining_children and time.monotonic() < child_deadline:
                remaining_children = [
                    child for child in remaining_children
                    if _process_is_alive(child)
                ]
                if remaining_children:
                    time.sleep(0.1)
            if remaining_children:
                raise RuntimeError(
                    "orphan child process(es) after graceful exit: "
                    + ", ".join(str(child.pid) for child in remaining_children)
                )
        except Exception:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            raise

    print(
        json.dumps(
            {
                "ok": True,
                "command": command,
                "health": health.get("status"),
                "exit_code": code,
                "child_processes_observed": len(child_processes),
                "orphan_child_processes": 0,
                "log": str(args.log.resolve()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
