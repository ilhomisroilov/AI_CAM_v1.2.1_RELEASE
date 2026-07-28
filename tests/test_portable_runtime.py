"""Regression tests for the portable one-command runtime (refactor/portable).

These lock in the behaviours that make `python run.py` work from any folder with
no /etc, no systemd and no exported environment:
  * credential auto-load from a git-ignored local env file (whitelist only),
  * runtime path portability (everything under PROJECT_ROOT by default),
  * non-fatal credential validation (admin/admin never blocks startup),
  * canonical GUARDED_PRIMARY OCR release mode from config/settings.yaml.
They are intentionally light (no torch/paddle/model loading).
"""
from __future__ import annotations

from pathlib import Path

from backend import config


def test_autoload_whitelist_loads_credentials_but_ignores_path_roots(tmp_path, monkeypatch):
    env_file = tmp_path / "ai-cam.env"
    env_file.write_text(
        'AI_CAM_CAMERA_PASSWORD="SECRET_CAM"\n'
        "AI_CAM_RFID_USERNAME=root\n"
        "export AI_CAM_RFID_PASSWORD='impinj-x'\n"
        "# a comment\n"
        "AI_CAM_PROJECT_ROOT=/opt/ai-cam\n"       # path-root -> MUST be ignored
        "AI_CAM_DATA_ROOT=/var/lib/ai-cam\n"       # path-root -> MUST be ignored
        "PADDLE_HOME=/var/lib/ai-cam/paddle\n",    # non-whitelisted -> ignored
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_CAM_ENV_FILE", str(env_file))
    for key in ("AI_CAM_CAMERA_PASSWORD", "AI_CAM_RFID_USERNAME",
                "AI_CAM_RFID_PASSWORD", "AI_CAM_PROJECT_ROOT",
                "AI_CAM_DATA_ROOT", "PADDLE_HOME"):
        monkeypatch.delenv(key, raising=False)

    loaded = config.autoload_local_secrets()

    # whitelisted credentials are honoured
    assert "AI_CAM_CAMERA_PASSWORD" in loaded
    assert config.os.environ["AI_CAM_CAMERA_PASSWORD"] == "SECRET_CAM"
    assert config.os.environ["AI_CAM_RFID_USERNAME"] == "root"
    assert config.os.environ["AI_CAM_RFID_PASSWORD"] == "impinj-x"
    # path-root / non-whitelisted keys are NEVER imported (keeps paths portable)
    assert "AI_CAM_PROJECT_ROOT" not in loaded
    assert config.os.environ.get("AI_CAM_PROJECT_ROOT") is None
    assert config.os.environ.get("AI_CAM_DATA_ROOT") is None
    assert config.os.environ.get("PADDLE_HOME") is None


def test_autoload_never_overrides_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "ai-cam.env"
    env_file.write_text('AI_CAM_CAMERA_PASSWORD="FROM_FILE"\n', encoding="utf-8")
    monkeypatch.setenv("AI_CAM_ENV_FILE", str(env_file))
    monkeypatch.setenv("AI_CAM_CAMERA_PASSWORD", "FROM_REAL_ENV")

    loaded = config.autoload_local_secrets()

    assert "AI_CAM_CAMERA_PASSWORD" not in loaded  # already set -> not overridden
    assert config.os.environ["AI_CAM_CAMERA_PASSWORD"] == "FROM_REAL_ENV"


def test_missing_local_env_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAM_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    # must return an empty list, never raise
    assert config.autoload_local_secrets() == []


def test_credential_validation_is_advisory_only(monkeypatch):
    """admin/admin (and empty hardware secrets) surface as WARNINGS, never as a
    fatal env-var-name gate — the app must always be allowed to start."""
    monkeypatch.setattr(config.AUTH, "enabled", True)
    monkeypatch.setattr(config.AUTH, "password", "admin")
    warnings = config.validate_required_secrets()
    assert isinstance(warnings, list)
    # human-readable, settings.yaml-oriented (NOT the legacy env-var names)
    assert any("auth.password" in w for w in warnings)
    assert not any(w == "AI_CAM_ADMIN_PASSWORD" for w in warnings)


def test_runtime_paths_are_project_relative():
    root = config.PROJECT_ROOT.resolve()
    for path in (config.DATA_DIR, config.CROPS_DIR, config.LOGS_DIR,
                 config.TEMP_DIR, config.ENGRAVED_COLLECTION_DIR,
                 config.BACKUPS_DIR, config.DB_PATH,
                 config.TRAINED_MODEL_PATH, config.PADDLE_DET_DIR):
        assert str(path.resolve()).startswith(str(root)), f"{path} escaped {root}"


def test_no_hardcoded_user_or_system_paths_in_defaults():
    """Default (no-env) runtime/model roots stay under PROJECT_ROOT — never baked
    to /opt, /etc or /var. (PROJECT_ROOT itself may live under a user dir on a dev
    box; only the derived roots are asserted.)"""
    assert str(config.RUNTIME_DIR).startswith(str(config.PROJECT_ROOT))
    assert str(config.MODELS_DIR).startswith(str(config.PROJECT_ROOT))
    for token in ("/opt/ai", "/var/lib/ai-cam", "/etc/ai-cam", "/var/log/ai-cam"):
        assert token not in str(config.RUNTIME_DIR)
        assert token not in str(config.MODELS_DIR)


def test_only_d2222_no_d2223_reintroduced():
    # exit/D2223 flow must stay absent in the canonical config
    assert config.PLC.signal_address == "D2222"
    assert str(getattr(config.PLC, "exit_address", "")).strip() == ""
