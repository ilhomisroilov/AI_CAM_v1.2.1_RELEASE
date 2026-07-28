"""Portable release preflight, database migration and dry-run gates.

This module is intentionally hardware-free. It validates the exact artifacts used
by production, proves the database can be created/migrated, and (for --dry-run)
loads all three OCR paths without starting PLC, camera, RFID or the web server.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import (
    BACKUPS_DIR,
    CONFIG_LOAD_ERRORS,
    CONFIG_SOURCES,
    CROPS_DIR,
    DATA_DIR,
    DB_PATH,
    ENGRAVED_COLLECTION_DIR,
    LOGS_DIR,
    MODELS_DIR,
    PADDLE_CLS_DIR,
    PADDLE_DET_DIR,
    PADDLE_REC_DIR,
    PROJECT_ROOT,
    RUNTIME_DIR,
    TEMP_DIR,
    TRAINED_MODEL_PATH,
    effective_config,
    validate_required_secrets,
)

RELEASE_VERSION = "1.2.1"
ENGRAVED_DIR = MODELS_DIR / "engraved_ocr_v1.2.1"
ENGRAVED_ONNX = ENGRAVED_DIR / "model.onnx"
ENGRAVED_METADATA = ENGRAVED_DIR / "model_metadata.json"
REQUIRED_RUNTIME_DIRS = (
    DATA_DIR,
    LOGS_DIR,
    CROPS_DIR,
    TEMP_DIR,
    ENGRAVED_COLLECTION_DIR,
    BACKUPS_DIR,
)
PADDLE_FILES = ("inference.pdmodel", "inference.pdiparams")
DEPENDENCIES = {
    "torch": "torch",
    "torchvision": "torchvision",
    "ultralytics": "ultralytics",
    "paddle": "paddlepaddle",
    "paddleocr": "paddleocr",
    "onnx": "onnx",
    "onnxruntime": "onnxruntime",
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "Pillow",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "yaml": "PyYAML",
}


class StartupGateError(RuntimeError):
    """A deterministic, operator-actionable startup-gate failure."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_runtime_directories() -> list[str]:
    created = []
    for directory in REQUIRED_RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise StartupGateError(f"runtime directory is not usable: {directory}")
        created.append(str(directory.resolve()))
    return created


def validate_paths() -> dict[str, str]:
    paths = {
        "project_root": PROJECT_ROOT,
        "runtime_root": RUNTIME_DIR,
        "database": DB_PATH,
        "logs": LOGS_DIR,
        "crops": CROPS_DIR,
        "temp": TEMP_DIR,
        "engraved_collection": ENGRAVED_COLLECTION_DIR,
        "backups": BACKUPS_DIR,
        "yolo": TRAINED_MODEL_PATH,
        "engraved_onnx": ENGRAVED_ONNX,
        "engraved_metadata": ENGRAVED_METADATA,
        "paddle_det": PADDLE_DET_DIR,
        "paddle_rec": PADDLE_REC_DIR,
        "paddle_cls": PADDLE_CLS_DIR,
    }
    model_names = {
        "project_root", "yolo", "engraved_onnx", "engraved_metadata",
        "paddle_det", "paddle_rec", "paddle_cls",
    }
    runtime_names = {
        "runtime_root", "database", "crops", "temp", "engraved_collection",
        "backups",
    }
    escaped = []
    for name, path in paths.items():
        if name in model_names and not _inside(path, PROJECT_ROOT):
            escaped.append(f"{name}={path} (outside project root)")
        elif name in runtime_names and not _inside(path, RUNTIME_DIR):
            escaped.append(f"{name}={path} (outside data root)")
        elif name == "logs" and not _inside(path, LOGS_DIR):
            escaped.append(f"{name}={path} (outside log root)")
    if escaped:
        raise StartupGateError(
            "configured path violates the deployment roots: " + ", ".join(escaped)
        )
    return {name: str(path.resolve()) for name, path in paths.items()}


