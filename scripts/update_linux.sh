#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="/opt/ai-cam"
DATA_ROOT="${AI_CAM_DATA_ROOT:-/var/lib/ai-cam}"
SOURCE=""
APPLY=0
usage() {
    echo "Usage: sudo $0 --source /path/to/extracted-release --apply"
}
while (($#)); do
    case "$1" in
        --source) SOURCE="${2:-}"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done
[[ "$(id -u)" -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
[[ "${APPLY}" -eq 1 && -n "${SOURCE}" ]] || { usage; exit 2; }
SOURCE="$(readlink -f -- "${SOURCE}")"
[[ -f "${SOURCE}/run.py" && -f "${SOURCE}/VERSION" ]] || {
    echo "Source is not an AI_CAM release" >&2
    exit 1
}
[[ "${SOURCE}" != "${APP_ROOT}" ]] || { echo "Source must be a staging directory" >&2; exit 1; }
[[ -x "${APP_ROOT}/.venv/bin/python" ]] || { echo "Existing venv missing" >&2; exit 1; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_DIR="${DATA_ROOT}/backups/releases"
install -d -o aicam -g aicam -m 0750 "${ROLLBACK_DIR}"
tar --exclude=.venv --exclude=.git --exclude=runtime --exclude=dist --exclude=build \
    -czf "${ROLLBACK_DIR}/ai-cam-code-${STAMP}.tar.gz" -C "${APP_ROOT}" .
"${APP_ROOT}/scripts/backup_database.sh"
systemctl stop ai-cam.service
rsync -a --delete \
    --exclude=.venv --exclude=.git --exclude=runtime --exclude=dist --exclude=build \
    --exclude=deploy/local \
    "${SOURCE}/" "${APP_ROOT}/"

FILTERED_LOCK="$(mktemp)"
trap 'rm -f -- "${FILTERED_LOCK}"' EXIT
grep -Ev '^(paddleocr|paddlepaddle|torch|torchvision)==' \
    "${APP_ROOT}/requirements-linux-lock.txt" > "${FILTERED_LOCK}"
"${APP_ROOT}/.venv/bin/python" -m pip install --no-deps -r "${FILTERED_LOCK}"
"${APP_ROOT}/.venv/bin/python" -m pip install --no-deps paddleocr==2.10.0
set -a
# shellcheck disable=SC1091
. /etc/ai-cam/ai-cam.env
set +a
runuser -u aicam --preserve-environment -- \
    "${APP_ROOT}/.venv/bin/python" "${APP_ROOT}/run.py" --self-check
runuser -u aicam --preserve-environment -- \
    "${APP_ROOT}/.venv/bin/python" "${APP_ROOT}/run.py" --migrate
systemctl start ai-cam.service
"${APP_ROOT}/scripts/healthcheck_linux.sh"
echo "Update complete. Rollback code: ${ROLLBACK_DIR}/ai-cam-code-${STAMP}.tar.gz"
