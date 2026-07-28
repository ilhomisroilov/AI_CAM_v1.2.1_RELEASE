#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${AI_CAM_PROJECT_ROOT:-/opt/ai-cam}"
PORT="${AI_CAM_PORT:-8080}"
PYTHON_BIN="${APP_ROOT}/.venv/bin/python"
[[ -x "${PYTHON_BIN}" ]] || { echo "AI_CAM venv unavailable" >&2; exit 1; }
systemctl is-active --quiet ai-cam.service || {
    echo "ai-cam.service is not active" >&2
    exit 1
}
"${PYTHON_BIN}" - "http://127.0.0.1:${PORT}/health" <<'PY'
import json
import sys
from urllib.request import urlopen

url = sys.argv[1]
with urlopen(url, timeout=5.0) as response:
    payload = json.loads(response.read().decode("utf-8"))
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit(f"core health unavailable: HTTP {response.status} {payload.get('status')}")
components = payload.get("components", {})
if not components.get("yolo", {}).get("ready"):
    raise SystemExit("core health unavailable: YOLO not ready")
if components.get("runtime", {}).get("status") == "critical":
    raise SystemExit("core health unavailable: runtime critical")
print(json.dumps({
    "status": payload.get("status"),
    "version": payload.get("application", {}).get("version"),
    "database": components.get("database", {}).get("integrity"),
    "devices": {
        name: components.get(name, {}).get("connected")
        for name in ("camera", "plc", "rfid")
    },
}, sort_keys=True))
PY
