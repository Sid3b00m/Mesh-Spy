from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.core.config import get_config
from app.core.security import login_limiter

COOKIE = "mesh_spy_session"
_sessions: dict[str, datetime] = {}
_sessions_lock = Lock()
SESSION_HOURS = 12


def _password() -> str:
    env = os.environ.get("MESH_SPY_PASSWORD", "").strip()
    if env:
        return env
    return (get_config().auth.password or "").strip()


def auth_enabled() -> bool:
    cfg = get_config().auth
    if not cfg.enabled:
        return False
    return bool(_password())


def verify_login(username: str, password: str, *, client_key: str | None = None) -> bool:
    if client_key and not login_limiter.allow(client_key):
        return False
    cfg = get_config().auth
    expected_user = cfg.username or "ops"
    expected_pass = _password()
    if not expected_pass:
        return False
    user_ok = hmac.compare_digest(
        username.strip().encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"),
        expected_pass.encode("utf-8"),
    )
    return user_ok and pass_ok


def _prune_sessions_locked(now: datetime) -> None:
    for token in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(token, None)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _sessions_lock:
        # Expired tokens are otherwise only dropped when re-presented, so a
        # long-running console would accumulate them forever.
        _prune_sessions_locked(now)
        _sessions[token] = now + timedelta(hours=SESSION_HOURS)
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp < datetime.now(timezone.utc):
            _sessions.pop(token, None)
            return False
    return True


def drop_session(token: str | None) -> None:
    if token:
        with _sessions_lock:
            _sessions.pop(token, None)


def public_path(path: str) -> bool:
    if path in ("/login", "/logout"):
        return True
    if path.startswith("/static/"):
        return True
    if path == "/api/health":
        return True
    return False


def attach_session_cookie(response, token: str) -> None:
    secure = os.environ.get("MESH_SPY_SECURE_COOKIE", "").strip() in ("1", "true", "yes")
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_HOURS * 3600,
    )
