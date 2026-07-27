# Runtime dependency policy

`requirements.txt` is the safe CPU entry point. It includes two curated files:

- `runtime-base.txt`: application libraries shared by all hosts;
- `runtime-cpu.txt`: exact, tested CPU engine wheels.

An NVIDIA deployment must use `tools/install_runtime.py --profile gpu`. The
installer reads the same curated files, replaces mutually exclusive CPU/GPU
engines, verifies the actual driver/CUDA capability, runs `pip check`, and then
runs the application self-check plus a real PaddleOCR preload. The GPU Paddle
pin is intentionally independent from `runtime-cpu.txt`: PaddleOCR 2.10.0 uses
the official Paddle GPU 2.6.1 CUDA 11.8/cuDNN 8.6 `cudnn-in` wheel. This avoids
trying to satisfy Paddle's cuDNN 8 ABI with Torch's cuDNN 9 package.

The exact official wheel listing is
<https://www.paddlepaddle.org.cn/whl/linux/cudnnin/stable.html>. Overrides must
remain an HTTPS exact `paddlepaddle-gpu==2.6.1` wheel for the current Python ABI;
the installer will not fall back to an unpinned/current Paddle channel.

Do not commit a `pip freeze` captured from a developer machine as
`requirements.txt`. For deployment evidence, create a clean virtual environment,
install one profile, pass the deep self-check, then archive a host-specific lock:

```text
python -m pip freeze --local > requirements/locks/<os>-<profile>-py311.txt
python -m pip check
python run.py --self-check --deep
```

The lock records one validated deployment artifact; it is not portable between
Windows CPU and Linux CUDA profiles.
