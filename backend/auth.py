"""
============================================================
auth.py  —  Yengil sessiya-asosli autentifikatsiya
============================================================
Zavod ichki tarmog'ida ruxsatsiz kirishni oldini olish uchun oddiy,
toza mexanizm:

  * Login credentiallari config/settings.yaml dagi config.AUTH dan olinadi.
  * Login muvaffaqiyatli bo'lsa -> tasodifiy sessiya tokeni yaratiladi
    va HttpOnly cookie sifatida o'rnatiladi.
  * Tokenlar xotirada saqlanadi (server qayta ishga tushsa tozalanadi —
    LAN uchun yetarli). TTL: AUTH.session_ttl_sec.
  * Parol hmac.compare_digest bilan solishtiriladi (timing-safe).

Sessiya holati cookie orqali saqlanadi — shuning uchun Dashboard <-> History
o'tishlarida login holati YO'QOLMAYDI.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from .config import AUTH, is_production_mode
from .logger import log

# token -> expiry/CSRF/role/username. Authorization state is server-side.
_sessions: Dict[str, Dict[str, object]] = {}
# Rate-limit state is intentionally kept in-memory.  Older deployments do not
# enable lockout yet, but the shared reset hook keeps tests and future policy
# extensions from leaking authentication state between runs.
_failed_attempts: Dict[str, list[float]] = {}
_lock = threading.Lock()


class InsecureDefaultCredentialsError(RuntimeError):
    """Haqiqiy liniyada standart credential bilan ishga tushish rad etildi."""


def enforce_startup_security_policy() -> None:
    """Productionda hammaga ma'lum admin/admin credentialini qat'iy rad etadi."""
    insecure = {"", "admin", "change_me", "changeme", "password"}
    if (AUTH.enabled and is_production_mode()
            and str(AUTH.password).strip().lower() in insecure):
        raise InsecureDefaultCredentialsError(
            "Production rejimida admin/admin yoki boshqa bo'sh/default auth "
            "paroli taqiqlangan; "
            "AI_CAM_AUTH_PASSWORD orqali kuchli credential bering.")


def reset_rate_limit_state() -> None:
    """Clear transient login-attempt state without changing active sessions."""
    with _lock:
        _failed_attempts.clear()


def _login_window(identifier: str, now: float) -> list[float]:
    window = max(1, int(getattr(AUTH, "lockout_seconds", 300)))
    return [ts for ts in _failed_attempts.get(identifier, []) if now - ts < window]


def is_locked_out(identifier: str) -> tuple[bool, int]:
    """Login manbasi bloklanganmi va necha soniya qolgani."""
    now = time.time()
    limit = max(1, int(getattr(AUTH, "max_failed_attempts", 5)))
    window = max(1, int(getattr(AUTH, "lockout_seconds", 300)))
    with _lock:
        attempts = _login_window(identifier, now)
        if attempts:
            _failed_attempts[identifier] = attempts
        else:
            _failed_attempts.pop(identifier, None)
        if len(attempts) < limit:
            return False, 0
        remaining = max(1, int(window - (now - attempts[-1])))
        return True, remaining


def record_login_failure(identifier: str) -> None:
    now = time.time()
    with _lock:
        attempts = _login_window(identifier, now)
        attempts.append(now)
        _failed_attempts[identifier] = attempts


def record_login_success(identifier: str) -> None:
    with _lock:
        _failed_attempts.pop(identifier, None)


def check_credentials(username: Optional[str], password: Optional[str]) -> Optional[str]:
    """Return ``admin``/``operator`` for valid timing-safe credentials."""
    supplied_user = username or ""
    supplied_password = password or ""
    if (hmac.compare_digest(supplied_user, AUTH.username)
            and hmac.compare_digest(supplied_password, AUTH.password)):
        return "admin"
    operator_user = getattr(AUTH, "operator_username", "")
    operator_password = getattr(AUTH, "operator_password", "")
    if operator_user and operator_password:
        if (hmac.compare_digest(supplied_user, operator_user)
                and hmac.compare_digest(supplied_password, operator_password)):
            return "operator"
    return None


def create_session(role: str = "admin", username: str = "") -> Tuple[str, str]:
    """Create a role-bound session and return ``(session, csrf)``."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {
            "exp": time.time() + AUTH.session_ttl_sec,
            "csrf": csrf,
            "role": role,
            "username": username,
        }
    return token, csrf


def is_valid(token: Optional[str]) -> bool:
    """Token mavjud va muddati o'tmaganligini tekshiradi."""
    if not AUTH.enabled:
        return True                      # auth o'chirilgan bo'lsa hamma ruxsatli
    if not token:
        return False
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return False
        if time.time() > float(session["exp"]):
            _sessions.pop(token, None)   # muddati o'tgan — tozalaymiz
            return False
        return True


def get_role(token: Optional[str]) -> Optional[str]:
    """Return the authenticated role; auth-disabled mode preserves admin access."""
    if not AUTH.enabled:
        return "admin"
    if not token:
        return None
    with _lock:
        session = _sessions.get(token)
        if session is None or time.time() > float(session["exp"]):
            return None
        return str(session.get("role", "admin"))


def get_username(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    with _lock:
        session = _sessions.get(token)
        if session is None or time.time() > float(session["exp"]):
            return None
        return str(session.get("username", "")) or None


def validate_csrf(request: object) -> bool:
    """Validate the session-bound CSRF header for state-changing requests."""
    if not AUTH.enabled or not getattr(AUTH, "csrf_enabled", True):
        return True
    cookies = getattr(request, "cookies", {})
    headers = getattr(request, "headers", {})
    token = cookies.get(AUTH.cookie_name)
    if not token:
        return False
    with _lock:
        session = _sessions.get(token)
        if session is None or time.time() > float(session["exp"]):
            return False
        expected = str(session.get("csrf", ""))
    supplied = headers.get("X-CSRF-Token", "")
    return bool(expected) and hmac.compare_digest(supplied, expected)


def destroy(token: Optional[str]) -> None:
    """Sessiyani bekor qiladi (logout)."""
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def cleanup_expired() -> None:
    """Muddati o'tgan tokenlarni tozalaydi (ixtiyoriy chaqiriladi)."""
    now = time.time()
    with _lock:
        for tok in [t for t, session in _sessions.items()
                    if now > float(session["exp"])]:
            _sessions.pop(tok, None)
