"""Unit tests for PBKDF2 authentication and session management."""

from __future__ import annotations

import time
from pathlib import Path

from mrtg_cmp.auth import (
    create_user_session,
    ensure_admin_user,
    get_session_user,
    hash_password,
    revoke_session,
    verify_password,
)
from mrtg_cmp.config import Settings
from mrtg_cmp.db import Database


def test_password_hashing_and_verification() -> None:
    """Verify password hashing produces formatted hashes verified in constant time."""
    password = "SuperSecretPassword123!"
    hashed = hash_password(password)

    assert hashed.startswith("pbkdf2_sha256$260000$")
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password(password, "malformed$hash") is False


def test_ensure_admin_user_idempotence(tmp_path: Path) -> None:
    """Ensure default admin user is seeded once and returns same user on repeated calls."""
    db = Database(tmp_path / "auth_test.db")
    db.initialize()
    cfg = Settings(admin_username="admin", admin_password="TestPassword456!")

    user1 = ensure_admin_user(database=db, cfg=cfg)
    assert user1["username"] == "admin"
    assert verify_password("TestPassword456!", user1["password_hash"]) is True

    # Second call returns existing without error
    user2 = ensure_admin_user(database=db, cfg=cfg)
    assert user2["id"] == user1["id"]
    assert user2["username"] == user1["username"]


def test_ensure_admin_user_updates_password(tmp_path: Path) -> None:
    """When config.admin_password changes, rotate the stored hash on next ensure_admin_user."""
    db = Database(tmp_path / "auth_rot_test.db")
    db.initialize()

    original = Settings(admin_username="admin", admin_password="InitialPassword1!")
    seeded = ensure_admin_user(database=db, cfg=original)
    assert verify_password("InitialPassword1!", seeded["password_hash"]) is True

    # Simulate the admin password being changed in the environment between runs
    rotated = Settings(admin_username="admin", admin_password="RotatedPassword2!")
    user = ensure_admin_user(database=db, cfg=rotated)

    # Same admin row, but the stored hash now validates against the new password
    assert user["id"] == seeded["id"]
    assert verify_password("RotatedPassword2!", user["password_hash"]) is True
    # And the old password no longer works
    assert verify_password("InitialPassword1!", user["password_hash"]) is False


def test_session_lifecycle_and_expiration(tmp_path: Path) -> None:
    """Ensure session tokens can be retrieved, expire correctly, and can be revoked."""
    db = Database(tmp_path / "auth_test.db")
    db.initialize()
    cfg = Settings(session_ttl_seconds=3600, remember_me_ttl_seconds=86400)

    user_id = db.create_user("operator", hash_password("pass"))

    # 1. Normal session
    token, ttl = create_user_session(
        db, user_id=user_id, username="operator", remember_me=False, cfg=cfg
    )
    assert ttl == 3600
    user = get_session_user(db, token)
    assert user is not None
    assert user["username"] == "operator"

    # 2. Revoke session (logout)
    revoked = revoke_session(db, token)
    assert revoked is True
    assert get_session_user(db, token) is None

    # 3. Expired session
    past_now = int(time.time()) - 100
    db.create_session(
        token="expired_token_123",
        user_id=user_id,
        username="operator",
        expires_at=past_now,
        remember_me=False,
    )
    assert get_session_user(db, "expired_token_123") is None
