"""Authentication and session management using PBKDF2-HMAC-SHA256."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, settings
from .db import Database

SESSION_COOKIE_NAME = "mrtg_session"
DEFAULT_PBKDF2_ITERATIONS = 260_000


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


def ensure_admin_user(
    database: Database | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """Seed the default administrative user if no user exists in the database."""
    config = cfg or settings
    db = database or Database(config.database_path)
    db.initialize()

    admin_username = config.admin_username
    existing = db.get_user(admin_username)
    if existing is not None:
        # Rotate the stored hash when the configured admin password changes
        if config.admin_password and not verify_password(
            config.admin_password, existing["password_hash"]
        ):
            hashed = hash_password(config.admin_password)
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

    # Default initial password if not specified via environment
    initial_password = config.admin_password or "admin123"
    hashed = hash_password(initial_password)
    user_id = db.create_user(username=admin_username, password_hash=hashed)
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
