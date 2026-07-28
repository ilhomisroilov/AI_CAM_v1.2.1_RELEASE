"""Read-only production diagnostics shared by startup and ``/health``."""
from __future__ import annotations

import concurrent.futures
import os
import shutil
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import (
    CAMERA,
    CONFIG_SOURCES,
    DB_PATH,
    LOGS_DIR,
    OPERATIONS,
    PLC,
    RFID,
    RUNTIME_DIR,
)

_preflight_lock = threading.Lock()
_last_preflight: dict[str, Any] = {
    "checked_at": None,
    "status": "not-run",
    "endpoints": {},
}


def _tcp_probe(name: str, host: str, port: int, timeout: float) -> tuple[str, dict]:
    started = time.perf_counter()
    result = {
        "host": host,
        "port": int(port),
        "reachable": False,
        "latency_ms": None,
        "error": None,
    }
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.1, timeout)):
            result["reachable"] = True
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return name, result


def run_network_preflight(timeout: float | None = None) -> dict[str, Any]:
    """Probe configured TCP endpoints concurrently without sending commands."""
    timeout = float(timeout or OPERATIONS.health_preflight_timeout_sec)
    no_hardware = os.environ.get("AI_CAM_NO_HARDWARE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    targets = [
        ("camera_control", CAMERA.ip, CAMERA.cola_port, not no_hardware),
        ("camera_blob", CAMERA.ip, CAMERA.blob_port, not no_hardware),
        ("plc", PLC.ip, PLC.port, bool(
            not no_hardware and PLC.enabled and PLC.mode != "simulator"
        )),
        ("rfid", RFID.ip_address, RFID.port, bool(
            not no_hardware and RFID.enabled and RFID.mode != "simulator"
        )),
    ]
    results: dict[str, Any] = {}
    enabled = [(name, host, port) for name, host, port, active in targets if active]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(enabled)), thread_name_prefix="preflight"
    ) as pool:
        jobs = [
            pool.submit(_tcp_probe, name, host, port, timeout)
            for name, host, port in enabled
        ]
        for job in concurrent.futures.as_completed(jobs):
            name, result = job.result()
            results[name] = result
    for name, host, port, active in targets:
        if not active:
            results[name] = {
                "host": host,
                "port": int(port),
                "reachable": None,
                "skipped": True,
                "reason": "component disabled or simulator mode",
            }
    reachable = sum(item.get("reachable") is True for item in results.values())
    attempted = sum(item.get("reachable") is not None for item in results.values())
    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "ok" if attempted == reachable else "degraded",
        "reachable": reachable,
        "attempted": attempted,
        "endpoints": results,
    }
    with _preflight_lock:
        _last_preflight.clear()
        _last_preflight.update(report)
    return report


def last_network_preflight() -> dict[str, Any]:
    with _preflight_lock:
        return {
            **_last_preflight,
            "endpoints": dict(_last_preflight.get("endpoints", {})),
        }


def _disk_snapshot(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    if free_gb <= OPERATIONS.disk_critical_free_gb:
        status = "critical"
    elif free_gb <= OPERATIONS.disk_warning_free_gb:
        status = "warning"
    else:
        status = "ok"
    return {
        "path": str(path),
        "status": status,
        "total_gb": round(usage.total / (1024 ** 3), 2),
        "used_gb": round(usage.used / (1024 ** 3), 2),
        "free_gb": round(free_gb, 2),
        "warning_free_gb": OPERATIONS.disk_warning_free_gb,
        "critical_free_gb": OPERATIONS.disk_critical_free_gb,
    }


def _database_snapshot() -> dict[str, Any]:
    result = {
        "path": str(DB_PATH),
        "exists": DB_PATH.is_file(),
        "bytes": DB_PATH.stat().st_size if DB_PATH.is_file() else 0,
        "journal_mode": None,
        "integrity": "not-created",
        "error": None,
    }
    if not DB_PATH.is_file():
        return result
    try:
        with sqlite3.connect(str(DB_PATH), timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            result["journal_mode"] = str(
                conn.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            result["integrity"] = str(
                conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            ).lower()
    except sqlite3.Error as exc:
        result["integrity"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _process_snapshot() -> dict[str, Any]:
    result = {
        "pid": os.getpid(),
        "child_processes": None,
        "max_expected_child_processes": OPERATIONS.max_expected_child_processes,
        "worker_leak_warning": False,
    }
    try:
        import psutil

        children = psutil.Process().children(recursive=True)
        result["child_processes"] = [
            {"pid": child.pid, "name": child.name(), "status": child.status()}
            for child in children
        ]
        result["worker_leak_warning"] = (
            len(children) > OPERATIONS.max_expected_child_processes
        )
        result["rss_mb"] = round(
            psutil.Process().memory_info().rss / (1024 * 1024), 1
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def operational_snapshot() -> dict[str, Any]:
    """Return bounded, read-only host state for monitoring."""
    disks: dict[str, Any] = {}
    for name, path in (("runtime", RUNTIME_DIR), ("logs", LOGS_DIR)):
        try:
            disks[name] = _disk_snapshot(path)
        except OSError as exc:
            disks[name] = {
                "path": str(path),
                "status": "critical",
                "error": f"{type(exc).__name__}: {exc}",
            }
    preflight = last_network_preflight()
    # Public health needs component/port/readiness but not industrial addresses
    # or raw socket errors. Exact endpoints remain in logs and diagnose_linux.sh.
    preflight["endpoints"] = {
        name: {
            key: value for key, value in item.items()
            if key not in {"host", "error"}
        }
        for name, item in preflight.get("endpoints", {}).items()
    }
    return {
        "config_sources": list(CONFIG_SOURCES),
        "network_preflight": preflight,
        "database": _database_snapshot(),
        "disk": disks,
        "process": _process_snapshot(),
    }
