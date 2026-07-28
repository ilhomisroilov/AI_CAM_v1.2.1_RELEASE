# AI_CAM systemd Integration

The service unit is `deploy/systemd/ai-cam.service` and directly executes:

```text
/opt/ai-cam/.venv/bin/python /opt/ai-cam/run.py
```

There is no alternate application entry point and no shell wrapper in the
unit. It uses `User=aicam`, `WorkingDirectory=/opt/ai-cam`, the protected
EnvironmentFile, one process owner, `SIGTERM`, a mixed process-tree kill
policy, a 240-second preload allowance, and a 90-second shutdown allowance.
Hardening is deliberately conservative so Torch, Paddle, shared memory,
process pools, sockets, model reads, SQLite, crops, and collector writes work.

Install and validate:

```bash
sudo /opt/ai-cam/scripts/install_systemd.sh
systemd-analyze verify /etc/systemd/system/ai-cam.service
sudo systemctl daemon-reload
sudo systemctl enable ai-cam
sudo systemctl start ai-cam
sudo systemctl status ai-cam --no-pager
journalctl -u ai-cam -f
```

Crash-restart test in a maintenance window:

```bash
main_pid="$(systemctl show -p MainPID --value ai-cam)"
sudo kill -KILL "${main_pid}"
sleep 10
systemctl is-active ai-cam
systemctl show -p NRestarts,MainPID ai-cam
```

Graceful stop/process-tree test:

```bash
sudo systemctl stop ai-cam
systemctl is-active ai-cam               # expected: inactive
pgrep -a -u aicam -f 'run.py|paddle|ocr' # expected: no output
sudo systemctl start ai-cam
```

The optional persistent daily timer runs a SQLite online backup without
stopping AI_CAM:

```bash
sudo systemctl enable --now ai-cam-backup.timer
systemctl list-timers ai-cam-backup.timer
sudo systemctl start ai-cam-backup.service
journalctl -u ai-cam-backup.service --no-pager
```

Remove only the integration, preserving all application data:

```bash
sudo /opt/ai-cam/scripts/uninstall_systemd.sh --yes
```
