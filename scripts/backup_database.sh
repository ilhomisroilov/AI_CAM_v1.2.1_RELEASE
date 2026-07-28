#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${AI_CAM_PROJECT_ROOT:-/opt/ai-cam}"
DATA_ROOT="${AI_CAM_DATA_ROOT:-/var/lib/ai-cam}"
DB_PATH="${AI_CAM_DB_PATH:-${DATA_ROOT}/data/ai_cam.db}"
BACKUP_ROOT="${AI_CAM_BACKUP_ROOT:-${DATA_ROOT}/backups}"
RETENTION_DAYS="${AI_CAM_BACKUP_RETENTION_DAYS:-30}"
PYTHON_BIN="${AI_CAM_PYTHON_BIN:-${APP_ROOT}/.venv/bin/python}"

[[ -x "${PYTHON_BIN}" ]] || { echo "Missing ${PYTHON_BIN}" >&2; exit 1; }
[[ -f "${DB_PATH}" ]] || { echo "Database not found: ${DB_PATH}" >&2; exit 1; }
[[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || {
    echo "AI_CAM_BACKUP_RETENTION_DAYS must be a non-negative integer" >&2
    exit 1
}
install -d -m 0750 "${BACKUP_ROOT}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL_PATH="${BACKUP_ROOT}/ai_cam_${STAMP}.db"
TEMP_PATH="${BACKUP_ROOT}/.ai_cam_${STAMP}.db.tmp"
LOCK_PATH="${BACKUP_ROOT}/.backup.lock"

exec 9>"${LOCK_PATH}"
flock -n 9 || { echo "Another backup is running" >&2; exit 1; }
trap 'rm -f -- "${TEMP_PATH}"' EXIT

"${PYTHON_BIN}" - "${DB_PATH}" "${TEMP_PATH}" <<'PY'
import sqlite3
import sys

source_path, target_path = sys.argv[1:3]
with sqlite3.connect(source_path, timeout=30.0) as source:
    source.execute("PRAGMA busy_timeout=30000")
    check = source.execute("PRAGMA quick_check(1)").fetchone()[0]
    if str(check).lower() != "ok":
        raise SystemExit(f"source database integrity failed: {check}")
    with sqlite3.connect(target_path) as target:
        source.backup(target, pages=256, sleep=0.05)
        target_check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if str(target_check).lower() != "ok":
            raise SystemExit(f"backup integrity failed: {target_check}")
PY
chmod 0640 "${TEMP_PATH}"
mv -f -- "${TEMP_PATH}" "${FINAL_PATH}"
(
    cd -- "${BACKUP_ROOT}"
    sha256sum "$(basename -- "${FINAL_PATH}")" > "$(basename -- "${FINAL_PATH}").sha256"
)

if (( RETENTION_DAYS > 0 )); then
    while IFS= read -r -d '' expired; do
        rm -f -- "${expired}" "${expired}.sha256"
    done < <(find "${BACKUP_ROOT}" -maxdepth 1 -type f \
        -name 'ai_cam_*.db' -mtime "+${RETENTION_DAYS}" -print0)
fi
echo "${FINAL_PATH}"
