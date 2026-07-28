"""
tests/test_auth_security.py
============================================================
P1 — Default credentials + brute-force himoyasi yo'qligi (audit finding).
Qamrab oladi:
  1) Production rejimida standart admin/admin -> startup REFUSED
  2) Simulator (dev) rejimida standart admin/admin -> ruxsat (faqat warning)
  3) Production + o'zgartirilgan credential -> ruxsat
  4) AUTH.enabled=False -> tekshiruv o'tkazilmaydi
  5) Login rate-limit + lockout
  6) Lockout muddati tugagach qayta login ishlaydi
  7) Muvaffaqiyatli login xato hisoblagichini tozalaydi
  8) Session cookie flagslari (HttpOnly / SameSite) + CSRF cookie (non-HttpOnly)
  9) Logout sessiyani bekor qiladi
  10) Parol (kamera/RFID/auth) logga chiqmaydi (markazlashgan sanitizer)

HECH QANDAY tarmoq so'rovi yo'q: TestClient "with"siz ishlatiladi (lifespan/
startup ishga tushmaydi), enforce_startup_security_policy() esa network'siz
sof funksiya sifatida to'g'ridan-to'g'ri chaqiriladi.
"""
from __future__ import annotations

import backend.auth as auth_mod
import backend.config as config_mod
from backend.logger import _SecretSanitizer

from conftest import login


# ===================================================================
# 1-4: Production-mode default-credential blocker
# ===================================================================
def test_default_credentials_refused_in_production_mode(monkeypatch):
    monkeypatch.setattr(config_mod.PLC, "mode", "melsec")
    monkeypatch.setattr(config_mod.RFID, "mode", "simulator")
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "admin")
    monkeypatch.setattr(config_mod.AUTH, "password", "admin")

    assert config_mod.is_production_mode() is True
    try:
        auth_mod.enforce_startup_security_policy()
        assert False, "Production + admin/admin startup REFUSED bo'lishi kerak edi"
    except auth_mod.InsecureDefaultCredentialsError as exc:
        assert "admin/admin" in str(exc)


def test_default_credentials_allowed_in_simulator_mode(monkeypatch):
    monkeypatch.setattr(config_mod.PLC, "mode", "simulator")
    monkeypatch.setattr(config_mod.RFID, "mode", "simulator")
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "admin")
    monkeypatch.setattr(config_mod.AUTH, "password", "admin")

    assert config_mod.is_production_mode() is False
    auth_mod.enforce_startup_security_policy()   # RAISE bo'lmasligi kerak


def test_non_default_credentials_allowed_in_production(monkeypatch):
    monkeypatch.setattr(config_mod.PLC, "mode", "melsec")
    monkeypatch.setattr(config_mod.RFID, "mode", "r700")
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "line1_admin")
    monkeypatch.setattr(config_mod.AUTH, "password", "S7r0ng!Pass")

    auth_mod.enforce_startup_security_policy()   # RAISE bo'lmasligi kerak


def test_auth_disabled_skips_blocker(monkeypatch):
    monkeypatch.setattr(config_mod.PLC, "mode", "melsec")
    monkeypatch.setattr(config_mod.RFID, "mode", "r700")
    monkeypatch.setattr(config_mod.AUTH, "enabled", False)
    monkeypatch.setattr(config_mod.AUTH, "username", "admin")
    monkeypatch.setattr(config_mod.AUTH, "password", "admin")

    auth_mod.enforce_startup_security_policy()   # RAISE bo'lmasligi kerak


# ===================================================================
# 5-7: Login rate-limit + lockout
# ===================================================================
def test_login_rate_limit_lockout_after_max_attempts(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "testadmin")
    monkeypatch.setattr(config_mod.AUTH, "password", "CorrectHorse1")
    monkeypatch.setattr(config_mod.AUTH, "max_failed_attempts", 3)
    monkeypatch.setattr(config_mod.AUTH, "lockout_seconds", 300)

    for _ in range(3):
        r = login(client, "testadmin", "wrong-password")
        assert r.status_code == 401

    # 4-chi urinish — lockout ishga tushgan bo'lishi kerak, TO'G'RI parol bilan ham
    r = login(client, "testadmin", "CorrectHorse1")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_lockout_expires_after_window(monkeypatch):
    class FakeClock:
        def __init__(self):
            self.t = 1_000_000.0

        def time(self):
            return self.t

    clock = FakeClock()
    monkeypatch.setattr(auth_mod.time, "time", clock.time)
    monkeypatch.setattr(config_mod.AUTH, "max_failed_attempts", 3)
    monkeypatch.setattr(config_mod.AUTH, "lockout_seconds", 60)

    ident = "10.0.0.5"
    for _ in range(3):
        auth_mod.record_login_failure(ident)
    locked, remaining = auth_mod.is_locked_out(ident)
    assert locked is True
    assert remaining > 0

    clock.t += 61   # vaqtni sun'iy oldinga suramiz (sleep() EMAS)
    locked2, _ = auth_mod.is_locked_out(ident)
    assert locked2 is False


