#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
START=0
[[ "${1:-}" != "--start" ]] || START=1
[[ "$(id -u)" -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
[[ "${APP_ROOT}" == "/opt/ai-cam" ]] || {
    echo "Expected release at /opt/ai-cam, got ${APP_ROOT}" >&2
    exit 1
}
[[ -x "${APP_ROOT}/.venv/bin/python" ]] || {
    echo "Run setup_ubuntu_26.sh first" >&2
    exit 1
}
[[ -r /etc/ai-cam/ai-cam.env ]] || {
    echo "Missing /etc/ai-cam/ai-cam.env" >&2
    exit 1
}
install -o root -g root -m 0644 \
    "${APP_ROOT}/deploy/systemd/ai-cam.service" /etc/systemd/system/ai-cam.service
install -o root -g root -m 0644 \
    "${APP_ROOT}/deploy/systemd/ai-cam-backup.service" /etc/systemd/system/ai-cam-backup.service
install -o root -g root -m 0644 \
    "${APP_ROOT}/deploy/systemd/ai-cam-backup.timer" /etc/systemd/system/ai-cam-backup.timer
install -o root -g root -m 0644 \
    "${APP_ROOT}/deploy/logrotate/ai-cam" /etc/logrotate.d/ai-cam
systemd-analyze verify /etc/systemd/system/ai-cam.service \
    /etc/systemd/system/ai-cam-backup.service \
    /etc/systemd/system/ai-cam-backup.timer
systemctl daemon-reload
systemctl enable ai-cam.service ai-cam-backup.timer
if [[ "${START}" -eq 1 ]]; then
    systemctl start ai-cam.service ai-cam-backup.timer
fi
echo "Installed and enabled AI_CAM units. Start with: sudo systemctl start ai-cam"
