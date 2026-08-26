from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mrtg_cmp.db import Database, TrafficSample


def test_initializes_wal_schema_and_indexes(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")

    database.initialize()

    with database.connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"traffic_samples", "users", "sessions"} <= tables

        indexes = {row[1] for row in connection.execute("PRAGMA index_list('traffic_samples')")}
        assert "idx_traffic_samples_timestamp" in indexes
        assert "idx_traffic_samples_epoch" in indexes

        session_indexes = {row[1] for row in connection.execute("PRAGMA index_list('sessions')")}
        assert "idx_sessions_expires_at" in session_indexes


def test_inserts_and_queries_traffic_samples_in_range(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    database.initialize()

    first_id = database.insert_traffic_sample(
        TrafficSample(
            timestamp="2025-01-01T00:00:00Z",
            rx_bytes=1_000,
            tx_bytes=2_000,
            rx_bps=80.0,
            tx_bps=160.0,
            uptime="00:01:00",
            status="UP",
        )
    )
    database.insert_traffic_sample(
        {
            "timestamp": "2025-01-03T00:00:00Z",
            "rx_bytes": 3_000,
            "tx_bytes": 4_000,
            "rx_bps": 240.0,
            "tx_bps": 320.0,
            "status": "DOWN",
        }
    )

    samples = database.get_traffic_samples("2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")

    assert len(samples) == 1
    assert samples[0]["id"] == first_id
    assert samples[0]["epoch"] == 1_735_689_600
    assert samples[0]["rx_bytes"] == 1_000
    assert samples[0]["tx_bps"] == 160.0
    assert samples[0]["uptime"] == "00:01:00"


def test_users_and_sessions_are_persisted_and_queryable(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    database.initialize()

    user_id = database.create_user(
        username="engineer",
        password_hash="pbkdf2_sha256$example",
        created_at="2025-01-01T00:00:00Z",
    )
    token = "a" * 64

    database.create_session(
        token=token,
        user_id=user_id,
        expires_at=1_735_776_000,
        remember_me=True,
        username="engineer",
    )

    session = database.get_session(token)
    assert session is not None
    assert session["token"] == token
    assert session["user_id"] == user_id
    assert session["username"] == "engineer"
    assert session["remember_me"] == 1

    assert database.get_user("engineer")["id"] == user_id
    assert database.get_user_by_id(user_id)["username"] == "engineer"


def test_initialization_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")

    database.initialize()
    database.initialize()

    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM traffic_samples").fetchone()[0] == 0


def test_expired_sessions_can_be_removed(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    database.initialize()
    user_id = database.create_user("engineer", "hash")

    database.create_session("expired", user_id, 1)
    database.create_session("active", user_id, 9_999_999_999)

    removed = database.delete_expired_sessions(2)

    assert removed == 1
    assert database.get_session("expired") is None
    assert database.get_session("active") is not None


def test_sample_timestamp_can_be_supplied_as_datetime(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    database.initialize()

    sample_id = database.insert_traffic_sample(
        TrafficSample(
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            rx_bytes=10,
            tx_bytes=20,
            rx_bps=1.0,
            tx_bps=2.0,
        )
    )

    assert database.get_traffic_sample(sample_id)["timestamp"] == "2025-01-01T00:00:00Z"


def test_duplicate_username_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    database.initialize()
    database.create_user("engineer", "hash")

    with pytest.raises(sqlite3.IntegrityError):
        database.create_user("engineer", "another-hash")
