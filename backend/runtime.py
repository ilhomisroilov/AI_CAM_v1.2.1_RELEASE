"""Cross-platform runtime detection and production self-checks.

This module deliberately contains no OCR/VIN decision logic.  It only answers
which compute engines are actually usable and applies the safe CPU fallback
when a GPU was requested but the installed Paddle build cannot use it.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import threading
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

from .native_runtime import configure_native_library_path, native_subprocess_env


# PyTorch documents this opt-in for CUDA availability checks that do not poison
# subsequent process creation. OCR already uses spawn isolation; keep the parent
# Torch probe safe as well.
os.environ.setdefault("PYTORCH_NVML_BASED_CUDA_CHECK", "1")
# Paddle is always probed/used in a fresh subprocess.  Populate the environment
# before those children start so Paddle cuDNN 8 can resolve pip CUDA 11 libs.
configure_native_library_path()


_CACHE_LOCK = threading.Lock()
_RUNTIME_CACHE: Optional[Dict[str, Any]] = None


class RuntimeReadinessError(RuntimeError):
    """Raised when a production host requested GPU but the deep gate failed."""


def _env_true(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _failure_code(error: Any, probe_ok: Any) -> Optional[str]:
    """Normalize native failures without publishing driver/library paths."""
    detail = str(error or "").lower()
    if any(library in detail for library in (
        "libcublas", "libnvrtc", "libcufft", "libcurand", "libcusolver",
        "libcusparse", "libcudart", "libnccl",
    )) and (
        "no such file" in detail
        or "cannot open" in detail
        or "could not load" in detail
    ):
        return "CUDA_LIBRARY_LOAD_FAILED"
    if "cudnn" in detail and (
        "cannot load" in detail
        or "could not load" in detail
        or "shared libr" in detail
        or "cudnngetversion" in detail
        or "dso_handle" in detail
    ):
        return "CUDNN_LOAD_FAILED"
    if probe_ok is False:
        return "COMPUTE_PROBE_FAILED"
    return None


def _finalize_engine_status(status: Dict[str, Any], deep: bool) -> Dict[str, Any]:
    # ``cuda_available`` is kept as a compatibility alias for raw visibility.
    # Production policy must use ``compute_ready`` instead.
    visible = bool(status.get("cuda_available"))
    status["device_visible"] = visible
    # Visibility alone is never readiness. A caller that needs GPU readiness
    # must request the deep probe and observe a successful tensor operation.
    status["compute_ready"] = bool(
        deep and visible and status.get("probe_ok") is True
    )
    status["failure_code"] = _failure_code(
        status.get("error"), status.get("probe_ok")
    )
    return status


def _package_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return "unknown"


def _nvidia_smi() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "hardware_present": False,
        "gpus": [],
        "driver_version": None,
        "driver_cuda_max": None,
        "error": None,
    }
    try:
        query = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        result["error"] = "nvidia-smi not found"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if query.returncode != 0:
        result["error"] = (query.stderr or query.stdout or "nvidia-smi failed").strip()
        return result

    for line in query.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            memory_mib: Optional[int] = int(float(parts[2]))
        except ValueError:
            memory_mib = None
        result["gpus"].append(
            {"name": parts[0], "driver_version": parts[1], "memory_mib": memory_mib}
        )
    result["hardware_present"] = bool(result["gpus"])
    if result["gpus"]:
        result["driver_version"] = result["gpus"][0]["driver_version"]

    # The normal summary output contains the maximum CUDA runtime version that
    # the installed driver advertises.  It is more robust than hard-coding an
    # Ubuntu release or a driver-number table.
    try:
        summary = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5, check=False
        )
        match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)?)", summary.stdout)
        if match:
            result["driver_cuda_max"] = match.group(1)
    except Exception:
        pass
    return result


def _torch_status(deep: bool) -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "installed": False,
        "version": _package_version("torch"),
        "cuda_build": None,
        "cuda_available": False,
        "device_visible": False,
        "compute_ready": False,
        "device_count": 0,
        "device_name": None,
        "probe_ok": None,
        "failure_code": None,
        "error": None,
    }
    try:
        import torch

        status["installed"] = True
        status["version"] = str(torch.__version__)
        status["cuda_build"] = torch.version.cuda
        status["cuda_available"] = bool(torch.cuda.is_available())
        if status["cuda_available"]:
            status["device_count"] = int(torch.cuda.device_count())
            if status["device_count"]:
                status["device_name"] = str(torch.cuda.get_device_name(0))
        if deep:
            if status["cuda_available"]:
                value = (torch.ones(1, device="cuda:0") + 1).cpu().item()
                status["probe_ok"] = value == 2
            else:
                value = (torch.ones(1, device="cpu") + 1).item()
                status["probe_ok"] = value == 2
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        if deep:
            status["probe_ok"] = False
    return _finalize_engine_status(status, deep)


def _paddle_status(deep: bool) -> Dict[str, Any]:
    cpu_distribution = _package_version("paddlepaddle")
    gpu_distribution = _package_version("paddlepaddle-gpu")
    status: Dict[str, Any] = {
        "installed": False,
        "version": gpu_distribution or cpu_distribution,
        "distribution": "paddlepaddle-gpu" if gpu_distribution else (
            "paddlepaddle" if cpu_distribution else None
        ),
        "conflicting_distributions": bool(cpu_distribution and gpu_distribution),
        "cuda_compiled": False,
        "cuda_available": False,
        "device_visible": False,
        "compute_ready": False,
        "device_count": 0,
        "device": None,
        "probe_ok": None,
        "failure_code": None,
        "error": None,
    }
    if status["version"] is None:
        return _finalize_engine_status(status, deep)

    # A frozen ONEDIR executable is an application, not a Python interpreter;
    # invoking ``AI_CAM.exe -c <code>`` cannot run the source-mode subprocess
    # probe. dependency_diagnostics() has already imported the bundled Paddle
    # module at this startup gate, so inspect that same runtime here. The
    # canonical --dry-run still initializes and executes Paddle through the
    # production isolated OCR worker processes.
    if getattr(sys, "frozen", False):
        try:
            import paddle

            status["installed"] = True
            status["version"] = str(paddle.__version__)
            status["cuda_compiled"] = bool(
                paddle.device.is_compiled_with_cuda()
            )
            try:
                status["device"] = str(paddle.device.get_device())
            except Exception:
                pass
            if status["cuda_compiled"]:
                try:
                    status["device_count"] = int(
                        paddle.device.cuda.device_count()
                    )
                except Exception:
                    pass
            status["cuda_available"] = bool(
                status["cuda_compiled"] and status["device_count"] > 0
            )
            if deep:
                previous = status["device"] or "cpu"
                target = "gpu:0" if status["cuda_available"] else "cpu"
                try:
                    paddle.device.set_device(target)
                    value = float(
                        (paddle.ones([1], dtype="float32") + 1).numpy()[0]
                    )
                    matrix = paddle.ones([2, 2], dtype="float32")
                    matmul_value = float(
                        paddle.matmul(matrix, matrix).numpy()[0][0]
                    )
                    image = paddle.ones([1, 1, 3, 3], dtype="float32")
                    kernel = paddle.ones([1, 1, 2, 2], dtype="float32")
                    conv_value = float(
                        paddle.nn.functional.conv2d(image, kernel)
                        .numpy()[0][0][0][0]
                    )
                    status["probe_ok"] = (
                        value == 2.0
                        and matmul_value == 2.0
                        and conv_value == 4.0
                    )
                finally:
                    try:
                        paddle.device.set_device(previous)
                    except Exception:
                        pass
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"
            if deep:
                status["probe_ok"] = False
        return _finalize_engine_status(status, deep)

    # Paddle stays isolated from the parent process.  The OCR implementation
    # intentionally imports Paddle only in its spawned worker, because loading
    # Torch/YOLO and Paddle native runtimes into one process is less stable.
    probe = r'''
import json
out = {"installed": False, "version": None, "cuda_compiled": False,
       "cuda_available": False, "device_count": 0, "device": None,
       "probe_ok": None, "error": None}
try:
    import paddle
    out["installed"] = True
    out["version"] = str(paddle.__version__)
    out["cuda_compiled"] = bool(paddle.device.is_compiled_with_cuda())
    try:
        out["device"] = str(paddle.device.get_device())
    except Exception:
        pass
    if out["cuda_compiled"]:
        try:
            out["device_count"] = int(paddle.device.cuda.device_count())
        except Exception:
            pass
    out["cuda_available"] = bool(out["cuda_compiled"] and out["device_count"] > 0)
    if __import__("os").environ.get("AI_CAM_DEEP_PROBE") == "1":
        previous = out["device"] or "cpu"
        target = "gpu:0" if out["cuda_available"] else "cpu"
        try:
            paddle.device.set_device(target)
            value = float((paddle.ones([1], dtype="float32") + 1).numpy()[0])
            matrix = paddle.ones([2, 2], dtype="float32")
            matmul_value = float(paddle.matmul(matrix, matrix).numpy()[0][0])
            image = paddle.ones([1, 1, 3, 3], dtype="float32")
            kernel = paddle.ones([1, 1, 2, 2], dtype="float32")
            conv_value = float(
                paddle.nn.functional.conv2d(image, kernel).numpy()[0][0][0][0]
            )
            out["probe_ok"] = (
                value == 2.0 and matmul_value == 2.0 and conv_value == 4.0
            )
        finally:
            try:
                paddle.device.set_device(previous)
            except Exception:
                pass
except Exception as exc:
    out["error"] = type(exc).__name__ + ": " + str(exc)
    if __import__("os").environ.get("AI_CAM_DEEP_PROBE") == "1":
        out["probe_ok"] = False
print("AI_CAM_PADDLE_PROBE=" + json.dumps(out, ensure_ascii=True))
'''
    try:
        env = native_subprocess_env()
        env["AI_CAM_DEEP_PROBE"] = "1" if deep else "0"
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=env,
        )
        marker = "AI_CAM_PADDLE_PROBE="
        payload = next(
            (line[len(marker):] for line in reversed(completed.stdout.splitlines())
             if line.startswith(marker)),
            None,
        )
        if payload is None:
            detail = (completed.stderr or completed.stdout or "no probe output").strip()
            raise RuntimeError(detail[-1000:])
        child = json.loads(payload)
        for key in ("installed", "version", "cuda_compiled", "cuda_available",
                    "device_count", "device", "probe_ok", "error"):
            status[key] = child.get(key)
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        if deep:
            status["probe_ok"] = False
    return _finalize_engine_status(status, deep)


def collect_runtime_diagnostics(deep: bool = False) -> Dict[str, Any]:
    """Return a secret-free, JSON-serializable runtime capability report."""
    nvidia = _nvidia_smi()
    torch = _torch_status(deep=deep)
    paddle = _paddle_status(deep=deep)
    # Tests and third-party callers may supply the pre-v2 shape. Normalize it
    # here so readiness never accidentally falls back to raw CUDA visibility.
    torch = _finalize_engine_status(torch, deep)
    paddle = _finalize_engine_status(paddle, deep)
    warnings = []

    if not torch["installed"]:
        warnings.append("PyTorch is not installed; YOLO cannot start.")
    if not paddle["installed"]:
        warnings.append("PaddlePaddle is not installed; OCR cannot start.")
    if paddle.get("conflicting_distributions"):
        warnings.append("Both paddlepaddle and paddlepaddle-gpu are installed; reinstall one profile.")
    if nvidia["hardware_present"] and not torch["device_visible"]:
        warnings.append("NVIDIA GPU detected, but the installed PyTorch build cannot use CUDA.")
    if nvidia["hardware_present"] and not paddle["device_visible"]:
        warnings.append("NVIDIA GPU detected, but the installed Paddle build cannot use CUDA.")
    if torch["device_visible"] != paddle["device_visible"]:
        warnings.append("YOLO and OCR do not have the same CUDA capability.")
    if deep and torch["probe_ok"] is False:
        warnings.append("PyTorch compute probe failed.")
    if deep and paddle["probe_ok"] is False:
        warnings.append("Paddle compute probe failed.")

    both_gpu = bool(torch["compute_ready"] and paddle["compute_ready"])
    profile = "gpu" if both_gpu else "cpu"
    if nvidia["hardware_present"] and (torch["compute_ready"] != paddle["compute_ready"]):
        profile = "mixed"
    elif nvidia["hardware_present"] and not both_gpu:
        profile = "cpu-fallback"

    report: Dict[str, Any] = {
        "schema_version": 2,
        "system": {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "packages": {
            "ultralytics": _package_version("ultralytics"),
            "paddleocr": _package_version("paddleocr"),
            "opencv": _package_version("opencv-python"),
        },
        "nvidia": nvidia,
        "torch": torch,
        "paddle": paddle,
        "effective_profile": profile,
        "gpu_pipeline_ready": both_gpu,
        "base_runtime_ready": bool(torch["installed"] and paddle["installed"]),
        "deep_probe": bool(deep),
        "warnings": warnings,
    }
    return report


def get_runtime_diagnostics(refresh: bool = False, deep: bool = False) -> Dict[str, Any]:
    global _RUNTIME_CACHE
    with _CACHE_LOCK:
        if refresh or _RUNTIME_CACHE is None or (deep and not _RUNTIME_CACHE.get("deep_probe")):
            _RUNTIME_CACHE = collect_runtime_diagnostics(deep=deep)
        return deepcopy(_RUNTIME_CACHE)


def apply_safe_runtime_policy(ocr_config: Any) -> Dict[str, Any]:
    """Apply a verified GPU policy, preserving an explicit CPU fallback path."""
    requested = bool(getattr(ocr_config, "use_gpu", False))
    explicit_required = _env_true("AI_CAM_REQUIRE_GPU")
    # Any requested GPU path gets a real tensor probe. This prevents a visible
    # device with a broken cuDNN ABI from being cached as production-ready.
    report = get_runtime_diagnostics(
        refresh=True, deep=bool(requested or explicit_required)
    )
    report["torch"] = _finalize_engine_status(
        report.get("torch", {}), bool(report.get("deep_probe"))
    )
    report["paddle"] = _finalize_engine_status(
        report.get("paddle", {}), bool(report.get("deep_probe"))
    )
    linux_host = report.get("system", {}).get("os") == "Linux"
    allow_cpu_fallback = _env_true("AI_CAM_ALLOW_CPU_FALLBACK")
    gpu_required = bool(
        explicit_required or (requested and linux_host and not allow_cpu_fallback)
    )
    effective = bool(requested and report["paddle"].get("compute_ready"))
    fallback_applied = bool(requested and not effective and not gpu_required)
    if fallback_applied:
        setattr(ocr_config, "use_gpu", False)
    failure_codes = sorted({
        str(engine.get("failure_code"))
        for engine in (report.get("torch", {}), report.get("paddle", {}))
        if engine.get("failure_code")
    })
    if gpu_required and not report.get("nvidia", {}).get("hardware_present"):
        failure_codes.append("NVIDIA_NOT_DETECTED")
    if gpu_required and not report.get("torch", {}).get("device_visible"):
        failure_codes.append("TORCH_CUDA_NOT_AVAILABLE")
    if gpu_required and not report.get("paddle", {}).get("device_visible"):
        failure_codes.append("PADDLE_CUDA_NOT_AVAILABLE")
    if gpu_required and not requested:
        failure_codes.append("OCR_GPU_DISABLED")
    failure_codes = sorted(set(failure_codes))
    report["policy"] = {
        "ocr_gpu_requested": requested,
        "ocr_gpu_effective": effective,
        "gpu_required": gpu_required,
        "cpu_fallback_allowed": allow_cpu_fallback,
        "cpu_fallback_applied": fallback_applied,
        "startup_ready": bool(
            not gpu_required
            or (report.get("gpu_pipeline_ready") and effective)
        ),
        "failure_codes": failure_codes,
    }
    global _RUNTIME_CACHE
    with _CACHE_LOCK:
        _RUNTIME_CACHE = deepcopy(report)
    return report


def record_ocr_preload(report: Dict[str, Any], ready: bool) -> Dict[str, Any]:
    """Attach the real production OCR preload result and refresh the cache."""
    updated = deepcopy(report)
    updated["ocr_preload"] = {
        "attempted": True,
        "ready": bool(ready),
        "failure_code": None if ready else "OCR_PRELOAD_FAILED",
    }
    policy = updated.setdefault("policy", {})
    runtime_ready = bool(
        not policy.get("gpu_required")
        or (
            updated.get("gpu_pipeline_ready")
            and policy.get("ocr_gpu_effective")
        )
    )
    policy["startup_ready"] = bool(runtime_ready and ready)
    codes = set(policy.get("failure_codes") or [])
    if not ready:
        codes.add("OCR_PRELOAD_FAILED")
    policy["failure_codes"] = sorted(codes)
    global _RUNTIME_CACHE
    with _CACHE_LOCK:
        _RUNTIME_CACHE = deepcopy(updated)
    return updated


def enforce_runtime_startup(report: Dict[str, Any]) -> None:
    """Fail closed when the host policy requires a verified GPU pipeline."""
    policy = report.get("policy", {})
    if policy.get("gpu_required") and not policy.get("startup_ready"):
        codes = policy.get("failure_codes") or ["GPU_RUNTIME_NOT_READY"]
        raise RuntimeReadinessError(
            "Production GPU runtime is not ready: " + ", ".join(codes)
        )


def evaluate_runtime(report: Dict[str, Any], require_gpu: bool = False) -> Tuple[bool, list[str]]:
    """Evaluate a report for CLI/deployment gates without changing runtime state."""
    failures = []
    system = report.get("system", {})
    try:
        py = tuple(int(part) for part in str(system.get("python", "0.0")).split(".")[:2])
    except ValueError:
        py = (0, 0)
    if not ((3, 10) <= py < (3, 13)):
        failures.append("Python 3.10-3.12 is required by the tested engine wheels.")
    if not report.get("base_runtime_ready"):
        failures.append("PyTorch and PaddlePaddle must both be importable.")
    if report.get("deep_probe"):
        for engine in ("torch", "paddle"):
            if report.get(engine, {}).get("probe_ok") is not True:
                failures.append(f"{engine} compute probe did not pass.")
    if require_gpu:
        if not report.get("nvidia", {}).get("hardware_present"):
            failures.append("NVIDIA GPU/driver was not detected by nvidia-smi.")
        if not report.get("gpu_pipeline_ready"):
            failures.append("Both PyTorch and PaddlePaddle must pass CUDA compute probes.")
    return not failures, failures


def log_runtime_report(report: Dict[str, Any], logger: Any) -> None:
    """Write concise startup diagnostics through the application logger."""
    torch = report["torch"]
    paddle = report["paddle"]
    nvidia = report["nvidia"]
    policy = report.get("policy", {})
    logger.info(
        "[RUNTIME] profile=%s os=%s python=%s nvidia=%s torch=%s "
        "torch_visible=%s torch_compute=%s paddle=%s paddle_visible=%s "
        "paddle_compute=%s ocr_gpu=%s",
        report["effective_profile"],
        report["system"]["os"],
        report["system"]["python"],
        nvidia.get("hardware_present"),
        torch.get("version"),
        torch.get("device_visible"),
        torch.get("compute_ready"),
        paddle.get("version"),
        paddle.get("device_visible"),
        paddle.get("compute_ready"),
        policy.get("ocr_gpu_effective", paddle.get("compute_ready")),
    )
    for warning in report.get("warnings", []):
        logger.warning("[RUNTIME] %s", warning)
    for code in policy.get("failure_codes", []):
        logger.error("[RUNTIME] readiness failure: %s", code)
    if policy.get("cpu_fallback_applied"):
        logger.warning(
            "[RUNTIME] ocr.use_gpu requested but Paddle CUDA is unavailable; "
            "safe CPU fallback was applied for this process."
        )


def runtime_health_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return the public, path/error-free subset used by ``/health``."""
    system = report.get("system", {})
    nvidia = report.get("nvidia", {})
    torch = report.get("torch", {})
    paddle = report.get("paddle", {})
    policy = report.get("policy", {})
    preload = report.get("ocr_preload", {})
    failure_codes = sorted({
        *(policy.get("failure_codes") or []),
        *(code for code in (
            torch.get("failure_code"), paddle.get("failure_code"),
            preload.get("failure_code"),
        ) if code),
    })
    critical = bool(
        (
            policy.get("gpu_required")
            and (
                not report.get("gpu_pipeline_ready")
                or not policy.get("ocr_gpu_effective")
            )
        )
        or (preload.get("attempted") and not preload.get("ready"))
    )
    return {
        "status": "critical" if critical else ("degraded" if (
            policy.get("cpu_fallback_applied")
            or report.get("effective_profile") == "mixed"
        ) else "ok"),
        "ready": not critical,
        "failure_codes": failure_codes,
        "effective_profile": report.get("effective_profile"),
        "gpu_pipeline_ready": bool(report.get("gpu_pipeline_ready")),
        "system": {
            "os": system.get("os"),
            "release": system.get("release"),
            "machine": system.get("machine"),
            "python": system.get("python"),
        },
        "nvidia": {
            "hardware_present": bool(nvidia.get("hardware_present")),
            "driver_version": nvidia.get("driver_version"),
            "driver_cuda_max": nvidia.get("driver_cuda_max"),
            "gpu_names": [gpu.get("name") for gpu in nvidia.get("gpus", [])],
        },
        "torch": {
            "version": torch.get("version"),
            "cuda_build": torch.get("cuda_build"),
            "cuda_available": bool(torch.get("cuda_available")),
            "device_visible": bool(torch.get("device_visible")),
            "compute_ready": bool(torch.get("compute_ready")),
            "probe_ok": torch.get("probe_ok"),
            "failure_code": torch.get("failure_code"),
            "device_count": int(torch.get("device_count") or 0),
        },
        "paddle": {
            "version": paddle.get("version"),
            "distribution": paddle.get("distribution"),
            "cuda_compiled": bool(paddle.get("cuda_compiled")),
            "cuda_available": bool(paddle.get("cuda_available")),
            "device_visible": bool(paddle.get("device_visible")),
            "compute_ready": bool(paddle.get("compute_ready")),
            "probe_ok": paddle.get("probe_ok"),
            "failure_code": paddle.get("failure_code"),
            "device_count": int(paddle.get("device_count") or 0),
        },
        "policy": {
            "ocr_gpu_requested": policy.get("ocr_gpu_requested"),
            "ocr_gpu_effective": policy.get("ocr_gpu_effective"),
            "gpu_required": bool(policy.get("gpu_required")),
            "cpu_fallback_allowed": bool(policy.get("cpu_fallback_allowed")),
            "cpu_fallback_applied": bool(policy.get("cpu_fallback_applied")),
            "startup_ready": policy.get("startup_ready"),
        },
        "ocr_preload": {
            "attempted": bool(preload.get("attempted")),
            "ready": preload.get("ready"),
            "failure_code": preload.get("failure_code"),
        },
    }


def report_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
