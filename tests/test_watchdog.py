"""Tests for TunnelWatchdog network triangulation and auto-healing."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from mrtg_cmp.tunnel_watchdog import MEMBER_BASE_URL, TunnelWatchdog


def _mock_portal_client(status_json: dict[str, object], detail_body: str = "") -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.cookies = {"PHPSESSID": "session-cookie"}

    login_response = MagicMock()
    login_response.json.return_value = {"code": "200"}
    detail_response = MagicMock()
    detail_response.text = detail_body
    status_response = MagicMock()
    status_response.status_code = 200
    status_response.json.return_value = status_json
    client.post.return_value = login_response
    client.get.side_effect = [detail_response, status_response]
    return client


def test_watchdog_diagnose_healthy() -> None:
    """Watchdog returns HEALTHY when target port is reachable."""
    wd = TunnelWatchdog()
    with patch.object(wd, "check_tcp_port", return_value=True):
        diag = wd.diagnose("id-04.tunnel.web.id", 5336)
        assert diag["code"] == "HEALTHY"
        assert diag["status"] == "UP"
        assert diag["port_open"] is True


def test_watchdog_diagnose_port_closed() -> None:
    """Watchdog distinguishes closed port from dead server."""
    wd = TunnelWatchdog()

    # Port closed, but server alive on 443
    def mock_check(host: str, port: int, timeout: float = 3.0) -> bool:
        return port == 443

    with (
        patch.object(wd, "check_tcp_port", side_effect=mock_check),
        patch("socket.gethostbyname", return_value="157.66.54.157"),
    ):
        diag = wd.diagnose("id-04.tunnel.web.id", 5336)
        assert diag["code"] == "PORT_CLOSED"
        assert diag["status"] == "DOWN"
        assert "port 5336 is closed" in diag["message"]


def test_inspect_member_portal_classifies_real_koneksi_error_response() -> None:
    """The member status API's real 405 payload triggers a restart recommendation."""
    wd = TunnelWatchdog()
    client = _mock_portal_client(
        {
            "code": "405",
            "message": "Koneksi Error",
            "response": "<div>Koneksi Error, Silahkan Restart VPN.</div>",
        }
    )

    with patch("httpx.Client", return_value=client):
        result = wd.inspect_member_portal("user@example.test", "password", "123")

    assert result["code"] == "KONEKSI_ERROR"
    assert result["needs_restart"] is True
    assert result["restart_url"] == f"{MEMBER_BASE_URL}/api/api-layanan-diagnosa.php?id=123"
    assert result["cookies"] == {"PHPSESSID": "session-cookie"}


def test_inspect_member_portal_classifies_response_only_koneksi_error() -> None:
    """A Koneksi Error in the response field is enough to trigger auto-healing."""
    wd = TunnelWatchdog()
    client = _mock_portal_client(
        {"code": "200", "message": "", "response": "Koneksi Error, restart VPN"}
    )

    with patch("httpx.Client", return_value=client):
        result = wd.inspect_member_portal("user@example.test", "password", "123")

    assert result["code"] == "KONEKSI_ERROR"
    assert result["needs_restart"] is True


def test_inspect_member_portal_preserves_connected_status() -> None:
    """A connected API status remains CONNECTED and is not marked for restart."""
    wd = TunnelWatchdog()
    client = _mock_portal_client({"code": "200", "message": "Terhubung", "response": ""})

    with patch("httpx.Client", return_value=client):
        result = wd.inspect_member_portal("user@example.test", "password", "123")

    assert result["code"] == "CONNECTED"
    assert result["needs_restart"] is False


def test_inspect_member_portal_does_not_classify_tidak_terhubung_as_connected() -> None:
    """A disconnected status must not be promoted to CONNECTED by substring matching."""
    wd = TunnelWatchdog()
    client = _mock_portal_client({"code": "200", "message": "Tidak Terhubung", "response": ""})

    with patch("httpx.Client", return_value=client):
        result = wd.inspect_member_portal("user@example.test", "password", "123")

    assert result["code"] == "DISCONNECTED"
    assert result["needs_restart"] is False


def test_watchdog_cooldown_guard() -> None:
    """Watchdog honors cooldown timer to prevent spamming restart."""
    wd = TunnelWatchdog()
    wd.last_restart_epoch = time.time() - 60  # restarted 1 min ago (cooldown 30 min)

    with (
        patch.object(wd, "diagnose", return_value={"code": "PORT_CLOSED"}),
        patch.object(
            wd,
            "inspect_member_portal",
            return_value={
                "success": True,
                "needs_restart": True,
                "restart_url": "http://example.com/restart",
                "cookies": {"PHPSESSID": "abc123xyz"},
            },
        ),
    ):
        res = wd.auto_heal_if_needed()
        assert res["action"] == "COOLDOWN"
        assert "cooldown active" in res["message"]


def test_watchdog_auto_heal_restart_success() -> None:
    """Watchdog successfully dispatches restart with authenticated cookies."""
    wd = TunnelWatchdog()
    wd.last_restart_epoch = 0.0  # no recent restart

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": "Restart sukses"}

    with (
        patch.object(wd, "diagnose", return_value={"code": "PORT_CLOSED"}),
        patch.object(
            wd,
            "inspect_member_portal",
            return_value={
                "success": True,
                "needs_restart": True,
                "restart_url": "http://example.com/restart",
                "cookies": {"PHPSESSID": "session_cookie"},
            },
        ),
        patch("httpx.Client.get", return_value=mock_resp),
    ):
        res = wd.auto_heal_if_needed()
        assert res["action"] == "RESTARTED"
        assert "Restart sukses" in res["message"]

