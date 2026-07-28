"""Ubuntu production deployment contracts (safe to collect on Windows)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from backend import config
from backend import production_runtime
from backend import release_startup
from backend.instance_lock import InstanceLock

ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "AI_CAM_PROJECT_ROOT": str(ROOT),
        "AI_CAM_DATA_ROOT": str(tmp_path / "runtime"),
        "AI_CAM_LOG_ROOT": str(tmp_path / "logs"),
        "AI_CAM_CONFIG": str(ROOT / "config" / "settings.example.yaml"),
        "AI_CAM_CAMERA_PASSWORD": "test-camera-secret",
        "AI_CAM_ADMIN_PASSWORD": "test-admin-secret",
        "AI_CAM_RFID_USERNAME": "test-rfid-user",
        "AI_CAM_RFID_PASSWORD": "test-rfid-secret",
        "AI_CAM_ALLOW_CPU_FALLBACK": "1",
        "AI_CAM_FILE_LOGGING": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _result_line(output: str) -> dict:
    line = next(item for item in output.splitlines() if item.startswith("RESULT="))
    return json.loads(line.removeprefix("RESULT="))


def test_tracked_production_topology_is_exact_and_d2222_only():
    import yaml

    production_yaml = (ROOT / "config/settings.yaml").read_text(encoding="utf-8")
    settings = yaml.safe_load(production_yaml)
    assert (
        settings["camera"]["ip"],
        settings["camera"]["cola_port"],
        settings["camera"]["blob_port"],
    ) == ("10.123.86.42", 2111, 2113)
    assert settings["plc"]["enabled"] is True
    assert (
        settings["plc"]["mode"], settings["plc"]["ip"],
        settings["plc"]["port"], settings["plc"]["plc_type"],
    ) == ("melsec", "10.123.40.99", 5003, "Q")
    assert settings["plc"]["signal_address"] == "D2222"
    assert settings["plc"]["trigger_on_value"] == 1
    assert settings["plc"]["exit_address"] == ""
    assert settings["rfid"]["enabled"] is True
    assert (
        settings["rfid"]["mode"], settings["rfid"]["ip_address"],
        settings["rfid"]["port"],
    ) == ("r700", "10.123.18.3", 80)
    assert "D2223" not in production_yaml.upper()


def test_real_mode_reports_every_required_missing_secret(monkeypatch):
    monkeypatch.setattr(config.PLC, "enabled", True)
    monkeypatch.setattr(config.PLC, "mode", "melsec")
    monkeypatch.setattr(config.RFID, "enabled", True)
    monkeypatch.setattr(config.RFID, "mode", "r700")
    monkeypatch.setattr(config.CAMERA, "password", "CHANGE_ME")
    monkeypatch.setattr(config.RFID, "username", "")
    monkeypatch.setattr(config.RFID, "password", "")
    monkeypatch.setattr(config.AUTH, "enabled", True)
    monkeypatch.setattr(config.AUTH, "password", "CHANGE_ME")
    # Portable model: warnings are human-readable and reference config/settings.yaml
    # fields (NOT env-var names). They are advisory only — startup never blocks.
    missing = config.validate_required_secrets()
    assert any("camera.password" in item for item in missing)
    assert any("rfid.username" in item for item in missing)
    assert any("rfid.password" in item for item in missing)
    assert any("auth.password" in item for item in missing)


def test_linux_path_and_environment_overrides(tmp_path):
    code = (
        "import json;"
        "from backend.config import effective_config;"
        "print('RESULT='+json.dumps(effective_config(redact=True)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_subprocess_env(tmp_path),
        text=True,
        capture_output=True,
        check=True,
    )
    payload = _result_line(result.stdout)
    assert payload["paths"]["project_root"] == str(ROOT.resolve())
    assert payload["paths"]["runtime_root"] == str((tmp_path / "runtime").resolve())
    assert payload["paths"]["log_root"] == str((tmp_path / "logs").resolve())
    assert payload["sections"]["plc"]["mode"] == "simulator"
    assert payload["sections"]["camera"]["password"] == "***"
    assert "test-camera-secret" not in result.stdout


def test_external_production_yaml_and_environment_secret(tmp_path):
    override = tmp_path / "settings.production.yaml"
    override.write_text(
        "server:\n  port: 8123\n"
        "camera:\n  password: ${AI_CAM_CAMERA_PASSWORD}\n"
        "plc:\n  enabled: false\n  mode: simulator\n"
        "rfid:\n  enabled: false\n  mode: simulator\n",
        encoding="utf-8",
    )
    env = _subprocess_env(tmp_path)
    env["AI_CAM_CONFIG"] = str(override)
    code = (
        "import json;"
        "from backend.config import CAMERA,CONFIG_SOURCES,SERVER,effective_config;"
        "print('RESULT='+json.dumps({"
        "'port':SERVER.port,'password_loaded':CAMERA.password=='test-camera-secret',"
        "'sources':CONFIG_SOURCES,'public':effective_config(redact=True)}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = _result_line(result.stdout)
    assert payload["port"] == 8123
    assert payload["password_loaded"] is True
    assert str(override.resolve()) in payload["sources"]
    assert payload["public"]["sections"]["camera"]["password"] == "***"
    assert "test-camera-secret" not in result.stdout


class _VersionInfo(tuple):
    major = property(lambda self: self[0])
    minor = property(lambda self: self[1])
    micro = property(lambda self: self[2])


def test_linux_rejects_python_314_before_dependency_work(monkeypatch):
    monkeypatch.setattr(release_startup.sys, "platform", "linux")
    monkeypatch.setattr(
        release_startup.sys, "version_info", _VersionInfo((3, 14, 0, "final", 0))
    )
    with pytest.raises(release_startup.StartupGateError, match="CPython 3.11"):
        release_startup.run_self_check()


def test_first_database_creation_uses_overridden_data_root_and_wal(tmp_path):
    result = subprocess.run(
        [sys.executable, "run.py", "--no-hardware", "--migrate"],
        cwd=ROOT,
        env=_subprocess_env(tmp_path),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    db_path = tmp_path / "runtime" / "data" / "ai_cam.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_one_instance_lock_rejects_second_process(tmp_path):
    lock_path = tmp_path / "ai-cam.lock"
    code = (
        "import sys;"
        "from pathlib import Path;"
        "from backend.instance_lock import InstanceLock,InstanceLockError;"
        f"p=Path({str(lock_path)!r});"
        "\ntry:\n InstanceLock(p).acquire()\nexcept InstanceLockError:\n sys.exit(0)\n"
        "sys.exit(5)"
    )
    with InstanceLock(lock_path):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=_subprocess_env(tmp_path),
            timeout=15,
        )
    assert result.returncode == 0


def test_offline_device_preflight_is_degraded_not_exception(monkeypatch):
    monkeypatch.delenv("AI_CAM_NO_HARDWARE", raising=False)
    monkeypatch.setattr(config.CAMERA, "ip", "192.0.2.10")
    monkeypatch.setattr(config.CAMERA, "cola_port", 9)
    monkeypatch.setattr(config.CAMERA, "blob_port", 9)
    monkeypatch.setattr(config.PLC, "enabled", False)
    monkeypatch.setattr(config.RFID, "enabled", False)
    result = production_runtime.run_network_preflight(timeout=0.05)
    assert result["status"] == "degraded"
    assert result["endpoints"]["camera_control"]["reachable"] is False


def test_critical_disk_rejects_trigger_before_session(
    pipeline_instance, monkeypatch
):
    import backend.pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=0),
    )
    before = pipeline_instance._dropped_trigger_count
    pipeline_instance.plc_on()
    assert pipeline_instance._active_session_id is None
    assert pipeline_instance._dropped_trigger_count == before + 1


def test_shutdown_durably_cancels_active_session(pipeline_instance):
    from backend.pipeline import SessionState

    pipeline_instance.plc_on()
    session_id = pipeline_instance._active_session_id
    assert session_id
    pipeline_instance.prepare_shutdown()
    assert pipeline_instance._active_session_id is None
    session = pipeline_instance._sessions[session_id]
    assert session.state == SessionState.CANCELLED
    assert session.failure_reason == "SERVICE_SHUTDOWN"


def test_no_hardware_preflight_opens_no_device_sockets(monkeypatch):
    monkeypatch.setenv("AI_CAM_NO_HARDWARE", "1")
    monkeypatch.setattr(
        production_runtime.socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("device socket opened"),
    )
    result = production_runtime.run_network_preflight(timeout=0.05)
    assert result["attempted"] == 0
    assert all(item["skipped"] for item in result["endpoints"].values())


def test_systemd_units_call_canonical_launcher_and_are_single_worker():
    service = (ROOT / "deploy/systemd/ai-cam.service").read_text(encoding="utf-8")
    assert "User=aicam" in service
    assert "Group=aicam" in service
    assert "WorkingDirectory=/opt/ai-cam" in service
    assert "EnvironmentFile=/etc/ai-cam/ai-cam.env" in service
    assert (
        "ExecStart=/opt/ai-cam/.venv/bin/python /opt/ai-cam/run.py" in service
    )
    assert "Restart=always" in service
    assert "KillSignal=SIGTERM" in service
    assert "--workers" not in service
    assert "uvicorn" not in service.lower()


def test_linux_scripts_are_strict_portable_and_not_windows_only():
    scripts = sorted((ROOT / "scripts").glob("*.sh"))
    expected = {
        "setup_ubuntu_26.sh", "run_linux.sh", "install_systemd.sh",
        "uninstall_systemd.sh", "update_linux.sh", "backup_database.sh",
        "restore_database.sh", "healthcheck_linux.sh", "diagnose_linux.sh",
    }
    assert expected <= {path.name for path in scripts}
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
        assert "\r" not in text
        assert "PowerShell" not in text
        assert "CTRL_BREAK_EVENT" not in text
        assert "CREATE_NEW_PROCESS_GROUP" not in text
        assert "cmd.exe" not in text


def test_linux_lock_has_no_windows_or_gui_opencv_packages():
    lock = (ROOT / "requirements-linux-lock.txt").read_text(encoding="utf-8")
    assert "protobuf==3.20.2" in lock
    assert "paddlepaddle==2.6.2" in lock
    assert "paddleocr==2.10.0" in lock
    assert "torch==2.4.1+cpu" in lock
    assert "opencv-python-headless==4.11.0.86" in lock
    for package in ("pywin32", "pyreadline", "pefile", "pyinstaller", "\nopencv-python=="):
        assert package not in lock.lower()


def test_linux_environment_uses_explicit_paddle_cache_and_project_models():
    env_text = (
        ROOT / "deploy/config/ai-cam.env.example"
    ).read_text(encoding="utf-8")
    assert "PADDLE_HOME=/var/lib/ai-cam/temp/paddle" in env_text
    assert "PADDLEOCR_HOME=/var/lib/ai-cam/temp/paddleocr" in env_text
    startup = (ROOT / "backend/release_startup.py").read_text(encoding="utf-8")
    assert "PADDLE_DET_DIR" in startup
    assert "PADDLE_REC_DIR" in startup
    assert "PADDLE_CLS_DIR" in startup


@pytest.mark.skipif(os.name != "posix", reason="Linux backup script contract")
def test_linux_backup_is_consistent_checksummed_and_non_destructive(tmp_path):
    data_root = tmp_path / "data-root"
    database = data_root / "data" / "ai_cam.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO evidence(value) VALUES ('kept')")
    env = os.environ.copy()
    env.update({
        "AI_CAM_PROJECT_ROOT": str(ROOT),
        "AI_CAM_DATA_ROOT": str(data_root),
        "AI_CAM_PYTHON_BIN": sys.executable,
        "AI_CAM_BACKUP_RETENTION_DAYS": "0",
    })
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "backup_database.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    backup = Path(result.stdout.strip().splitlines()[-1])
    assert backup.is_file()
    assert backup.with_suffix(backup.suffix + ".sha256").is_file()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM evidence").fetchone()[0] == "kept"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone()[0] == "kept"


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group contract")
@pytest.mark.slow
def test_linux_sigterm_gracefully_cleans_process_group(tmp_path):
    env = _subprocess_env(tmp_path)
    port = 18080
    override = tmp_path / "settings.production.yaml"
    override.write_text(
        f"server:\n  host: 127.0.0.1\n  port: {port}\n"
        "detection:\n  device: cpu\n"
        "ocr:\n  use_gpu: false\n"
        "plc:\n  enabled: false\n  mode: simulator\n"
        "rfid:\n  enabled: false\n  mode: simulator\n"
        "auth:\n  enabled: false\n",
        encoding="utf-8",
    )
    env["AI_CAM_CONFIG"] = str(override)
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--no-hardware"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"server exited early: {proc.returncode}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.5)
        else:
            pytest.fail("server did not become healthy")
        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=90) == 0
        time.sleep(0.5)
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
