# AI_CAM SQLite Backup and Restore

The live database is `/var/lib/ai-cam/data/ai_cam.db`. It uses WAL, a
10-second busy timeout, transactional writes, unique session IDs, and restart
recovery. Never copy the `.db` file alone while the service is live.

## Consistent backup

```bash
sudo -u aicam /opt/ai-cam/scripts/backup_database.sh
ls -l /var/lib/ai-cam/backups
sha256sum -c /var/lib/ai-cam/backups/ai_cam_TIMESTAMP.db.sha256
```

The script uses Python's SQLite online backup API, verifies source and
destination integrity, writes a temporary file, atomically renames it, and
creates SHA-256 evidence. `AI_CAM_BACKUP_RETENTION_DAYS` defaults to 30; zero
disables automatic expiry.

## Restore

Restore is deliberately confirmation-gated and stops the service:

```bash
sudo /opt/ai-cam/scripts/restore_database.sh \
  --backup /var/lib/ai-cam/backups/ai_cam_TIMESTAMP.db \
  --yes
```

It verifies checksum and integrity, backs up the current DB, atomically
installs the selected copy with `aicam` ownership, removes stale WAL sidecars,
runs idempotent migrations, and returns the service to its prior active state.

After restore:

```bash
sudo -u aicam sqlite3 /var/lib/ai-cam/data/ai_cam.db \
  'PRAGMA journal_mode; PRAGMA integrity_check;'
sudo /opt/ai-cam/scripts/healthcheck_linux.sh
```

Copy backups to site-approved offline or remote storage according to the
plant recovery policy. The bundled timer protects only against local database
corruption, not total host/disk loss.