def _required_file(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise StartupGateError(f"{label} is missing or empty: {path}")
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def validate_model_artifacts() -> dict[str, Any]:
    models: dict[str, Any] = {
        "yolo": _required_file(TRAINED_MODEL_PATH, "YOLO model"),
        "engraved_onnx": _required_file(ENGRAVED_ONNX, "Engraved ONNX model"),
        "engraved_metadata": _required_file(
            ENGRAVED_METADATA, "Engraved metadata"
        ),
    }
    try:
        metadata = json.loads(ENGRAVED_METADATA.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StartupGateError(f"invalid Engraved metadata JSON: {exc}") from exc
    required_keys = {
        "model_name",
        "model_version",
        "input_size",
        "charset",
        "output_dimension",
        "reject_index",
        "onnx_status",
    }
    missing = sorted(required_keys - set(metadata))
    if missing:
        raise StartupGateError(f"Engraved metadata missing keys: {missing}")
    if metadata["model_version"] != RELEASE_VERSION:
        raise StartupGateError(
            f"Engraved model version {metadata['model_version']!r} != {RELEASE_VERSION}"
        )
    if metadata["onnx_status"] != "ok":
        raise StartupGateError("Engraved metadata does not mark ONNX output as ok")
    output_dimension = int(metadata["output_dimension"])
    if output_dimension != len(str(metadata["charset"])) + 1:
        raise StartupGateError(
            "Engraved output contract mismatch: output_dimension must equal "
            "charset length + reject output"
        )
    if int(metadata["reject_index"]) != output_dimension - 1:
        raise StartupGateError("Engraved reject_index does not match output contract")
    declared_onnx = str(metadata.get("onnx_sha256", "") or "").lower()
    actual_onnx = models["engraved_onnx"]["sha256"]
    if declared_onnx and declared_onnx != actual_onnx:
        raise StartupGateError("Engraved ONNX checksum does not match metadata")
    models["engraved_contract"] = {
        "model_name": metadata["model_name"],
        "model_version": metadata["model_version"],
        "charset": metadata["charset"],
        "output_dimension": output_dimension,
        "reject_index": int(metadata["reject_index"]),
    }

    for role, directory in (
        ("paddle_det", PADDLE_DET_DIR),
        ("paddle_rec", PADDLE_REC_DIR),
        ("paddle_cls", PADDLE_CLS_DIR),
    ):
        if not directory.is_dir():
            raise StartupGateError(f"{role} directory missing: {directory}")
        models[role] = {
            name: _required_file(directory / name, f"{role} {name}")
            for name in PADDLE_FILES
        }
        models[role]["path"] = str(directory.resolve())
    return models


def dependency_diagnostics() -> dict[str, Any]:
    results: dict[str, Any] = {}
    failures = []
    for module_name, dist_name in DEPENDENCIES.items():
        try:
            importlib.import_module(module_name)
            try:
                version = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                version = getattr(sys.modules[module_name], "__version__", "builtin")
            results[module_name] = {"ok": True, "version": str(version)}
        except Exception as exc:
            results[module_name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            failures.append(module_name)
    results["sqlite3"] = {
        "ok": True,
        "version": sqlite3.sqlite_version,
    }
    if failures:
        detail = "; ".join(
            f"{name}={results[name]['error']}" for name in failures
        )
        raise StartupGateError(
            "required dependency imports failed: " + detail
        )
    return results


def prepare_database(path: Path) -> dict[str, Any]:
    """Create base schema, apply v1.2.1 migration twice, then verify it."""
    from .database import db
    from .database import ocr_v121_db

    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_override = getattr(db, "_DB_PATH_OVERRIDE", None)
    db.set_db_path(path)
    try:
        db.init_db()
        with sqlite3.connect(str(path)) as connection:
            first = ocr_v121_db.migrate(connection)
            second = ocr_v121_db.migrate(connection)
            state = ocr_v121_db.migration_state(connection)
            vin_count = int(
                connection.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]
            )
        if not all(
            state.get(key)
            for key in ("engine_table", "collection_table", "final_ocr_columns", "applied")
        ):
            raise StartupGateError(f"v1.2.1 migration verification failed: {state}")
        if second.get("columns_added_this_call"):
            raise StartupGateError(
                "v1.2.1 migration is not idempotent: second run added columns"
            )
        return {
            "path": str(path),
            "base_schema": True,
            "first_migration": first,
            "second_migration": second,
            "state": state,
            "vin_records": vin_count,
        }
    finally:
        db.set_db_path(previous_override)


def _write_report(name: str, report: dict[str, Any]) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / name
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


def run_self_check(*, deep: bool = False, require_gpu: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    if CONFIG_LOAD_ERRORS:
        raise StartupGateError(
            "configuration load failed: " + "; ".join(CONFIG_LOAD_ERRORS)
        )
    if sys.platform.startswith("linux") and sys.version_info[:2] != (3, 11):
        raise StartupGateError(
            "Linux production requires CPython 3.11; "
            f"current interpreter is {sys.version_info.major}.{sys.version_info.minor}"
        )
    report: dict[str, Any] = {
        "mode": "self-check",
        "version": RELEASE_VERSION,
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "runtime_directories": ensure_runtime_directories(),
        "paths": validate_paths(),
        "configuration": effective_config(redact=True),
        "models": validate_model_artifacts(),
        "dependencies": dependency_diagnostics(),
    }
    from .runtime import collect_runtime_diagnostics, evaluate_runtime

    runtime = collect_runtime_diagnostics(deep=bool(deep or require_gpu))
    runtime_ok, runtime_failures = evaluate_runtime(runtime, require_gpu=require_gpu)
    runtime["check"] = {
        "ok": runtime_ok,
        "require_gpu": require_gpu,
        "failures": runtime_failures,
    }
    if not runtime_ok:
        engine_detail = "; ".join(
            f"{name}={runtime.get(name, {}).get('error')}"
            for name in ("torch", "paddle")
            if runtime.get(name, {}).get("error")
        )
        raise StartupGateError(
            "runtime diagnostics failed: "
            + "; ".join(runtime_failures)
            + (f"; {engine_detail}" if engine_detail else "")
        )
    report["runtime"] = runtime
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    report["ok"] = True
    report["path_audit_report"] = str(
        _write_report("startup_path_audit.json", report)
    )
    return report


def run_dry_run() -> dict[str, Any]:
    """Prove startup viability without any live hardware or permanent server."""
    started = time.perf_counter()
    report = run_self_check(deep=False, require_gpu=False)
    report["mode"] = "dry-run"
    fd, temp_name = tempfile.mkstemp(
        prefix="ai_cam_dry_run_", suffix=".db", dir=str(TEMP_DIR)
    )
    os.close(fd)
    temp_db = Path(temp_name)
    temp_db.unlink(missing_ok=True)
    cache_dir = Path(
        tempfile.mkdtemp(prefix="paddle_empty_cache_", dir=str(TEMP_DIR))
    )
    old_cache = {
        name: os.environ.get(name)
        for name in ("PADDLE_HOME", "PADDLEOCR_HOME")
    }
    os.environ["PADDLE_HOME"] = str(cache_dir)
    os.environ["PADDLEOCR_HOME"] = str(cache_dir)
    engines = []
    from .config import OCR
    original_ocr_gpu = OCR.use_gpu
    try:
        from .runtime import apply_safe_runtime_policy
        report["safe_runtime_policy"] = apply_safe_runtime_policy(OCR)
        report["database"] = prepare_database(temp_db)
        import numpy as np
        from .ai import ocr_contract as contract
        from .ai.engines.engraved_v121 import EngravedV121Recognizer
        from .ai.engines.paddle_profiles import (
            PaddleEnhancedRecognizer,
            PaddleRawRecognizer,
        )

        engraved = EngravedV121Recognizer(str(ENGRAVED_DIR), expected_length=17)
        raw = PaddleRawRecognizer()
        enhanced = PaddleEnhancedRecognizer()
        engines = [engraved, raw, enhanced]
        for engine in engines:
            engine.initialize()
            health = engine.health()
            if not health.ready:
                raise StartupGateError(
                    f"OCR engine {engine.engine_id} did not initialize: "
                    f"{health.last_error or health.detail}"
                )

        dummy = np.full((64, 512, 3), 255, dtype=np.uint8)
        request = contract.OCRRecognitionRequest(
            session_id="DRY-RUN",
            capture_generation=1,
            job_id="DRY-RUN",
            crops=[contract.CropRef(0, dummy, source="dry-run")],
            submitted_at=time.time(),
        )
        engine_results = {}
        for engine in engines:
            result = engine.recognize(request)
            engine_results[engine.engine_id] = {
                "status": result.engine_status,
                "latency_ms": result.latency_ms,
                "raw_sequence": result.raw_sequence,
            }
            if result.engine_status in (
                contract.ENGINE_UNAVAILABLE,
                contract.ERROR,
                contract.OCR_ENGINE_CRASH,
                contract.OCR_ENGINE_TIMEOUT,
            ):
                raise StartupGateError(
                    f"OCR engine {engine.engine_id} smoke inference failed: "
                    f"{result.engine_status} {result.warnings}"
                )
        report["ocr_engines"] = engine_results
        report["paddle_cache_isolation"] = {
            "empty_cache": str(cache_dir),
            "files_created": [
                str(path.relative_to(cache_dir))
                for path in cache_dir.rglob("*")
                if path.is_file()
            ],
            "configured_dirs": {
                "det_model_dir": str(PADDLE_DET_DIR.resolve()),
                "rec_model_dir": str(PADDLE_REC_DIR.resolve()),
                "cls_model_dir": str(PADDLE_CLS_DIR.resolve()),
            },
        }
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        report["ok"] = True
        report["dry_run_report"] = str(_write_report("dry_run_report.json", report))
        return report
    finally:
        for engine in reversed(engines):
            try:
                engine.close()
            except Exception:
                pass
        temp_db.unlink(missing_ok=True)
        for name, value in old_cache.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        OCR.use_gpu = original_ocr_gpu
        # Cache directory is deliberately retained under ignored runtime/temp so
        # the dry-run report can prove whether a fallback download occurred.


def prepare_normal_startup() -> dict[str, Any]:
    report = run_self_check(deep=False, require_gpu=False)
    report["mode"] = "normal-startup"
    # Credentials are read from config/settings.yaml (canonical, single source).
    # Empty/placeholder secrets are a clear WARNING — never a fatal gate. The app
    # still starts so the dashboard and every non-credential service stay available;
    # hardware that needs a credential simply reports "disconnected" until
    # settings.yaml is filled in. Environment variables remain an OPTIONAL override;
    # their absence never stops startup (no /etc/ai-cam/ai-cam.env dependency).
    credential_warnings = validate_required_secrets()
    report["credential_warnings"] = credential_warnings
    if credential_warnings:
        print(
            "[AI_CAM CONFIG] Diqqat — quyidagi credential(lar) bo'sh yoki placeholder; "
            "config/settings.yaml da to'ldiring (startup davom etadi):",
            file=sys.stderr,
        )
        for item in credential_warnings:
            print(f"  - {item}", file=sys.stderr)
    report["database"] = prepare_database(DB_PATH)
    report["ok"] = True
    report["startup_report"] = str(_write_report("startup_report.json", report))
    return report
