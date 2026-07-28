# AI_CAM Production Configuration

Configuration is layered in this order:

1. `/opt/ai-cam/config/settings.yaml` — tracked production structure and
   nonsecret line topology.
2. `AI_CAM_CONFIG`, normally
   `/etc/ai-cam/settings.production.yaml` — host overrides.
3. `/etc/ai-cam/ai-cam.env` — secrets and deployment roots.

Later values override earlier values. `${AI_CAM_*}` YAML placeholders expand
from the environment. Real mode fails before server/device startup if required
credentials are empty or placeholders.

Current nonsecret production topology:

```yaml
camera: {ip: 10.123.86.42, cola_port: 2111, blob_port: 2113}
plc:
  {enabled: true, mode: melsec, ip: 10.123.40.99, port: 5003,
   plc_type: Q, signal_address: D2222, trigger_on_value: 1}
rfid: {enabled: true, mode: r700, ip: 10.123.18.3, port: 80}
server: {host: 0.0.0.0, port: 8080}
```

D2222 is the single verified arrival pulse/register. D2223 is not active.
Timeouts, reconnect/backoff, RFID inventory/retry windows, antenna ports,
transmit power, OCR thresholds/profiles, session/capture deadlines, and
`Asia/Tashkent` are retained from the working repository.

Required secret environment keys:

```text
AI_CAM_CAMERA_PASSWORD
AI_CAM_ADMIN_PASSWORD
AI_CAM_RFID_USERNAME
AI_CAM_RFID_PASSWORD
```

Never paste their values into YAML, Git, logs, reports, screenshots, issue
trackers, or archives.

Useful commands:

```bash
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --print-config
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --self-check
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --dry-run
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --no-hardware
```

`--no-hardware` changes PLC and RFID to simulator-safe mode in memory for
smoke tests; it does not rewrite production files. `--self-check` and
`--dry-run` do not connect to live PLC/camera/RFID.

Persistent paths are supplied by the env file:

```text
AI_CAM_PROJECT_ROOT=/opt/ai-cam
AI_CAM_DATA_ROOT=/var/lib/ai-cam
AI_CAM_LOG_ROOT=/var/log/ai-cam
AI_CAM_CONFIG=/etc/ai-cam/settings.production.yaml
```
