"""Unit tests for PBKDF2 authentication and session management."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mrtg_cmp.auth import (
    LOGIN_MAX_ATTEMPTS,
    clear_login_failures,
    create_user_session,
    ensure_admin_user,
    get_session_user,
    hash_password,
    login_lockout_remaining,
    record_login_failure,
    reset_login_rate_limit,
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


def test_ensure_admin_user_never_falls_back_to_a_known_password(tmp_path: Path) -> None:
    """Without ADMIN_PASSWORD the seed uses a random password, never 'admin123' (CWE-798)."""
    db = Database(tmp_path / "auth_random_test.db")
    db.initialize()

    user = ensure_admin_user(database=db, cfg=Settings(admin_username="admin", admin_password=None))

    assert verify_password("admin123", user["password_hash"]) is False
    assert verify_password("", user["password_hash"]) is False


def test_login_rate_limit_locks_out_after_max_attempts() -> None:
    """Five consecutive failures lock the IP/username pair for the lockout window."""
    reset_login_rate_limit()
    ip, username = "203.0.113.7", "admin"

    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        assert record_login_failure(ip, username) == 0
    assert login_lockout_remaining(ip, username) == 0

    assert record_login_failure(ip, username) > 0
    assert login_lockout_remaining(ip, username) > 0
    assert login_lockout_remaining(ip, username) <= 900
    # A different account from the same host is throttled too (per-IP key).
    assert login_lockout_remaining(ip, "other") > 0

    clear_login_failures(ip, username)
    assert login_lockout_remaining(ip, username) == 0


def test_login_rate_limit_expires_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures older than the lockout window stop counting."""
    reset_login_rate_limit()
    import mrtg_cmp.auth as auth_module

    base = time.monotonic()
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: base)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        record_login_failure("198.51.100.9", "operator")
    assert login_lockout_remaining("198.51.100.9", "operator") > 0

    monkeypatch.setattr(auth_module.time, "monotonic", lambda: base + 901)
    assert login_lockout_remaining("198.51.100.9", "operator") == 0


def test_ensure_admin_user_strips_whitespace_and_updates_password(tmp_path: Path) -> None:
    """Whitespace around admin_password in config is stripped before hashing/verification."""
    db = Database(tmp_path / "auth_ws_rot_test.db")
    db.initialize()

    original = Settings(admin_username="admin", admin_password="InitialPassword1!")
    seeded = ensure_admin_user(database=db, cfg=original)
    assert verify_password("InitialPassword1!", seeded["password_hash"]) is True

    rotated = Settings(admin_username="admin", admin_password="  PaddedPassword456!  \n")
    user = ensure_admin_user(database=db, cfg=rotated)

    assert user["id"] == seeded["id"]
    assert verify_password("PaddedPassword456!", user["password_hash"]) is True
    assert verify_password("  PaddedPassword456!  \n", user["password_hash"]) is False


def test_ensure_admin_user_does_not_rotate_on_blank_password(tmp_path: Path) -> None:
    """An empty or whitespace-only password does not overwrite an existing admin password."""
    db = Database(tmp_path / "auth_blank_test.db")
    db.initialize()

    original = Settings(admin_username="admin", admin_password="OriginalPass123!")
    seeded = ensure_admin_user(database=db, cfg=original)
    assert verify_password("OriginalPass123!", seeded["password_hash"]) is True

    blank_cfg = Settings(admin_username="admin", admin_password="   ")
    user = ensure_admin_user(database=db, cfg=blank_cfg)

    assert user["id"] == seeded["id"]
    assert verify_password("OriginalPass123!", user["password_hash"]) is True


def test_settings_loads_config_env_with_precedence(tmp_path: Path) -> None:
    """SettingsConfigDict prioritizes config/.env over root .env."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    config_env = cfg_dir / ".env"
    root_env = tmp_path / ".env"

    config_env.write_text(
        "ADMIN_PASSWORD=FromConfig\nSITE_NAME=ConfigGateway\n", encoding="utf-8"
    )
    root_env.write_text(
        "ADMIN_PASSWORD=FromRoot\nSITE_NAME=RootGateway\nLOCATION_NAME=FallbackLocation\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=(str(config_env), str(root_env)))
    assert settings.admin_password == "FromConfig"
    assert settings.site_name == "ConfigGateway"
    assert settings.location_name == "FallbackLocation"


def test_settings_falls_back_to_root_env_when_config_env_missing(tmp_path: Path) -> None:
    """When config/.env is absent, root .env is loaded cleanly."""
    root_env = tmp_path / ".env"
    root_env.write_text("ADMIN_PASSWORD=FallbackSecret\n", encoding="utf-8")

    settings = Settings(_env_file=(str(tmp_path / "config" / ".env"), str(root_env)))
    assert settings.admin_password == "FallbackSecret"


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
