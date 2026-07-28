# AI_CAM NVIDIA Profile

The CPU profile is the safe default. GPU is optional and is not inferred from
a PCI device alone.

Prerequisites:

```bash
nvidia-smi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
```

Then:

```bash
cd /opt/ai-cam
sudo ./scripts/setup_ubuntu_26.sh --gpu
```

The installer keeps Torch 2.4.1/torchvision 0.19.1 and uses the official CUDA
12.1 PyTorch wheel channel. PaddleOCR remains 2.10.0; Paddle GPU uses the
official Paddle 2.6.1 CUDA 11.8/cuDNN-in wheel rather than upgrading to Paddle
3. It runs real `torch.cuda.is_available()` and
`paddle.device.is_compiled_with_cuda()` tests. If installation or capability
verification fails, it restores CPU engines and exits nonzero.

Official compatibility references:

- <https://pytorch.org/get-started/previous-versions/>
- <https://www.paddlepaddle.org.cn/documentation/docs/en/2.6/install/pip/linux-pip_en.html>

Verify:

```bash
sudo -u aicam /opt/ai-cam/.venv/bin/python - <<'PY'
import paddle, torch
print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))
print(paddle.__version__, paddle.device.is_compiled_with_cuda(), paddle.device.get_device())
PY
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --self-check --deep --require-gpu
```

For a GPU-required site, set `AI_CAM_ALLOW_CPU_FALLBACK=0`. Monitor
`nvidia-smi`, journal runtime diagnostics, temperature, memory, and driver
errors. A successful install is not live-line validation.
