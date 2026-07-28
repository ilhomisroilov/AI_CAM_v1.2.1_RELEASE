# AI_CAM v1.2.1 — Ubuntu Server 26.04 Deployment

This package installs the same `run.py` application used by Windows. Ubuntu
production is pinned to 64-bit CPython 3.11; it does not change
`/usr/bin/python3`.

## 1. Transfer and verify

On the deployment workstation:

```bash
cd /tmp
sha256sum -c SHA256SUMS
sudo install -d -o root -g root -m 0755 /opt/ai-cam
sudo tar -xzf AI_CAM_v1.2.1_ubuntu26_amd64.tar.gz -C /opt/ai-cam
cd /opt/ai-cam
```

The archive expands directly into the application root. It contains models,
source, requirements, units, scripts, documentation, checksums, and
provenance. It does not contain `.git`, `.venv`, runtime data, Windows build
output, logs, or secrets.

For a Git-based staging flow, check out
`release/v1.2.1-ubuntu-production` into `/opt/ai-cam`, run `git lfs pull`,
and verify that model files are binaries rather than Git LFS pointer text.

## 2. Install the CPU-safe profile

```bash
cd /opt/ai-cam
sudo ./scripts/setup_ubuntu_26.sh --cpu
```

The installer creates the `aicam` system account, persistent directories,
configuration templates, Python 3.11 venv, pinned dependencies, and runs:

```bash
/opt/ai-cam/.venv/bin/python /opt/ai-cam/run.py --self-check
/opt/ai-cam/.venv/bin/python /opt/ai-cam/run.py --dry-run
```

Use `--gpu` only after completing
[UBUNTU_26_GPU.md](UBUNTU_26_GPU.md).

## 3. Populate secrets

Edit the root-owned file:

```bash
sudoedit /etc/ai-cam/ai-cam.env
sudo chown root:aicam /etc/ai-cam/ai-cam.env
sudo chmod 0640 /etc/ai-cam/ai-cam.env
```

Set nonempty values for `AI_CAM_CAMERA_PASSWORD`,
`AI_CAM_ADMIN_PASSWORD`, `AI_CAM_RFID_USERNAME`, and
`AI_CAM_RFID_PASSWORD`. Never put them in YAML, Git, support bundles, or the
deployment archive.

Review the redacted effective configuration:

```bash
sudo -u aicam /opt/ai-cam/scripts/run_linux.sh --print-config
```

## 4. Install and start systemd

```bash
sudo /opt/ai-cam/scripts/install_systemd.sh
sudo systemctl start ai-cam.service
sudo systemctl start ai-cam-backup.timer
sudo systemctl status ai-cam.service --no-pager
sudo journalctl -u ai-cam.service -f
sudo /opt/ai-cam/scripts/healthcheck_linux.sh
```

Allow TCP 8080 only from the factory LAN, for example:

```bash
sudo ufw allow from FACTORY_LAN_CIDR to any port 8080 proto tcp
```

Replace `FACTORY_LAN_CIDR` deliberately. Do not expose AI_CAM to the public
Internet.

## 5. Reboot verification

```bash
sudo reboot
# After reconnect:
systemctl is-enabled ai-cam.service ai-cam-backup.timer
systemctl is-active ai-cam.service
sudo /opt/ai-cam/scripts/healthcheck_linux.sh
journalctl -u ai-cam.service -b --no-pager
```

Complete live camera, PLC, RFID, reconnect, and one full D2222 session tests
before declaring production verification.
