"""Tests for RouterOS Web Console bridge, session manager, and execution formatting."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from mrtg_cmp.console import (
    ConsoleSession,
    ConsoleSessionManager,
    execute_routeros_command,
    format_key_value_output,
    format_tabular_output,
    resolve_command_tokens,
)


def test_session_manager_lifecycle() -> None:
    """ConsoleSessionManager manages in-memory sessions, validation, and termination."""
    mgr = ConsoleSessionManager()

    # Empty token
    assert mgr.get_session(None) is None
    assert mgr.get_session("nonexistent") is None

    # Manually inject a session
    now = time.time()
    session = ConsoleSession(
        token="test_token_123",
        username="admin",
        password="secret_password",
        host="192.168.88.1",
        port=8728,
        identity="MikroTik-Test",
        created_at=now,
        last_active=now,
    )
    mgr._sessions["test_token_123"] = session

    # Retrieve valid session
    s = mgr.get_session("test_token_123")
    assert s is not None
    assert s.username == "admin"
    assert s.identity == "MikroTik-Test"

    # Terminate session
    assert mgr.terminate("test_token_123") is True
    assert mgr.get_session("test_token_123") is None
    assert mgr.terminate("test_token_123") is False


def test_session_manager_timeout() -> None:
    """ConsoleSessionManager expires sessions idle for longer than timeout."""
    mgr = ConsoleSessionManager()
    old_time = time.time() - 350  # 350s ago (> 300s timeout)
    session = ConsoleSession(
        token="expired_token",
        username="admin",
        password="secret_password",
        host="192.168.88.1",
        port=8728,
        identity="MikroTik-Test",
        created_at=old_time,
        last_active=old_time,
    )
    mgr._sessions["expired_token"] = session

    assert mgr.get_session("expired_token") is None
    assert "expired_token" not in mgr._sessions


def test_format_tabular_output() -> None:
    """format_tabular_output creates aligned monospace text from dict records."""
    assert format_tabular_output([]) == "(no items found)"

    data = [
        {"name": "WAN", "type": "ether", "running": "true"},
        {"name": "bridge-LAN", "type": "bridge", "running": "true"},
    ]
    formatted = format_tabular_output(data)
    assert "NAME" in formatted
    assert "TYPE" in formatted
    assert "RUNNING" in formatted
    assert "WAN" in formatted
    assert "bridge-LAN" in formatted


def test_format_key_value_output() -> None:
    """format_key_value_output formats key value pairs cleanly."""
    data = {"cpu": "12%", "uptime": "79d", "board-name": "hEX PoE"}
    formatted = format_key_value_output(data)
    assert "cpu: 12%" in formatted
    assert "board-name: hEX PoE" in formatted


def test_execute_routeros_command_local_parsing() -> None:
    """execute_routeros_command handles comments, empty lines, and exits locally."""
    now = time.time()
    session = ConsoleSession(
        token="tok",
        username="admin",
        password="pwd",
        host="127.0.0.1",
        port=8728,
        identity="Test",
        created_at=now,
        last_active=now,
    )

    # Empty command
    ok, out = execute_routeros_command(session, "")
    assert ok is True
    assert out == ""

    # Comment
    ok, out = execute_routeros_command(session, "# this is a comment")
    assert ok is True
    assert out == ""

    # Quit
    ok, out = execute_routeros_command(session, "quit")
    assert ok is True
    assert "closed" in out


def test_execute_routeros_command_mocked_pool() -> None:
    """execute_routeros_command executes commands through routeros_api pool."""
    now = time.time()
    session = ConsoleSession(
        token="tok",
        username="admin",
        password="pwd",
        host="127.0.0.1",
        port=8728,
        identity="Test",
        created_at=now,
        last_active=now,
    )

    mock_resource = MagicMock()
    mock_resource.get.return_value = [{"address": "192.168.1.1/24", "interface": "WAN"}]

    mock_client = MagicMock()
    mock_client.get_resource.return_value = mock_resource

    with patch("routeros_api.RouterOsApiPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_pool.get_api.return_value = mock_client
        mock_pool_cls.return_value = mock_pool

        ok, out = execute_routeros_command(session, "/ip address print")
        assert ok is True
        assert "192.168.1.1/24" in out
        assert "WAN" in out
        mock_client.get_resource.assert_called_with("/ip/address")


def test_resolve_command_tokens() -> None:
    """resolve_command_tokens expands CLI abbreviations and formats RouterOS paths correctly."""
    # 'ip add pr' -> /ip/address print
    path, verb, args = resolve_command_tokens(["ip", "add", "pr"])
    assert path == "/ip/address"
    assert verb == "print"
    assert args == {}

    # 'int eth pr' -> /interface/ethernet print
    path, verb, args = resolve_command_tokens(["int", "eth", "pr"])
    assert path == "/interface/ethernet"
    assert verb == "print"

    # 'sys res pr' -> /system/resource print
    path, verb, args = resolve_command_tokens(["sys", "res", "pr"])
    assert path == "/system/resource"
    assert verb == "print"

    # 'ip fi nat pr' -> /ip/firewall/nat print
    path, verb, args = resolve_command_tokens(["ip", "fi", "nat", "pr"])
    assert path == "/ip/firewall/nat"
    assert verb == "print"

    # 'user print' -> /user print
    path, verb, args = resolve_command_tokens(["user", "print"])
    assert path == "/user"
    assert verb == "print"

    # 'ip add add address=1.2.3.4/24 interface=WAN'
    path, verb, args = resolve_command_tokens(
        ["ip", "add", "add", "address=1.2.3.4/24", "interface=WAN"]
    )
    assert path == "/ip/address"
    assert verb == "add"
    assert args["address"] == "1.2.3.4/24"
    assert args["interface"] == "WAN"

