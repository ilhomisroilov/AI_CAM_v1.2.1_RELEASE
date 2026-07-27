"""
tests/test_log_clear_authz.py
============================================================
P2 — /api/logs/clear audit-wipe (audit finding: server.py:467-473, har
login qilgan foydalanuvchi bir so'rovda butun log tarixini o'chira oladi;
rol nazorati, CSRF himoyasi, audit entry YO'Q).

Qamrab oladi:
  1) Oddiy (operator) rol log clear qila OLMAYDI (403)
  2) Admin rol log clear qila OLADI (CSRF bilan)
  3) CSRF tokenisiz admin ham rad etiladi
  4) Muvaffaqiyatli clear'dan keyin audit yozuvi qoladi (kim/qachon)
"""
from __future__ import annotations

import backend.config as config_mod
import backend.server as server_mod
from backend.logger import ui_handler

from conftest import login, get_csrf_token


def _mock_clear_files(monkeypatch):
    """
    MUHIM: backend.logger.clear_files() JORIY (real) logs/*.log fayllarini
    truncate qiladi — bu haqiqiy audit dalili bo'lgan fayllar, testlarda
    ULARGA HECH QACHON tegilmasligi kerak. Shu sababli bu modul clear_files()
    ni har doim no-op bilan almashtiradi; faqat ui_handler (xotiradagi ring
    buffer, doim ham ephemeral/test-safe) haqiqiy tozalanadi. Rol/CSRF/audit
    mantig'i clear_files() chaqirilishidan MUSTAQIL sinaladi.
    """
    calls = []
    monkeypatch.setattr(server_mod, "clear_files", lambda: calls.append(True))
    return calls


def _login_admin(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "clearadmin")
    monkeypatch.setattr(config_mod.AUTH, "password", "ClearAdminPass1")
    monkeypatch.setattr(config_mod.AUTH, "operator_username", "clearop")
    monkeypatch.setattr(config_mod.AUTH, "operator_password", "ClearOpPass1")
    r = login(client, "clearadmin", "ClearAdminPass1")
    assert r.status_code == 303


def _login_operator(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "clearadmin")
    monkeypatch.setattr(config_mod.AUTH, "password", "ClearAdminPass1")
    monkeypatch.setattr(config_mod.AUTH, "operator_username", "clearop")
    monkeypatch.setattr(config_mod.AUTH, "operator_password", "ClearOpPass1")
    r = login(client, "clearop", "ClearOpPass1")
    assert r.status_code == 303


def test_operator_cannot_clear_logs(client, monkeypatch):
    _mock_clear_files(monkeypatch)
    _login_operator(client, monkeypatch)
    csrf = get_csrf_token(client)
    r = client.delete("/api/logs/clear", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403
    assert r.json()["ok"] is False


def test_admin_can_clear_logs_with_csrf(client, monkeypatch):
    calls = _mock_clear_files(monkeypatch)
    _login_admin(client, monkeypatch)
    csrf = get_csrf_token(client)
    r = client.delete("/api/logs/clear", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == [True]   # clear_files() chaqirilgani tasdiqlandi (real fayl TEGILMADI)


def test_admin_without_csrf_is_rejected(client, monkeypatch):
    _mock_clear_files(monkeypatch)
    _login_admin(client, monkeypatch)
    r = client.delete("/api/logs/clear")   # X-CSRF-Token yo'q
    assert r.status_code == 403


def test_clear_leaves_audit_entry_behind(client, monkeypatch):
    _mock_clear_files(monkeypatch)
    _login_admin(client, monkeypatch)
    csrf = get_csrf_token(client)
    r = client.delete("/api/logs/clear", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    # Tozalashdan KEYIN audit yozuvi bufer/faylga qo'shiladi (tozalanmaydi).
    snap = ui_handler.snapshot()
    assert len(snap) >= 1
    assert any("AUDIT" in e["msg"] and "clearadmin" in e["msg"] for e in snap)