def test_successful_login_resets_failed_attempts(monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "max_failed_attempts", 3)
    monkeypatch.setattr(config_mod.AUTH, "lockout_seconds", 300)
    ident = "10.0.0.6"
    auth_mod.record_login_failure(ident)
    auth_mod.record_login_failure(ident)
    auth_mod.record_login_success(ident)
    locked, _ = auth_mod.is_locked_out(ident)
    assert locked is False
    assert auth_mod._failed_attempts.get(ident, []) == []


def test_lockout_scoped_after_reset_does_not_bleed_between_tests():
    """_reset_auth_state autouse fixture har testdan avval tozalashini tekshiradi."""
    locked, _ = auth_mod.is_locked_out("10.0.0.5")
    assert locked is False


# ===================================================================
# 8-9: Cookie flags + logout invalidation
# ===================================================================
def test_login_sets_secure_cookie_flags(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "cookieuser")
    monkeypatch.setattr(config_mod.AUTH, "password", "CookiePass1")

    r = login(client, "cookieuser", "CookiePass1")
    assert r.status_code == 303
    set_cookie_headers = r.headers.get_list("set-cookie")
    session_line = next(h for h in set_cookie_headers if h.startswith("ai_cam_session="))
    csrf_line = next(h for h in set_cookie_headers if h.startswith("ai_cam_csrf="))

    assert "httponly" in session_line.lower()
    assert "samesite=lax" in session_line.lower()
    # CSRF cookie must be readable by JS -> NOT HttpOnly
    assert "httponly" not in csrf_line.lower()

    assert client.cookies.get("ai_cam_session")
    assert client.cookies.get("ai_cam_csrf")


def test_logout_invalidates_session(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "logoutuser")
    monkeypatch.setattr(config_mod.AUTH, "password", "LogoutPass1")

    login(client, "logoutuser", "LogoutPass1")
    r1 = client.get("/api/status")
    assert r1.status_code == 200

    client.get("/logout")
    r2 = client.get("/api/status")
    assert r2.status_code == 401


# ===================================================================
# 10: Parol logga chiqmaydi (markazlashgan sanitizer, logger.py)
# ===================================================================
def test_camera_password_sanitized_in_log_line(monkeypatch):
    monkeypatch.setattr(config_mod.CAMERA, "password", "TestCameraPassword-NotProduction!")
    text = "C->K  sMN CheckPassword 3 TestCameraPassword-NotProduction!"
    out = _SecretSanitizer._sanitize(text)
    assert "TestCameraPassword-NotProduction!" not in out
    assert "***" in out


def test_rfid_and_auth_password_sanitized_via_key_value_pattern(monkeypatch):
    monkeypatch.setattr(config_mod.RFID, "password", "TestRfidPassword-NotProduction!")
    monkeypatch.setattr(config_mod.AUTH, "password", "AuthSecretXYZ")
    text = "connecting rfid password=TestRfidPassword-NotProduction! auth password: AuthSecretXYZ"
    out = _SecretSanitizer._sanitize(text)
    assert "TestRfidPassword-NotProduction!" not in out
    assert "AuthSecretXYZ" not in out


def test_authorization_header_sanitized():
    text = "Request header Authorization: Bearer abc.def.ghi"
    out = _SecretSanitizer._sanitize(text)
    assert "abc.def.ghi" not in out
    assert "***" in out


def test_cookie_header_sanitized():
    text = "Request header Cookie: ai_cam_session=SOMETOKENVALUE"
    out = _SecretSanitizer._sanitize(text)
    assert "SOMETOKENVALUE" not in out
    assert "***" in out
