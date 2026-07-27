"""Cross-platform compute profile and public diagnostic tests."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.native_runtime as native_runtime
import backend.runtime as runtime
from tools import install_runtime


def _report(*, hardware: bool, torch_cuda: bool, paddle_cuda: bool, deep: bool = False):
    return {
        "schema_version": 1,
        "system": {
            "os": "Linux",
            "release": "future-release",
            "machine": "x86_64",
            "python": "3.11.9",
            "executable": "/secret/path/.venv/bin/python",
        },
        "packages": {"ultralytics": "x", "paddleocr": "y", "opencv": "z"},
        "nvidia": {
            "hardware_present": hardware,
            "gpus": [{"name": "Test GPU", "driver_version": "1", "memory_mib": 1}]
            if hardware else [],
            "driver_version": "1" if hardware else None,
            "driver_cuda_max": "12.0" if hardware else None,
            "error": "must not be public",
        },
        "torch": {
            "installed": True,
            "version": "test",
            "cuda_build": "11.8" if torch_cuda else None,
            "cuda_available": torch_cuda,
            "device_visible": torch_cuda,
            "compute_ready": bool(torch_cuda and deep),
            "device_count": 1 if torch_cuda else 0,
            "device_name": "Test GPU" if torch_cuda else None,
            "probe_ok": True if deep else None,
            "failure_code": None,
            "error": None,
        },
        "paddle": {
            "installed": True,
            "version": "test",
            "distribution": "paddlepaddle-gpu" if paddle_cuda else "paddlepaddle",
            "conflicting_distributions": False,
            "cuda_compiled": paddle_cuda,
            "cuda_available": paddle_cuda,
            "device_visible": paddle_cuda,
            "compute_ready": bool(paddle_cuda and deep),
            "device_count": 1 if paddle_cuda else 0,
            "device": "gpu:0" if paddle_cuda else "cpu",
            "probe_ok": True if deep else None,
            "failure_code": None,
            "error": None,
        },
        "effective_profile": "gpu" if torch_cuda and paddle_cuda else (
            "cpu-fallback" if hardware else "cpu"
        ),
        "gpu_pipeline_ready": torch_cuda and paddle_cuda,
        "base_runtime_ready": True,
        "deep_probe": deep,
        "warnings": [],
    }


@pytest.fixture(autouse=True)
def _restore_runtime_cache():
    old = runtime._RUNTIME_CACHE
    yield
    runtime._RUNTIME_CACHE = old


def test_collect_marks_hardware_with_cpu_wheels_as_fallback(monkeypatch):
    monkeypatch.setattr(runtime, "_nvidia_smi", lambda: _report(
        hardware=True, torch_cuda=False, paddle_cuda=False
    )["nvidia"])
    monkeypatch.setattr(runtime, "_torch_status", lambda deep: _report(
        hardware=True, torch_cuda=False, paddle_cuda=False
    )["torch"])
    monkeypatch.setattr(runtime, "_paddle_status", lambda deep: _report(
        hardware=True, torch_cuda=False, paddle_cuda=False
    )["paddle"])

    report = runtime.collect_runtime_diagnostics()

    assert report["effective_profile"] == "cpu-fallback"
    assert report["gpu_pipeline_ready"] is False
    assert any("PyTorch" in warning for warning in report["warnings"])
    assert any("Paddle" in warning for warning in report["warnings"])


def test_non_deep_visibility_is_not_compute_readiness(monkeypatch):
    report = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=False)
    monkeypatch.setattr(runtime, "_nvidia_smi", lambda: report["nvidia"])
    monkeypatch.setattr(runtime, "_torch_status", lambda deep: report["torch"])
    monkeypatch.setattr(runtime, "_paddle_status", lambda deep: report["paddle"])

    diagnosed = runtime.collect_runtime_diagnostics(deep=False)

    assert diagnosed["torch"]["device_visible"] is True
    assert diagnosed["paddle"]["device_visible"] is True
    assert diagnosed["torch"]["compute_ready"] is False
    assert diagnosed["paddle"]["compute_ready"] is False
    assert diagnosed["gpu_pipeline_ready"] is False


def test_safe_policy_disables_unusable_requested_paddle_gpu_with_explicit_fallback(monkeypatch):
    report = _report(hardware=True, torch_cuda=False, paddle_cuda=False)
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda **kwargs: report.copy())
    monkeypatch.setenv("AI_CAM_ALLOW_CPU_FALLBACK", "1")
    config = SimpleNamespace(use_gpu=True)

    applied = runtime.apply_safe_runtime_policy(config)

    assert config.use_gpu is False
    assert applied["policy"]["ocr_gpu_requested"] is True
    assert applied["policy"]["ocr_gpu_effective"] is False
    assert applied["policy"]["gpu_required"] is False
    assert applied["policy"]["cpu_fallback_allowed"] is True
    assert applied["policy"]["cpu_fallback_applied"] is True


def test_linux_nvidia_requested_gpu_fails_closed_by_default(monkeypatch):
    report = _report(hardware=True, torch_cuda=False, paddle_cuda=False, deep=True)
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda **kwargs: report.copy())
    monkeypatch.delenv("AI_CAM_ALLOW_CPU_FALLBACK", raising=False)
    config = SimpleNamespace(use_gpu=True)

    applied = runtime.apply_safe_runtime_policy(config)

    assert config.use_gpu is True
    assert applied["policy"]["gpu_required"] is True
    assert applied["policy"]["startup_ready"] is False
    with pytest.raises(runtime.RuntimeReadinessError):
        runtime.enforce_runtime_startup(applied)


def test_linux_requested_gpu_fails_closed_when_driver_is_missing(monkeypatch):
    report = _report(hardware=False, torch_cuda=False, paddle_cuda=False, deep=True)
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda **kwargs: report.copy())
    monkeypatch.delenv("AI_CAM_ALLOW_CPU_FALLBACK", raising=False)

    applied = runtime.apply_safe_runtime_policy(SimpleNamespace(use_gpu=True))

    assert applied["policy"]["gpu_required"] is True
    assert "NVIDIA_NOT_DETECTED" in applied["policy"]["failure_codes"]
    with pytest.raises(runtime.RuntimeReadinessError):
        runtime.enforce_runtime_startup(applied)


def test_safe_policy_keeps_gpu_when_both_host_and_paddle_work(monkeypatch):
    report = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=True)
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda **kwargs: report.copy())
    config = SimpleNamespace(use_gpu=True)

    applied = runtime.apply_safe_runtime_policy(config)

    assert config.use_gpu is True
    assert applied["policy"]["ocr_gpu_effective"] is True
    runtime.enforce_runtime_startup(applied)


def test_explicit_gpu_requirement_rejects_disabled_ocr_gpu(monkeypatch):
    report = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=True)
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda **kwargs: report.copy())
    monkeypatch.setenv("AI_CAM_REQUIRE_GPU", "1")
    config = SimpleNamespace(use_gpu=False)

    applied = runtime.apply_safe_runtime_policy(config)

    assert applied["policy"]["startup_ready"] is False
    assert "OCR_GPU_DISABLED" in applied["policy"]["failure_codes"]
    with pytest.raises(runtime.RuntimeReadinessError):
        runtime.enforce_runtime_startup(applied)


def test_deep_probe_failure_is_not_gpu_ready_and_classifies_cudnn(monkeypatch):
    report = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=True)
    report["paddle"]["probe_ok"] = False
    report["paddle"]["error"] = (
        "RuntimeError: Cannot load cudnn shared library; cudnnGetVersion failed"
    )
    monkeypatch.setattr(runtime, "_nvidia_smi", lambda: report["nvidia"])
    monkeypatch.setattr(runtime, "_torch_status", lambda deep: report["torch"])
    monkeypatch.setattr(runtime, "_paddle_status", lambda deep: report["paddle"])

    diagnosed = runtime.collect_runtime_diagnostics(deep=True)

    assert diagnosed["paddle"]["device_visible"] is True
    assert diagnosed["paddle"]["compute_ready"] is False
    assert diagnosed["paddle"]["failure_code"] == "CUDNN_LOAD_FAILED"
    assert diagnosed["gpu_pipeline_ready"] is False
    assert diagnosed["effective_profile"] == "mixed"


def test_missing_transitive_cublas_is_classified_separately():
    error = (
        "Could not load library libcudnn_ops_infer.so.8. Error: "
        "libcublas.so.11: cannot open shared object file: No such file or directory"
    )
    assert runtime._failure_code(error, False) == "CUDA_LIBRARY_LOAD_FAILED"


def test_missing_transitive_nvrtc_is_classified_separately():
    error = (
        "Could not load library libcudnn_ops_infer.so.8. Error: "
        "libnvrtc.so: cannot open shared object file: No such file or directory"
    )
    assert runtime._failure_code(error, False) == "CUDA_LIBRARY_LOAD_FAILED"


def test_native_cuda_discovery_excludes_torch_cudnn(tmp_path):
    site_packages = tmp_path / "site-packages"
    cublas = site_packages / "nvidia" / "cublas" / "lib"
    cudnn = site_packages / "nvidia" / "cudnn" / "lib"
    cublas.mkdir(parents=True)
    cudnn.mkdir(parents=True)
    (cublas / "libcublas.so.11").touch()
    (cudnn / "libcudnn.so.9").touch()

    found = native_runtime.discover_nvidia_library_dirs([site_packages])

    assert cublas.resolve() in found
    assert cudnn.resolve() not in found


def test_versioned_nvrtc_target_is_found_for_unversioned_alias(tmp_path):
    nvrtc = tmp_path / "nvidia" / "cuda_nvrtc" / "lib"
    nvrtc.mkdir(parents=True)
    older = nvrtc / "libnvrtc.so.11.1"
    newer = nvrtc / "libnvrtc.so.11.2"
    older.touch()
    newer.touch()

    assert native_runtime.find_versioned_cuda_library(
        "libnvrtc.so", [nvrtc]
    ) == newer


def test_all_versioned_cuda_libs_get_aliases_but_cudnn_is_excluded(tmp_path):
    cuda_lib = tmp_path / "cuda" / "lib"
    cuda_lib.mkdir(parents=True)
    nvrtc = cuda_lib / "libnvrtc.so.11.2"
    cublas = cuda_lib / "libcublas.so.11"
    cublas_lt = cuda_lib / "libcublasLt.so.11"
    cudnn = cuda_lib / "libcudnn.so.9"
    for path in (nvrtc, cublas, cublas_lt, cudnn):
        path.touch()

    aliases = native_runtime.cuda_compatibility_alias_targets([cuda_lib])

    assert aliases["libnvrtc.so"] == nvrtc
    assert aliases["libcublas.so"] == cublas
    assert aliases["libcublasLt.so"] == cublas_lt
    assert "libcudnn.so" not in aliases


def test_native_subprocess_env_prepends_cuda_dirs_and_is_idempotent(monkeypatch):
    cuda_dir = Path("/venv/site-packages/nvidia/cublas/lib")
    monkeypatch.setattr(native_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        native_runtime, "discover_nvidia_library_dirs", lambda: [cuda_dir]
    )

    first = native_runtime.native_subprocess_env(
        {"LD_LIBRARY_PATH": "/existing/lib"}
    )
    second = native_runtime.native_subprocess_env(first)

    assert first["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(cuda_dir), "/existing/lib",
    ]
    assert second["LD_LIBRARY_PATH"] == first["LD_LIBRARY_PATH"]


def test_preload_failure_is_readiness_failure_without_native_error_leak():
    report = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=True)
    report["policy"] = {
        "ocr_gpu_requested": True,
        "ocr_gpu_effective": True,
        "gpu_required": True,
        "startup_ready": True,
        "failure_codes": [],
    }
    updated = runtime.record_ocr_preload(report, False)
    summary = runtime.runtime_health_summary(updated)

    assert summary["status"] == "critical"
    assert summary["ready"] is False
    assert "OCR_PRELOAD_FAILED" in summary["failure_codes"]
    with pytest.raises(runtime.RuntimeReadinessError):
        runtime.enforce_runtime_startup(updated)


def test_require_gpu_is_a_deployment_gate():
    cpu = _report(hardware=True, torch_cuda=False, paddle_cuda=False, deep=True)
    ok, failures = runtime.evaluate_runtime(cpu, require_gpu=True)
    assert ok is False
    assert any("CUDA" in failure for failure in failures)

    gpu = _report(hardware=True, torch_cuda=True, paddle_cuda=True, deep=True)
    assert runtime.evaluate_runtime(gpu, require_gpu=True) == (True, [])


def test_public_health_summary_does_not_expose_paths_or_native_errors():
    report = _report(hardware=True, torch_cuda=False, paddle_cuda=False)
    report["policy"] = {
        "ocr_gpu_requested": True,
        "ocr_gpu_effective": False,
        "cpu_fallback_applied": True,
    }
    summary = runtime.runtime_health_summary(report)
    rendered = repr(summary)
    assert "/secret/path" not in rendered
    assert "must not be public" not in rendered
    assert summary["status"] == "degraded"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://download.pytorch.org/whl/cu118", 11.8),
        ("https://example.invalid/cu126/", 12.6),
        ("https://example.invalid/cuda11.8/wheel.whl", 11.8),
        ("https://example.invalid/cpu", None),
    ],
)
def test_cuda_channel_parser(url, expected):
    assert install_runtime._cuda_required_by_index(url) == expected


def test_curated_engine_pins_are_single_source():
    versions = install_runtime._engine_versions()
    assert versions == {
        "torch": "2.4.1",
        "torchvision": "0.19.1",
        "paddlepaddle": "2.6.2",
    }


def test_gpu_paddle_pin_is_independent_and_exact_for_python_abi():
    url = install_runtime._default_paddle_gpu_wheel_url()
    tag = f"cp{install_runtime.sys.version_info.major}{install_runtime.sys.version_info.minor}"
    assert install_runtime.PADDLE_GPU_VERSION == "2.6.1"
    assert "linux-gpu-cuda11.8-cudnn8.6" in url
    assert url.endswith(f"paddlepaddle_gpu-2.6.1-{tag}-{tag}-linux_x86_64.whl")
    install_runtime._validate_paddle_wheel_url(url)


def test_paddle_idempotency_requires_real_gpu_probe(monkeypatch):
    monkeypatch.setattr(
        install_runtime, "_installed_version",
        lambda name: "2.6.1" if name == "paddlepaddle-gpu" else None,
    )
    monkeypatch.setattr(install_runtime, "_probe_script", lambda script: False)
    assert install_runtime._paddle_gpu_satisfied() is False
    scripts = []
    monkeypatch.setattr(
        install_runtime, "_probe_script", lambda script: scripts.append(script) or True
    )
    assert install_runtime._paddle_gpu_satisfied() is True
    assert "paddle.matmul" in scripts[0]
    assert "paddle.nn.functional.conv2d" in scripts[0]


def test_runtime_deep_paddle_probe_exercises_cublas_and_cudnn():
    import inspect

    source = inspect.getsource(runtime._paddle_status)
    assert "paddle.matmul" in source
    assert "paddle.nn.functional.conv2d" in source
