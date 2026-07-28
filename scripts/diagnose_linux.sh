#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(id -u)" -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
if [[ -r /etc/ai-cam/ai-cam.env ]]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/ai-cam/ai-cam.env
    set +a
fi
APP_ROOT="${AI_CAM_PROJECT_ROOT:-/opt/ai-cam}"
DATA_ROOT="${AI_CAM_DATA_ROOT:-/var/lib/ai-cam}"
DB_PATH="${AI_CAM_DB_PATH:-${DATA_ROOT}/data/ai_cam.db}"
FAILURES=0
TARGETS=(
    "camera-control|10.123.86.42|2111"
    "camera-blob|10.123.86.42|2113"
    "plc|10.123.40.99|5003"
    "rfid|10.123.18.3|80"
)

section() {
    printf '\n[%s]\n' "$1"
}
section "host"
date --iso-8601=seconds
uname -a
id
uptime
section "interfaces"
ip -brief address
section "routes"
ip route show
for ip in 10.123.86.42 10.123.40.99 10.123.18.3; do
    ip route get "${ip}" || FAILURES=$((FAILURES + 1))
done
section "device reachability"
for target in "${TARGETS[@]}"; do
    IFS='|' read -r name host port <<<"${target}"
    ping -c 1 -W 1 "${host}" >/dev/null 2>&1 || \
        echo "${name}: ICMP unavailable (may be filtered)"
    if nc -z -w 2 "${host}" "${port}"; then
        echo "${name}: ${host}:${port} reachable"
    else
        echo "${name}: ${host}:${port} UNREACHABLE" >&2
        FAILURES=$((FAILURES + 1))
    fi
done
section "DNS"
resolvectl status 2>/dev/null || cat /etc/resolv.conf
section "firewall"
ufw status verbose 2>/dev/null || true
nft list ruleset 2>/dev/null || true
section "time synchronization"
timedatectl status
section "service and process ownership"
systemctl --no-pager --full status ai-cam.service 2>/dev/null || \
    FAILURES=$((FAILURES + 1))
pgrep -a -u aicam -f 'python.*run.py|paddle|ocr' || true
section "filesystem"
df -h "${DATA_ROOT}" /var/log/ai-cam 2>/dev/null || FAILURES=$((FAILURES + 1))
find "${DATA_ROOT}" -maxdepth 2 -printf '%M %u:%g %p\n' 2>/dev/null | head -n 100
section "database"
if [[ -f "${DB_PATH}" ]]; then
    sqlite3 "${DB_PATH}" 'PRAGMA journal_mode; PRAGMA integrity_check;'
else
    echo "Database not created: ${DB_PATH}"
fi
section "models"
if [[ -x "${APP_ROOT}/.venv/bin/python" ]]; then
    runuser -u aicam --preserve-environment -- \
        "${APP_ROOT}/.venv/bin/python" "${APP_ROOT}/run.py" --self-check || \
        FAILURES=$((FAILURES + 1))
else
    echo "Linux venv missing" >&2
    FAILURES=$((FAILURES + 1))
fi
section "recent journal"
journalctl -u ai-cam.service -n 100 --no-pager 2>/dev/null || true
exit "${FAILURES}"
