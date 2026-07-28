#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
ENV_FILE="${AI_CAM_ENV_FILE:-/etc/ai-cam/ai-cam.env}"
[[ "$(uname -s)" == "Linux" ]] || { echo "Linux only" >&2; exit 1; }
[[ -x "${APP_ROOT}/.venv/bin/python" ]] || {
    echo "Missing Linux venv: ${APP_ROOT}/.venv" >&2
    exit 1
}
if [[ -r "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
fi
export AI_CAM_PROJECT_ROOT="${AI_CAM_PROJECT_ROOT:-${APP_ROOT}}"
exec "${APP_ROOT}/.venv/bin/python" "${APP_ROOT}/run.py" "$@"
