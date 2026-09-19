"""Authentication and session management using PBKDF2-HMAC-SHA256."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, settings
from .db import Database

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "mrtg_session"
DEFAULT_PBKDF2_ITERATIONS = 260_000

#: Login throttle (CWE-307): a key is locked for LOGIN_LOCKOUT_SECONDS once it
#: accumulates LOGIN_MAX_ATTEMPTS consecutive failures inside the window.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 900

_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a cryptographically random salt."""
    if salt is None:
        salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        DEFAULT_PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${DEFAULT_PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a PBKDF2 formatted hash in constant time."""
    try:
        algorithm, iterations_str, salt_hex, hash_hex = hashed_password.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def _login_keys(ip: str, username: str) -> tuple[str, str]:
    """Return the throttle keys for a login attempt: per-IP and per-username."""
    return f"ip:{ip}", f"user:{username.strip().lower()}"


def login_lockout_remaining(ip: str, username: str) -> int:
    """Return the seconds left on the login lockout, or 0 when the key is usable."""
    now = time.monotonic()
    with _login_lock:
        for key in _login_keys(ip, username):
            attempts = [
                ts for ts in _login_failures.get(key, ()) if now - ts < LOGIN_LOCKOUT_SECONDS
            ]
            if attempts:
                _login_failures[key] = attempts
            if len(attempts) >= LOGIN_MAX_ATTEMPTS:
                return int(LOGIN_LOCKOUT_SECONDS - (now - attempts[0])) + 1
    return 0


def record_login_failure(ip: str, username: str) -> int:
    """Record a failed login against the IP and username keys; return the lockout seconds."""
    now = time.monotonic()
    with _login_lock:
        for key in _login_keys(ip, username):
            attempts = _login_failures.setdefault(key, [])
            attempts.append(now)
            _login_failures[key] = [
                ts for ts in attempts if now - ts < LOGIN_LOCKOUT_SECONDS
            ]
    return login_lockout_remaining(ip, username)


def clear_login_failures(ip: str, username: str) -> None:
    """Drop the failure history for a key pair after a successful login."""
    with _login_lock:
        for key in _login_keys(ip, username):
            _login_failures.pop(key, None)


def reset_login_rate_limit() -> None:
    """Clear every throttle counter (test isolation)."""
    with _login_lock:
        _login_failures.clear()


def ensure_admin_user(
    database: Database | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """Seed the default administrative user if no user exists in the database."""
    config = cfg or settings
    db = database or Database(config.database_path)
    db.initialize()

    admin_username = config.admin_username
    admin_password = (
        config.admin_password.strip() if config.admin_password is not None else None
    )

    existing = db.get_user(admin_username)
    if existing is not None:
        if admin_password and not verify_password(
            admin_password, existing["password_hash"]
        ):
            hashed = hash_password(admin_password)
            existing_id = int(existing["id"])
            with db.connection() as connection, connection:
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hashed, existing_id),
                )
            rotated = db.get_user_by_id(existing_id)
            if rotated is None:
                raise RuntimeError(
                    f"Failed to retrieve admin user after password rotation for id {existing_id}"
                )
            return rotated
        return existing

    initial_password = admin_password or secrets.token_urlsafe(24)
    if not admin_password:
        logger.warning(
            "ADMIN_PASSWORD is not configured; generated a random password for '%s'. "
            "Copy it from this line, then set ADMIN_PASSWORD in .env: %s",
            admin_username,
            initial_password,
        )
    hashed = hash_password(initial_password)
    user_id = db.create_user(username=admin_username, password_hash=hashed, role='admin')
    user = db.get_user_by_id(user_id)
    if user is None:
        raise RuntimeError(f"Failed to retrieve newly created user id {user_id}")
    return user


def create_user_session(
    database: Database,
    user_id: int,
    username: str,
    remember_me: bool = False,
    cfg: Settings | None = None,
) -> tuple[str, int]:
    """Create a new session token and return (token, max_age_seconds)."""
    config = cfg or settings
    ttl = config.remember_me_ttl_seconds if remember_me else config.session_ttl_seconds
    now_epoch = int(datetime.now(UTC).timestamp())
    expires_at = now_epoch + ttl

    token = secrets.token_hex(32)
    database.create_session(
        token=token,
        user_id=user_id,
        username=username,
        expires_at=expires_at,
        remember_me=remember_me,
    )
    return token, ttl


def get_session_user(database: Database, token: str) -> dict[str, Any] | None:
    """Retrieve the user associated with an active, unexpired session token."""
    session = database.get_session(token)
    if not session:
        return None

    now_epoch = int(datetime.now(UTC).timestamp())
    if session["expires_at"] <= now_epoch:
        database.delete_session(token)
        return None

    return database.get_user_by_id(session["user_id"])


def revoke_session(database: Database, token: str) -> bool:
    """Explicitly delete a session token (logout)."""
    return database.delete_session(token)


# FastAPI Dependencies
def get_db() -> Database:
    """Dependency provider for the configured SQLite Database repository."""
    return Database(settings.database_path)


def get_current_user_optional(
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any] | None:
    """Extract authenticated user from cookie if valid, else return None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return get_session_user(db, token)


def require_authenticated_user(
    request: Request,
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    """Require an authenticated user.

    Redirects browser requests to /login or raises 401 for API calls.
    """
    if current_user is not None:
        return current_user

    accept = request.headers.get("accept", "").lower()
    if "text/html" in accept:
        # Redirect browser navigation to the login page
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={next_path}"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def require_admin(
    request: Request,
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    """Require an authenticated admin user.
    
    Rejects 'viewer' or unauthenticated users with HTTP 403 Forbidden.
    Raises HTTP 401 or redirects to login for unauthenticated requests.
    """
    if current_user is None:
        accept = request.headers.get("accept", "").lower()
        if "text/html" in accept:
            # Redirect browser navigation to the login page
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": f"/login?next={next_path}"},
            )
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: admin privileges required",
        )
    
    return current_user


