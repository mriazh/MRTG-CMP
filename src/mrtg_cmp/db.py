"""SQLite persistence for traffic samples and dashboard authentication data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import settings

_VALID_STATUSES = {"UP", "DOWN", "RESET"}


def _utc_timestamp(value: str | datetime) -> tuple[str, int]:
    """Return a canonical UTC timestamp and its integer Unix epoch."""

    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

    parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), int(parsed.timestamp())


def _normalise_boundary(value: str | datetime | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    _, epoch = _utc_timestamp(value)
    return epoch


@dataclass(slots=True)
class TrafficSample:
    """A single WAN traffic observation."""

    timestamp: str | datetime
    rx_bytes: int
    tx_bytes: int
    rx_bps: float
    tx_bps: float
    epoch: int | None = None
    uptime: str | None = None
    status: str = "UP"

    def normalise(self) -> tuple[str, int, str]:
        timestamp, timestamp_epoch = _utc_timestamp(self.timestamp)
        epoch = timestamp_epoch if self.epoch is None else int(self.epoch)
        status = self.status.upper()
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")
        return timestamp, epoch, status


class Database:
    """Connection-per-operation SQLite repository.

    A new connection is used for each operation so the repository is safe to use from
    the collector and web worker without sharing a connection across threads.
    WAL mode and foreign-key enforcement are enabled on every connection.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else settings.database_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a configured connection and leave transaction control to caller."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema and indexes if they do not already exist."""

        with self.connection() as connection:
            # This is intentionally executed on every connection: it is the
            # durable setting that enables concurrent readers and writers.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS traffic_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    rx_bytes INTEGER NOT NULL,
                    tx_bytes INTEGER NOT NULL,
                    rx_bps REAL NOT NULL,
                    tx_bps REAL NOT NULL,
                    uptime TEXT,
                    status TEXT NOT NULL CHECK (status IN ('UP', 'DOWN', 'RESET'))
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    remember_me INTEGER NOT NULL DEFAULT 0
                        CHECK (remember_me IN (0, 1)),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS console_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_preview TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_traffic_samples_timestamp
                    ON traffic_samples(timestamp);
                CREATE INDEX IF NOT EXISTS idx_traffic_samples_epoch
                    ON traffic_samples(epoch);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
                    ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_console_logs_epoch
                    ON console_logs(epoch DESC);
                """
            )
            connection.commit()

    initialize_schema = initialize

    @staticmethod
    def _sample_values(
        sample: TrafficSample | Mapping[str, Any],
    ) -> tuple[Any, ...]:
        if isinstance(sample, TrafficSample):
            timestamp, epoch, status = sample.normalise()
            return (
                timestamp,
                epoch,
                int(sample.rx_bytes),
                int(sample.tx_bytes),
                float(sample.rx_bps),
                float(sample.tx_bps),
                sample.uptime,
                status,
            )

        values = dict(sample)
        timestamp_value = values.get("timestamp")
        if timestamp_value is None:
            raise ValueError("traffic sample requires timestamp")
        timestamp, timestamp_epoch = _utc_timestamp(timestamp_value)
        epoch = int(values.get("epoch", timestamp_epoch))
        status = str(values.get("status", "UP")).upper()
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")
        return (
            timestamp,
            epoch,
            int(values["rx_bytes"]),
            int(values["tx_bytes"]),
            float(values["rx_bps"]),
            float(values["tx_bps"]),
            values.get("uptime"),
            status,
        )

    def insert_traffic_sample(self, sample: TrafficSample | Mapping[str, Any]) -> int:
        """Insert one traffic observation and return its primary key."""

        values = self._sample_values(sample)
        with self.connection() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO traffic_samples (
                    timestamp, epoch, rx_bytes, tx_bytes, rx_bps, tx_bps,
                    uptime, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("SQLite did not return a row id")
            return int(lastrowid)

    insert_sample = insert_traffic_sample

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_traffic_sample(self, sample_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM traffic_samples WHERE id = ?", (int(sample_id),)
            ).fetchone()
            return self._row_to_dict(row)

    def get_latest_sample(self) -> dict[str, Any] | None:
        """Return the most recent traffic sample, or None if the table is empty."""
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM traffic_samples ORDER BY epoch DESC, id DESC LIMIT 1"
                ).fetchone()
                return self._row_to_dict(row)
        except sqlite3.OperationalError:
            self.initialize()
            return None

    get_latest_traffic_sample = get_latest_sample

    def get_recent_traffic_samples(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent traffic samples, newest first."""
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            with self.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM traffic_samples ORDER BY epoch DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            self.initialize()
            return []

    def get_traffic_samples(
        self,
        start: str | datetime | int | float,
        end: str | datetime | int | float,
        *,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return samples in an inclusive time range, oldest first."""

        start_epoch = _normalise_boundary(start)
        end_epoch = _normalise_boundary(end)
        if start_epoch > end_epoch:
            raise ValueError("start must not be later than end")
        if limit < 1:
            raise ValueError("limit must be positive")

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM traffic_samples
                WHERE epoch BETWEEN ? AND ?
                ORDER BY epoch ASC, id ASC
                LIMIT ?
                """,
                (start_epoch, end_epoch, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    query_traffic_samples = get_traffic_samples

    def create_user(
        self,
        username: str,
        password_hash: str,
        created_at: str | datetime | None = None,
    ) -> int:
        """Create a user and return its primary key."""

        if not username.strip():
            raise ValueError("username must not be empty")
        if not password_hash:
            raise ValueError("password_hash must not be empty")
        if created_at is None:
            created_at_value = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(created_at, datetime):
            created_at_value, _ = _utc_timestamp(created_at)
        else:
            created_at_value, _ = _utc_timestamp(created_at)

        with self.connection() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (username.strip(), password_hash, created_at_value),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("SQLite did not return a row id")
            return int(lastrowid)

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
            return self._row_to_dict(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
            return self._row_to_dict(row)

    def create_session(
        self,
        token: str,
        user_id: int | None = None,
        expires_at: int | None = None,
        remember_me: bool = False,
        username: str | None = None,
    ) -> str:
        """Create a session, resolving ``user_id`` from ``username`` if needed."""

        if not token:
            raise ValueError("token must not be empty")
        if user_id is None:
            if not username:
                raise ValueError("username is required when user_id is omitted")
            user = self.get_user(username)
            if user is None:
                raise ValueError(f"unknown user: {username}")
            user_id = int(user["id"])
        if expires_at is None:
            raise ValueError("expires_at is required")
        user = self.get_user_by_id(user_id)
        if user is None:
            raise ValueError(f"unknown user_id: {user_id}")
        session_username = username or str(user["username"])

        with self.connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    token, user_id, username, expires_at, remember_me
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token,
                    int(user_id),
                    session_username,
                    int(expires_at),
                    int(bool(remember_me)),
                ),
            )
        return token

    def get_session(self, token: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
            return self._row_to_dict(row)

    def delete_session(self, token: str) -> bool:
        """Revoke one session by token."""
        with self.connection() as connection, connection:
            cursor = connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return bool(cursor.rowcount > 0)

    def delete_expired_sessions(self, now: int | None = None) -> int:
        """Delete expired sessions and return the number removed."""

        current_time = int(datetime.now(UTC).timestamp()) if now is None else int(now)
        with self.connection() as connection, connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (current_time,)
            )
            return int(cursor.rowcount)

    def insert_console_log(
        self,
        username: str,
        command: str,
        status: str,
        output_preview: str,
        timestamp: str | None = None,
        epoch: int | None = None,
    ) -> int:
        """Insert a console command execution log entry and return its id."""
        if timestamp is None or epoch is None:
            ts_str, ts_epoch = _utc_timestamp(datetime.now(UTC))
            timestamp = timestamp or ts_str
            epoch = epoch if epoch is not None else ts_epoch

        with self.connection() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO console_logs (
                    timestamp, epoch, username, command, status, output_preview
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, epoch, username, command, status, output_preview[:500]),
            )
            return int(cursor.lastrowid or 0)

    def get_recent_console_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent console execution audit logs ordered newest first."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM console_logs
                ORDER BY epoch DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def close(self) -> None:
        """Compatibility no-op; connections are scoped to each operation."""

        return None


def initialize_database(path: str | Path | None = None) -> Database:
    """Create and return an initialized database repository."""

    database = Database(path)
    database.initialize()
    return database


__all__ = ["Database", "TrafficSample", "initialize_database"]
