#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
APP_ROOT="${AI_CAM_PROJECT_ROOT:-/opt/ai-cam}"
DATA_ROOT="${AI_CAM_DATA_ROOT:-/var/lib/ai-cam}"
LOG_ROOT="${AI_CAM_LOG_ROOT:-/var/log/ai-cam}"
CONFIG_ROOT="/etc/ai-cam"
PROFILE=""
PYTHON_VERSION="3.11.9"
PYTHON_SHA256="9b1e896523fc510691126c864406d9360a3d1e986acbda59cda57b5abda45b87"
PYTHON_PREFIX="${APP_ROOT}/.python/${PYTHON_VERSION}"
BUILD_DIR=""
FILTERED_LOCK=""

cleanup() {
    [[ -z "${FILTERED_LOCK}" ]] || rm -f -- "${FILTERED_LOCK}"
    [[ -z "${BUILD_DIR}" ]] || rm -rf -- "${BUILD_DIR}"
}
trap cleanup EXIT

usage() {
    echo "Usage: sudo $0 --cpu|--gpu"
}

die() {
    echo "[setup] ERROR: $*" >&2
    exit 1
}

for arg in "$@"; do
    case "${arg}" in
        --cpu) PROFILE="cpu" ;;
        --gpu) PROFILE="gpu" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: ${arg}" ;;
    esac
done
[[ -n "${PROFILE}" ]] || { usage; exit 2; }
[[ "$(id -u)" -eq 0 ]] || die "run as root with sudo"
[[ "$(uname -s)" == "Linux" ]] || die "this installer is Linux-only"
[[ "$(uname -m)" == "x86_64" ]] || die "only x86_64/amd64 is supported"
[[ "${SOURCE_ROOT}" == "${APP_ROOT}" ]] || die \
    "extract/copy this release to ${APP_ROOT} before running setup (current: ${SOURCE_ROOT})"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu is required (detected ${ID:-unknown})"
    [[ "${VERSION_ID:-}" == "26.04" ]] || echo \
        "[setup] WARNING: designed for Ubuntu 26.04; detected ${VERSION_ID:-unknown}" >&2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git git-lfs rsync xz-utils \
    sqlite3 libsqlite3-dev libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libncurses-dev libffi-dev liblzma-dev uuid-dev tk-dev \
    libgl1 libglib2.0-0 libgomp1 libsm6 libxext6 \
    iproute2 iputils-ping netcat-openbsd dnsutils lsof procps \
    logrotate nftables
git lfs install --system

if ! id aicam >/dev/null 2>&1; then
    useradd --system --home-dir "${DATA_ROOT}" --create-home \
        --shell /usr/sbin/nologin --user-group aicam
fi
install -d -o root -g root -m 0755 "${APP_ROOT}"
install -d -o root -g aicam -m 0750 "${CONFIG_ROOT}"
install -d -o aicam -g aicam -m 0750 \
    "${DATA_ROOT}" "${DATA_ROOT}/data" "${DATA_ROOT}/crops" \
    "${DATA_ROOT}/temp" "${DATA_ROOT}/engraved_ocr_collection" \
    "${DATA_ROOT}/temp/paddle" "${DATA_ROOT}/temp/paddleocr" \
    "${DATA_ROOT}/backups" "${DATA_ROOT}/dataset_collected" "${LOG_ROOT}"

if [[ ! -f "${CONFIG_ROOT}/settings.production.yaml" ]]; then
    install -o root -g aicam -m 0640 \
        "${APP_ROOT}/deploy/config/settings.production.yaml" \
        "${CONFIG_ROOT}/settings.production.yaml"
fi
if [[ ! -f "${CONFIG_ROOT}/ai-cam.env" ]]; then
    install -o root -g aicam -m 0640 \
        "${APP_ROOT}/deploy/config/ai-cam.env.example" \
        "${CONFIG_ROOT}/ai-cam.env"
    echo "[setup] Created ${CONFIG_ROOT}/ai-cam.env; populate its secret values before service start."
else
    chown root:aicam "${CONFIG_ROOT}/ai-cam.env"
    chmod 0640 "${CONFIG_ROOT}/ai-cam.env"
fi

find_python311() {
    local candidate
    for candidate in \
        "${PYTHON_PREFIX}/bin/python3.11" \
        /usr/local/bin/python3.11 \
        /usr/bin/python3.11; do
        if [[ -x "${candidate}" ]] && "${candidate}" -c \
            'import struct,sys; raise SystemExit(sys.version_info[:2] != (3,11) or struct.calcsize("P") != 8)'; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python311 || true)"
if [[ -z "${PYTHON_BIN}" ]] && apt-cache show python3.11 >/dev/null 2>&1; then
    apt-get install -y python3.11 python3.11-venv python3.11-dev
    PYTHON_BIN="$(find_python311 || true)"