def change_password_and_revoke_sessions(
    db: Database,
    username: str,
    old_password: str,
    new_password: str,
) -> bool:
    """Change user password and revoke all active sessions for that user.
    
    Returns True if successful, False if old password is incorrect.
    """
    user = db.get_user(username)
    if not user or not verify_password(old_password, user["password_hash"]):
        return False
    
    # Generate new password hash
    new_hashed_password = hash_password(new_password)
    
    # Update user password
    with db.connection() as connection, connection:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hashed_password, user["id"]),
        )
        
        # Revoke all sessions for this user
        connection.execute(
            "DELETE FROM sessions WHERE user_id = ?",
            (user["id"],),
        )
    
    return True


def validate_redirect_url(next_url: str | None, base_url: str = "http://localhost:8000") -> str:
    """Validate redirect URL to prevent open redirects (CWE-601).
    
    Returns the validated URL or base_url if validation fails.
    """
    if not next_url:
        return base_url
    
    # Strip whitespace and URL fragments
    next_url = next_url.split("#")[0].strip()
    
    # Parse the URL
    from urllib.parse import urlparse
    parsed = urlparse(next_url)
    
    # Only allow relative URLs (no scheme, netloc, or path starting with http:// or https://)
    if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
        return base_url
    
    # Return the validated URL (relative or same-origin)
    return next_url
__all__ = [
    "change_password_and_revoke_sessions",
    "clear_login_failures",
    "create_user_session",
    "ensure_admin_user",
    "get_current_user_optional",
    "get_db",
    "get_session_user",
    "hash_password",
    "login_lockout_remaining",
    "LOGIN_LOCKOUT_SECONDS",
    "LOGIN_MAX_ATTEMPTS",
    "record_login_failure",
    "require_admin",
    "require_authenticated_user",
    "reset_login_rate_limit",
    "revoke_session",
    "SESSION_COOKIE_NAME",
    "validate_redirect_url",
    "verify_password",
]
