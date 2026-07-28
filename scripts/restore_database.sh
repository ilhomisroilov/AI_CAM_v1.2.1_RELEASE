#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
if [[ -r /etc/ai-cam/ai-cam.env ]]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/ai-cam/ai-cam.env
    set +a
fi
DATA_ROOT="${AI_CAM_DATA_ROOT:-/var/lib/ai-cam}"
DB_PATH="${AI_CAM_DB_PATH:-${DATA_ROOT}/data/ai_cam.db}"
PYTHON_BIN="${AI_CAM_PYTHON_BIN:-${APP_ROOT}/.venv/bin/python}"
BACKUP_PATH=""
CONFIRM=0

usage() {
    echo "Usage: sudo $0 --backup /path/to/ai_cam_TIMESTAMP.db --yes"
}
while (($#)); do
    case "$1" in
        --backup) BACKUP_PATH="${2:-}"; shift 2 ;;
        --yes) CONFIRM=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done
[[ "$(id -u)" -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
[[ "${CONFIRM}" -eq 1 && -n "${BACKUP_PATH}" ]] || { usage; exit 2; }
BACKUP_PATH="$(readlink -f -- "${BACKUP_PATH}")"
[[ -f "${BACKUP_PATH}" ]] || { echo "Backup not found" >&2; exit 1; }
[[ -x "${PYTHON_BIN}" ]] || { echo "Missing ${PYTHON_BIN}" >&2; exit 1; }
if [[ -f "${BACKUP_PATH}.sha256" ]]; then
    (
        cd -- "$(dirname -- "${BACKUP_PATH}")"
        sha256sum -c "$(basename -- "${BACKUP_PATH}").sha256"
    )
fi
"${PYTHON_BIN}" - "${BACKUP_PATH}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as connection:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
if str(result).lower() != "ok":
    raise SystemExit(f"backup integrity failed: {result}")
PY

WAS_ACTIVE=0
RESTORE_TEMP=""
systemctl is-active --quiet ai-cam.service && WAS_ACTIVE=1
if (( WAS_ACTIVE )); then
    systemctl stop ai-cam.service
fi
cleanup() {
    local code=$?
    trap - EXIT
    if [[ -n "${RESTORE_TEMP}" ]]; then
        rm -f -- "${RESTORE_TEMP}"
    fi
    if (( WAS_ACTIVE )); then
        systemctl start ai-cam.service || true
    fi
    exit "${code}"
}
trap cleanup EXIT

if [[ -f "${DB_PATH}" ]]; then
    "${APP_ROOT}/scripts/backup_database.sh" >/dev/null
fi
install -d -o aicam -g aicam -m 0750 "$(dirname -- "${DB_PATH}")"
RESTORE_TEMP="${DB_PATH}.restore.$$"
install -o aicam -g aicam -m 0640 "${BACKUP_PATH}" "${RESTORE_TEMP}"
mv -f -- "${RESTORE_TEMP}" "${DB_PATH}"
RESTORE_TEMP=""
rm -f -- "${DB_PATH}-wal" "${DB_PATH}-shm"
runuser -u aicam --preserve-environment -- \
    "${PYTHON_BIN}" "${APP_ROOT}/run.py" --migrate
echo "Restored ${BACKUP_PATH} -> ${DB_PATH}"
