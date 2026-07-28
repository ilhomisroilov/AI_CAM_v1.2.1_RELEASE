# AI_CAM Ubuntu Operations

## Service control

```bash
sudo systemctl start ai-cam
sudo systemctl stop ai-cam
sudo systemctl restart ai-cam
sudo systemctl status ai-cam --no-pager
journalctl -u ai-cam -f
```

A deliberate stop remains stopped. `Restart=always` recovers unexpected
process exits while systemd itself is keeping the service active.

## Daily checks

```bash
sudo /opt/ai-cam/scripts/healthcheck_linux.sh
systemctl list-timers ai-cam-backup.timer
df -h /var/lib/ai-cam /var/log/ai-cam
journalctl -u ai-cam --since today -p warning --no-pager
```

`/health` reports release, runtime profile, config source, DB integrity/size,
disk thresholds, process tree, session, devices, reconnect data, YOLO, three
OCR roles, collector, and preflight. Device-offline state is degraded but does
not terminate the process; missing core models/runtime or critical disk/DB
health returns HTTP 503.

## Full diagnostics

```bash
sudo /opt/ai-cam/scripts/diagnose_linux.sh | tee /tmp/ai-cam-diagnose.txt
```

The diagnostic contains no configured secret values. Review it before sharing
because hostnames, routes, IP addresses, and operational VIN logs may still be
sensitive.

## Safe update

Verify a new archive in a separate staging directory, then:

```bash
sudo /opt/ai-cam/scripts/update_linux.sh \
  --source /tmp/ai-cam-next --apply
```

The script writes a consistent DB backup and code rollback archive before
stopping the service, preserves the venv and persistent paths, verifies the
new source, migrates, starts, and health-checks it.

## Rollback

Stop the service, preserve the failed tree, extract the reported
`/var/lib/ai-cam/backups/releases/ai-cam-code-*.tar.gz` into `/opt/ai-cam`,
restore dependencies if the lock changed, run `--self-check` and `--migrate`,
then start. Restore a database only when a migration is not backward
compatible; follow [UBUNTU_26_BACKUP_RESTORE.md](UBUNTU_26_BACKUP_RESTORE.md).

## Retention

- Crops: oldest-first count limit, default 5,000; override with
  `AI_CAM_CROP_RETENTION_MAX`.
- Collector evidence: never silently deleted by default
  (`collector_retention_days: 0`). Archive/review it before explicit cleanup.
- Backups: default 30 days; set `AI_CAM_BACKUP_RETENTION_DAYS=0` to disable
  deletion.
- Journald: use the host journald retention policy.
- Optional files: set `AI_CAM_FILE_LOGGING=1`; logrotate keeps 14 compressed
  rotations.
