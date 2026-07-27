"""Install one clean AI_CAM runtime profile.

Examples:
    python tools/install_runtime.py --profile cpu
    python tools/install_runtime.py --profile gpu

The GPU profile is selected automatically only on Linux with a working
``nvidia-smi``. PyTorch uses the tested cu118 channel; Paddle is pinned to the
official 2.6.1 CUDA 11.8/cuDNN 8.6 cudnn-in wheel required by PaddleOCR 2.10.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.native_runtime import (  # noqa: E402
    configure_native_library_path,
    native_subprocess_env,
)

CPU_REQUIREMENTS = ROOT / "requirements" / "runtime-cpu.txt"
BASE_REQUIREMENTS = ROOT / "requirements" / "runtime-base.txt"

DEFAULT_TORCH_GPU_INDEX = os.environ.get(
    "AI_CAM_TORCH_INDEX_URL", "https://download.pytorch.org/whl/cu118"
)
PADDLE_GPU_VERSION = "2.6.1"
PADDLE_GPU_FIND_LINKS = "https://www.paddlepaddle.org.cn/whl/linux/cudnnin/stable.html"
PADDLE_GPU_WHEEL_ROOT = (
    "https://paddle-wheel.bj.bcebos.com/2.6.1/linux-cudnnin/"
    "linux-gpu-cuda11.8-cudnn8.6-mkl-gcc8.2-avx"
)


def _default_paddle_gpu_wheel_url() -> str:
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    filename = f"paddlepaddle_gpu-{PADDLE_GPU_VERSION}-{tag}-{tag}-linux_x86_64.whl"
    return os.environ.get(
        "AI_CAM_PADDLE_WHEEL_URL", f"{PADDLE_GPU_WHEEL_ROOT}/{filename}"
    )


def _engine_versions() -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for raw in CPU_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(torch|torchvision|paddlepaddle)==([^;\s]+)", raw.strip())
        if match and match.group(1) not in versions:
            versions[match.group(1)] = match.group(2).replace("+cpu", "")
    missing = {"torch", "torchvision", "paddlepaddle"} - set(versions)
    if missing:
        raise RuntimeError(f"Engine pins missing from {CPU_REQUIREMENTS}: {sorted(missing)}")
    return versions


def _nvidia_driver_cuda_max() -> Optional[float]:
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=8, check=False
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)?)", result.stdout)
    return float(match.group(1)) if result.returncode == 0 and match else None


def _cuda_required_by_index(url: str) -> Optional[float]:
    match = re.search(r"(?:^|/)cu(\d{2,3})(?:/|$)", url.rstrip("/"))
    if not match:
        explicit = re.search(r"cuda(\d{1,2})\.(\d+)", url)
        if explicit:
            return float(f"{explicit.group(1)}.{explicit.group(2)}")
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 2:
        return float(f"{digits[0]}.{digits[1]}")
    return float(f"{digits[:-1]}.{digits[-1]}")


def resolve_profile(requested: str) -> str:
    if requested != "auto":
        return requested
    if platform.system() == "Linux" and _nvidia_driver_cuda_max() is not None:
        return "gpu"
    return "cpu"


def _validate_paddle_wheel_url(url: str) -> None:
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    expected = f"paddlepaddle_gpu-{PADDLE_GPU_VERSION}-{tag}-{tag}-linux_x86_64.whl"
    if not url.startswith("https://") or not url.rstrip("/").endswith(expected):
        raise RuntimeError(
            "Paddle GPU override must be an HTTPS URL to the exact tested "
            f"{PADDLE_GPU_VERSION} {tag} Linux x86_64 wheel: {expected}"
        )


def validate_host(profile: str, torch_index: str, paddle_wheel: str) -> None:
    py = sys.version_info[:2]
    if not ((3, 10) <= py < (3, 13)):
        raise RuntimeError(
            f"Python {py[0]}.{py[1]} is unsupported by the tested wheels; "
            "create a Python 3.11 virtual environment."
        )
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        raise RuntimeError("Refusing a system-wide install; activate a virtual environment first.")
    if profile != "gpu":
        return
    if platform.system() != "Linux":
        raise RuntimeError(
            "The production GPU profile is supported on Linux. Use the CPU profile on Windows."
        )
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The pinned Paddle CUDA wheel supports Linux x86_64 only.")
    _validate_paddle_wheel_url(paddle_wheel)
    driver_max = _nvidia_driver_cuda_max()
    if driver_max is None:
        raise RuntimeError("nvidia-smi failed; install/repair the NVIDIA driver before Python wheels.")
    for label, url in (("PyTorch", torch_index), ("Paddle", paddle_wheel)):
        required = _cuda_required_by_index(url)
        if required is not None and driver_max < required:
            raise RuntimeError(
                f"NVIDIA driver advertises CUDA {driver_max}, but the {label} wheel "
                f"channel requires CUDA {required}: {url}"
            )


def _run(command: List[str], dry_run: bool) -> None:
    print("+ " + shlex.join(command), flush=True)
    if not dry_run:
        subprocess.run(
            command, cwd=ROOT, check=True, env=native_subprocess_env()
        )


def _installed_version(distribution: str) -> Optional[str]:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _probe_script(script: str) -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True,
            text=True, timeout=45, check=False, env=native_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _torch_gpu_satisfied(version: str) -> bool:
    installed = _installed_version("torch") or ""
    if not (installed == version or installed.startswith(version + "+cu118")):
        return False
    return _probe_script(
        "import torch; assert torch.version.cuda == '11.8'; "
        "assert torch.cuda.is_available(); "
        "assert float((torch.ones(1, device='cuda:0') + 1).cpu().item()) == 2.0"
    )


def _paddle_gpu_satisfied() -> bool:
    if _installed_version("paddlepaddle-gpu") != PADDLE_GPU_VERSION:
        return False
    if _installed_version("paddlepaddle") is not None:
        return False
    # A generic 2.6.1 wheel and the cudnn-in 2.6.1 wheel share a package
    # version. Exercise elementwise, cuBLAS matmul and cuDNN convolution: a
    # simple tensor alone can pass while libcublas.so is still unavailable.
    return _probe_script(
        "import paddle; assert paddle.device.is_compiled_with_cuda(); "
        "assert paddle.device.cuda.device_count() > 0; "
        "paddle.device.set_device('gpu:0'); "
        "assert float((paddle.ones([1], dtype='float32') + 1).numpy()[0]) == 2.0; "
        "m=paddle.ones([2,2], dtype='float32'); "
        "assert float(paddle.matmul(m,m).numpy()[0][0]) == 2.0; "
        "x=paddle.ones([1,1,3,3], dtype='float32'); "
        "w=paddle.ones([1,1,2,2], dtype='float32'); "
        "assert float(paddle.nn.functional.conv2d(x,w).numpy()[0][0][0][0]) == 4.0"
    )


def _install_paddle_gpu(pip: List[str], wheel_url: str, dry_run: bool) -> None:
    direct = pip + ["install", "--upgrade", wheel_url]
    if dry_run:
        _run(direct, True)
        return
    try:
        _run(direct, False)
    except subprocess.CalledProcessError:
        # The fallback cannot silently select 2.6.2/current. Download only the
        # exact wheel from Paddle's official cudnn-in page, validate its ABI
        # filename, then let the local-wheel install resolve normal Python deps.
        with tempfile.TemporaryDirectory(prefix="ai-cam-paddle-") as temp_dir:
            _run(
                pip + [
                    "download", f"paddlepaddle-gpu=={PADDLE_GPU_VERSION}",
                    "--no-index", "--find-links", PADDLE_GPU_FIND_LINKS,
                    "--no-deps", "--dest", temp_dir,
                ],
                False,
            )
            wheels = list(Path(temp_dir).glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("Official Paddle fallback did not yield exactly one wheel.")
            _validate_paddle_wheel_url("https://fallback.invalid/" + wheels[0].name)
            _run(pip + ["install", "--upgrade", str(wheels[0])], False)


def install(args: argparse.Namespace) -> str:
    profile = resolve_profile(args.profile)
    validate_host(profile, args.torch_index_url, args.paddle_wheel_url)
    versions = _engine_versions()
    py = sys.executable
    pip = [py, "-m", "pip"]

    print(
        f"AI_CAM runtime profile={profile} os={platform.system()} "
        f"python={platform.python_version()}",
        flush=True,
    )
    _run(pip + ["install", "--upgrade", "pip"], args.dry_run)

    if profile == "cpu":
        # Paddle CPU and GPU distributions expose the same import package.
        if _installed_version("paddlepaddle-gpu") is not None:
            _run(
                pip + ["uninstall", "-y", "paddlepaddle", "paddlepaddle-gpu"],
                args.dry_run,
            )
        _run(
            pip + ["install", "--upgrade", "-r", str(ROOT / "requirements.txt")],
            args.dry_run,
        )
    else:
        if not _torch_gpu_satisfied(versions["torch"]):
            _run(
                pip
                + [
                    "install", "--upgrade",
                    f"torch=={versions['torch']}",
                    f"torchvision=={versions['torchvision']}",
                    "--index-url", args.torch_index_url,
                ],
                args.dry_run,
            )
        else:
            print("= compatible Torch cu118 tensor probe already passes; keeping install.")

        # Torch's pip CUDA components now exist.  Besides publishing their
        # non-cuDNN directories, this creates venv-local unversioned CUDA
        # aliases (libcublas.so, libnvrtc.so, etc.) for Paddle's bundled
        # cuDNN 8 without exposing Torch's cuDNN 9.
        native_dirs = configure_native_library_path()
        if not args.dry_run:
            if not native_dirs:
                raise RuntimeError(
                    "No pip NVIDIA CUDA library directories were found after "
                    "the Torch cu118 install."
                )
            print(
                "= native CUDA dependency directories enabled: "
                + ", ".join(
                    path.parent.name if path.name == "lib" else path.name
                    for path in native_dirs
                ),
                flush=True,
            )

        if not _paddle_gpu_satisfied():
            _run(
                pip + ["uninstall", "-y", "paddlepaddle", "paddlepaddle-gpu"],
                args.dry_run,
            )
            _install_paddle_gpu(pip, args.paddle_wheel_url, args.dry_run)
        else:
            print("= compatible Paddle 2.6.1 CUDA tensor probe already passes; keeping install.")
        _run(
            pip + ["install", "--upgrade", "-r", str(BASE_REQUIREMENTS)],
            args.dry_run,
        )

    _run(pip + ["check"], args.dry_run)
    _run(
        [py, "-c", "import paddle; paddle.utils.run_check()"],
        args.dry_run,
    )
    check = [py, str(ROOT / "run.py"), "--self-check", "--deep"]
    if profile == "gpu":
        check.append("--require-gpu")
    _run(check, args.dry_run)
    _run(
        [py, str(ROOT / "tools" / "ocr_preload_smoke.py"), "--profile", profile],
        args.dry_run,
    )
    return profile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install a clean AI_CAM runtime profile")
    parser.add_argument(
        "--profile",
        choices=("auto", "cpu", "gpu"),
        default="auto",
        help="auto uses GPU only on Linux when nvidia-smi works (default: auto)",
    )
    parser.add_argument(
        "--torch-index-url",
        default=DEFAULT_TORCH_GPU_INDEX,
        help="official PyTorch CUDA wheel channel (or AI_CAM_TORCH_INDEX_URL)",
    )
    parser.add_argument(
        "--paddle-wheel-url", "--paddle-index-url",
        dest="paddle_wheel_url",
        default=_default_paddle_gpu_wheel_url(),
        help=(
            "exact Paddle 2.6.1 CUDA 11.8/cuDNN 8.6 cudnn-in wheel "
            "(or AI_CAM_PADDLE_WHEEL_URL); old --paddle-index-url is an alias"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="print commands only")
    return parser.parse_args()


if __name__ == "__main__":
    parsed_args = parse_args()
    try:
        selected = install(parsed_args)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"INSTALL FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if parsed_args.dry_run:
        print(f"AI_CAM {selected} runtime install plan validated (dry-run; not installed).")
    else:
        print(f"AI_CAM {selected} runtime installed and verified.")
