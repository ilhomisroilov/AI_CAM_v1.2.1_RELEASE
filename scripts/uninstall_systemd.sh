#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(id -u)" -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
if [[ "${1:-}" != "--yes" ]]; then
    echo "This removes AI_CAM systemd unit files but preserves code, config, data, logs, and backups."
    echo "Re-run with --yes to continue."
    exit 2
fi
systemctl disable --now ai-cam.service ai-cam-backup.timer 2>/dev/null || true
rm -f -- \
    /etc/systemd/system/ai-cam.service \
    /etc/systemd/system/ai-cam-backup.service \
    /etc/systemd/system/ai-cam-backup.timer \
    /etc/logrotate.d/ai-cam
systemctl daemon-reload
systemctl reset-failed
echo "Removed systemd/logrotate integration. Application data was preserved."