fi
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[setup] Python 3.11 package unavailable; building verified ${PYTHON_VERSION} without changing /usr/bin/python3."
    BUILD_DIR="$(mktemp -d)"
    curl -fL --retry 3 \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
        -o "${BUILD_DIR}/Python-${PYTHON_VERSION}.tar.xz"
    echo "${PYTHON_SHA256}  ${BUILD_DIR}/Python-${PYTHON_VERSION}.tar.xz" | sha256sum -c -
    tar -xJf "${BUILD_DIR}/Python-${PYTHON_VERSION}.tar.xz" -C "${BUILD_DIR}"
    pushd "${BUILD_DIR}/Python-${PYTHON_VERSION}" >/dev/null
    ./configure --prefix="${PYTHON_PREFIX}" --with-ensurepip=install --enable-optimizations
    make -j"$(nproc)"
    make altinstall
    popd >/dev/null
    PYTHON_BIN="$(find_python311 || true)"
fi
[[ -n "${PYTHON_BIN}" ]] || die "could not provision a 64-bit CPython 3.11"
"${PYTHON_BIN}" -c \
    'import struct,sys; assert sys.version_info[:2] == (3,11); assert struct.calcsize("P") == 8; print(sys.version)'

if [[ -e "${APP_ROOT}/.venv" && ! -x "${APP_ROOT}/.venv/bin/python" ]]; then
    die "${APP_ROOT}/.venv exists but is not a Linux venv; remove it explicitly and rerun"
fi
"${PYTHON_BIN}" -m venv "${APP_ROOT}/.venv"
VENV_PYTHON="${APP_ROOT}/.venv/bin/python"
"${VENV_PYTHON}" -m pip install --upgrade \
    pip==26.1.2 setuptools==75.8.0 wheel==0.45.1

# PaddleOCR declares GUI OpenCV distributions. Install the fully pinned graph
# with its wheel omitted, then add PaddleOCR without dependencies so only the
# headless cv2 wheel is present on the server.
FILTERED_LOCK="$(mktemp)"
grep -v '^paddleocr==' "${APP_ROOT}/requirements-linux-lock.txt" > "${FILTERED_LOCK}"
"${VENV_PYTHON}" -m pip install --no-deps -r "${FILTERED_LOCK}"
"${VENV_PYTHON}" -m pip install --no-deps paddleocr==2.10.0

rollback_cpu_engines() {
    echo "[setup] GPU verification failed; restoring the CPU-safe engine profile." >&2
    "${VENV_PYTHON}" -m pip uninstall -y paddlepaddle-gpu torch torchvision || true
    "${VENV_PYTHON}" -m pip install paddlepaddle==2.6.2
    "${VENV_PYTHON}" -m pip install \
        torch==2.4.1+cpu torchvision==0.19.1+cpu \
        --index-url https://download.pytorch.org/whl/cpu
    echo "cpu" > "${DATA_ROOT}/runtime-profile"
}

if [[ "${PROFILE}" == "gpu" ]]; then
    command -v nvidia-smi >/dev/null 2>&1 || die \
        "--gpu requires a working NVIDIA driver and nvidia-smi"
    nvidia-smi --query-gpu=name,driver_version,memory.total \
        --format=csv,noheader || die "nvidia-smi driver query failed"
    if ! (
        "${VENV_PYTHON}" -m pip uninstall -y paddlepaddle torch torchvision
        "${VENV_PYTHON}" -m pip install \
            torch==2.4.1 torchvision==0.19.1 \
            --index-url https://download.pytorch.org/whl/cu121
        "${VENV_PYTHON}" -m pip install --no-index --no-deps \
            paddlepaddle-gpu==2.6.1 \
            --find-links https://www.paddlepaddle.org.cn/whl/linux/cudnnin/stable.html
        "${VENV_PYTHON}" -c \
            'import paddle,torch; assert torch.cuda.is_available(); assert paddle.device.is_compiled_with_cuda(); print(torch.cuda.get_device_name(0)); print(paddle.device.get_device())'
    ); then
        rollback_cpu_engines
        die "GPU packages or CUDA capability test failed; CPU profile restored"
    fi
else
    "${VENV_PYTHON}" -c \
        'import paddle,torch; print("torch", torch.__version__, "cuda", torch.cuda.is_available()); print("paddle", paddle.__version__)'
fi
echo "${PROFILE}" > "${DATA_ROOT}/runtime-profile"
chown -R aicam:aicam "${DATA_ROOT}" "${LOG_ROOT}"
chmod -R u=rwX,g=rX,o= "${APP_ROOT}/models"

set -a
# shellcheck disable=SC1091
. "${CONFIG_ROOT}/ai-cam.env"
set +a
export AI_CAM_PROJECT_ROOT="${APP_ROOT}"
export AI_CAM_DATA_ROOT="${DATA_ROOT}"
export AI_CAM_LOG_ROOT="${LOG_ROOT}"
export AI_CAM_CONFIG="${CONFIG_ROOT}/settings.production.yaml"
runuser -u aicam --preserve-environment -- \
    "${VENV_PYTHON}" "${APP_ROOT}/run.py" --self-check
runuser -u aicam --preserve-environment -- \
    "${VENV_PYTHON}" "${APP_ROOT}/run.py" --dry-run

"${VENV_PYTHON}" -c \
    'import cv2,onnxruntime,paddle,paddleocr,torch,ultralytics; print({"cv2":cv2.__version__,"onnxruntime":onnxruntime.__version__,"paddle":paddle.__version__,"torch":torch.__version__,"ultralytics":ultralytics.__version__})'
echo "[setup] AI_CAM ${PROFILE} profile installed. Populate secrets, then run scripts/install_systemd.sh."
