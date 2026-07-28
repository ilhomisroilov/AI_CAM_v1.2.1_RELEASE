# AI_CAM Ubuntu Troubleshooting

Start with:

```bash
systemctl status ai-cam --no-pager
journalctl -u ai-cam -b -n 300 --no-pager
sudo /opt/ai-cam/scripts/diagnose_linux.sh
```

## Startup gate reports missing secrets

Populate `/etc/ai-cam/ai-cam.env`; keep it `0640 root:aicam`. Inspect only the
redacted result with `run_linux.sh --print-config`.

## Wrong Python

Linux production accepts only CPython 3.11. Do not point the service at Ubuntu
26.04's default Python 3.14 or change `/usr/bin/python3`. Rerun
`setup_ubuntu_26.sh`; it locates or provisions 64-bit 3.11 under the application
root and creates `/opt/ai-cam/.venv`.

## Device offline

The process should remain running. Check health preflight, routes, VLAN/ACLs,
and TCP endpoints from [UBUNTU_26_NETWORK.md](UBUNTU_26_NETWORK.md). Confirm
that another host/application does not own the camera or reader connection.

## Port 8080 unavailable

```bash
sudo ss -ltnp 'sport = :8080'
sudo systemctl stop ai-cam
pgrep -a -u aicam -f run.py
```

AI_CAM's lock also refuses a second launcher using the same data root.

## Database error or full disk

```bash
df -h /var/lib/ai-cam /var/log/ai-cam
sudo -u aicam sqlite3 /var/lib/ai-cam/data/ai_cam.db \
  'PRAGMA journal_mode; PRAGMA integrity_check;'
sudo -u aicam /opt/ai-cam/scripts/backup_database.sh
```

Do not delete collector evidence silently. Archive/review it and apply an
explicit site retention decision. Crop count retention is automatic and
bounded.

## Orphan OCR processes

```bash
systemctl show -p MainPID,ControlGroup ai-cam
systemd-cgls /system.slice/ai-cam.service
pgrep -a -u aicam -f 'run.py|paddle|ocr'
```

Stop through systemd and allow the 90-second timeout. `KillMode=mixed` is the
final bounded cleanup. Capture the journal before killing anything manually.

## Reset after failure

```bash
sudo systemctl reset-failed ai-cam
sudo systemctl start ai-cam
sudo /opt/ai-cam/scripts/healthcheck_linux.sh
```
