"""Unit tests for RouterOS traffic collector and rate calculation."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from mrtg_cmp.collector import (
    DEFAULT_DOWN_TIMEOUT,
    DEFAULT_MAX_SANE_BPS,
    FLAPPING_WINDOW_SECONDS,
    RouterOSClient,
    TrafficCollector,
    calculate_rate,
)
from mrtg_cmp.db import Database


def test_calculate_rate_first_baseline_sample() -> None:
    """The first sample has no previous reference and should yield 0.0 bps with UP status."""
    result = calculate_rate(
        prev_rx_bytes=None,
        prev_tx_bytes=None,
        prev_epoch=None,
        curr_rx_bytes=100_000,
        curr_tx_bytes=200_000,
        curr_epoch=1_000,
    )
    assert result.rx_bps == 0.0
    assert result.tx_bps == 0.0
    assert result.status == "UP"
    assert result.should_store is True


def test_calculate_rate_normal_progression() -> None:
    """Normal delta calculation converts byte delta to bits per second accurately."""
    # 5 minutes (300s), 300,000,000 bytes delta = 2.4 Gb / 300s = 8,000,000 bps = 8 Mbps
    result = calculate_rate(
        prev_rx_bytes=1_000_000_000,
        prev_tx_bytes=2_000_000_000,
        prev_epoch=1_000.0,
        curr_rx_bytes=1_300_000_000,
        curr_tx_bytes=2_150_000_000,
        curr_epoch=1_300.0,
    )
    assert result.status == "UP"
    assert result.should_store is True
    # rx: 300,000,000 * 8 / 300 = 8,000,000 bps
    assert result.rx_bps == 8_000_000.0
    # tx: 150,000,000 * 8 / 300 = 4,000,000 bps
    assert result.tx_bps == 4_000_000.0


def test_calculate_rate_counter_rollover_or_reboot() -> None:
    """When octet counters decrease, detect RESET and suppress false rate spikes."""
    result = calculate_rate(
        prev_rx_bytes=5_000_000_000,
        prev_tx_bytes=5_000_000_000,
        prev_epoch=1_000.0,
        curr_rx_bytes=100_000,  # router rebooted
        curr_tx_bytes=200_000,
        curr_epoch=1_300.0,
    )
    assert result.status == "RESET"
    assert result.rx_bps == 0.0
    assert result.tx_bps == 0.0
    assert result.should_store is True


def test_calculate_rate_non_positive_interval_is_discarded() -> None:
    """Zero or negative time intervals are discarded to prevent division by zero."""
    result = calculate_rate(
        prev_rx_bytes=100_000,
        prev_tx_bytes=100_000,
        prev_epoch=1_000.0,
        curr_rx_bytes=200_000,
        curr_tx_bytes=200_000,
        curr_epoch=1_000.0,  # same timestamp
    )
    assert result.should_store is False


def test_calculate_rate_implausible_spike_is_guarded() -> None:
    """Rates exceeding the physical threshold (>10 Gbps) are flagged RESET."""
    result = calculate_rate(
        prev_rx_bytes=1_000,
        prev_tx_bytes=1_000,
        prev_epoch=1_000.0,
        curr_rx_bytes=100_000_000_000,  # enormous corrupt jump
        curr_tx_bytes=1_000,
        curr_epoch=1_001.0,  # in 1 second
        max_sane_bps=DEFAULT_MAX_SANE_BPS,
    )
    assert result.status == "RESET"
    assert result.rx_bps == 0.0


class DummyRouterOSClient:
    """Mock RouterOS API client for deterministic offline testing."""

    def __init__(
        self,
        responses: list[dict[str, Any] | Exception],
        timeout: int = 15,
    ) -> None:
        self.responses = list(responses)
        self.disconnect_called = False
        self.timeout = timeout
        self.timeout_history: list[int] = [timeout]

    def set_timeout(self, timeout: int) -> None:
        self.timeout = timeout
        self.timeout_history.append(timeout)

    def query_interface(self, interface_name: str) -> dict[str, Any]:
        if not self.responses:
            raise RuntimeError("No more mock responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def disconnect(self) -> None:
        self.disconnect_called = True


def test_traffic_collector_poll_lifecycle(tmp_path: Path) -> None:
    """Test collector poll_once storing baseline, normal delta, and down event."""
    db_path = tmp_path / "test_traffic.db"
    database = Database(db_path)
    database.initialize()

    mock_client = DummyRouterOSClient(
        [
            {"name": "WAN", "rx_bytes": 1_000_000, "tx_bytes": 2_000_000, "uptime": "1d"},
            {"name": "WAN", "rx_bytes": 1_300_000, "tx_bytes": 2_150_000, "uptime": "1d"},
            ConnectionResetError("Socket reset by peer"),
        ]
    )

    now = 1_700_000_000.0
    collector = TrafficCollector(
        database=database,
        client=mock_client,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    # 1. First sample (baseline)
    s1 = collector.poll_once()
    assert s1 is not None
    assert s1.rx_bps == 0.0
    assert s1.status == "UP"

    # Advance time 300 seconds (5 min)
    now += 300.0
    # rx delta: 300,000 * 8 / 300 = 8,000 bps
    # tx delta: 150,000 * 8 / 300 = 4,000 bps
    s2 = collector.poll_once()
    assert s2 is not None
    assert s2.rx_bps == 8_000.0
    assert s2.tx_bps == 4_000.0
    assert s2.status == "UP"

    # Advance time and trigger connection failure
    now += 300.0
    s3 = collector.poll_once()
    assert s3 is not None
    assert s3.status == "DOWN"
    assert s3.rx_bps == 0.0
    assert mock_client.disconnect_called is True

    # Verify all records stored in SQLite
    samples = database.get_traffic_samples(start=0, end=2_000_000_000)
    assert len(samples) == 3
    assert samples[0]["status"] == "UP"
    assert samples[1]["status"] == "UP"
    assert samples[2]["status"] == "DOWN"


def test_diagnose_failure_scenarios(tmp_path: Path) -> None:
    """_diagnose_failure correctly classifies 5 failure scenarios."""
    from unittest.mock import MagicMock, patch

    from mrtg_cmp.notifier import (
        SCENARIO_DEBIAN_NET_DOWN,
        SCENARIO_MIKROTIK_OFFLINE,
        SCENARIO_ROUTEROS_API_DOWN,
        SCENARIO_TUNNEL_PROVIDER_DOWN,
        SCENARIO_TUNNEL_SESSION_ERROR,
    )

    db = Database(tmp_path / "diag.db")
    db.initialize()
    collector = TrafficCollector(database=db)

    # Scenario 1: Local internet down (socket to 1.1.1.1/8.8.8.8 fails)
    with patch("socket.create_connection", side_effect=OSError("Network unreachable")):
        scenario, reason = collector._diagnose_failure(RuntimeError("timeout"))
        assert scenario == SCENARIO_DEBIAN_NET_DOWN
        assert "DNS" in reason

    # Scenario 2: DNS resolution for tunnel host fails
    def mock_socket_conn(addr: tuple[str, int], timeout: float = 2.0) -> object:
        if addr[0] in ("1.1.1.1", "8.8.8.8"):
            return MagicMock()
        raise OSError("Connection failed")

    with (
        patch("socket.create_connection", side_effect=mock_socket_conn),
        patch("socket.gethostbyname", side_effect=socket.gaierror("No such host")),
    ):
        scenario, reason = collector._diagnose_failure(RuntimeError("timeout"))
        assert scenario == SCENARIO_TUNNEL_PROVIDER_DOWN
        assert "DNS outage" in reason

    # Scenario 3: Portal reports Koneksi Error before tunnel host validation
    def mock_socket_alive(addr: tuple[str, int], timeout: float = 2.0) -> object:
        return MagicMock()

    with (
        patch("socket.create_connection", side_effect=mock_socket_alive),
        patch("socket.gethostbyname", side_effect=AssertionError("portal must be inspected first")),
        patch.object(collector.config, "tunnel_web_email", "user@test.com"),
        patch.object(collector.config, "tunnel_web_password", "<REDACTED>"),
        patch.object(collector.config, "tunnel_web_service_id", "123"),
        patch(
            "mrtg_cmp.tunnel_watchdog.tunnel_watchdog.inspect_member_portal",
            return_value={"success": True, "code": "KONEKSI_ERROR", "needs_restart": True},
        ),
    ):
        scenario, reason = collector._diagnose_failure(RuntimeError("timeout"))
        assert scenario == SCENARIO_TUNNEL_SESSION_ERROR
        assert "Koneksi Error" in reason

    # Scenario 4: Portal reports CONNECTED but port 5336 fails -> RouterOS API Down
    with (
        patch("socket.create_connection", side_effect=mock_socket_alive),
        patch("socket.gethostbyname", return_value="157.66.54.157"),
        patch.object(collector.config, "tunnel_web_email", "user@test.com"),
        patch.object(collector.config, "tunnel_web_password", "<REDACTED>"),
        patch.object(collector.config, "tunnel_web_service_id", "123"),
        patch(
            "mrtg_cmp.tunnel_watchdog.tunnel_watchdog.inspect_member_portal",
            return_value={"success": True, "code": "CONNECTED", "needs_restart": False},
        ),
    ):
        scenario, reason = collector._diagnose_failure(RuntimeError("Connection refused"))
        assert scenario == SCENARIO_ROUTEROS_API_DOWN
        assert "Tunnel is connected [OK]" in reason
        assert "RouterOS API on" in reason

    # Scenario 5: Default MikroTik offline
    with (
        patch("socket.create_connection", side_effect=mock_socket_alive),
        patch("socket.gethostbyname", return_value="157.66.54.157"),
        patch.object(collector.config, "tunnel_web_email", None),
        patch.object(collector.config, "tunnel_web_password", None),
        patch.object(collector.config, "tunnel_web_service_id", None),
    ):
        scenario, reason = collector._diagnose_failure(RuntimeError("timed out"))
        assert scenario == SCENARIO_MIKROTIK_OFFLINE
        assert "timed out" in reason


def test_routeros_client_set_timeout() -> None:
    """RouterOSClient.set_timeout updates client.timeout and pool timeout if connected."""
    from unittest.mock import MagicMock

    client = RouterOSClient(
        host="192.168.88.1",
        username="admin",
        password="",
        timeout=15,
    )
    assert client.timeout == 15

    # Calling set_timeout before connection pool exists
    client.set_timeout(2)
    assert client.timeout == 2

    # Calling set_timeout with active connection pool propagates to pool
    mock_pool = MagicMock()
    client._pool = mock_pool
    client.set_timeout(3)
    assert client.timeout == 3
    mock_pool.set_timeout.assert_called_once_with(3)


def test_traffic_collector_switches_timeout_on_down_and_restores_on_up(tmp_path: Path) -> None:
    """TrafficCollector switches to fast timeout when down and restores normal timeout when UP."""
    from unittest.mock import patch

    db = Database(tmp_path / "timeout.db")
    db.initialize()

    mock_client = DummyRouterOSClient(
        [
            ConnectionResetError("Socket reset 1"),
            ConnectionResetError("Socket reset 2"),
            {"name": "WAN", "rx_bytes": 500_000, "tx_bytes": 600_000, "uptime": "1d"},
        ],
        timeout=15,
    )

    collector = TrafficCollector(
        database=db,
        client=mock_client,  # type: ignore[arg-type]
    )

    with patch.object(collector, "_diagnose_failure", return_value=("MIKROTIK_OFFLINE", "mocked")):
        # First failure: consecutive_failures=1
        collector.poll_once()
        assert collector._is_currently_down is False

        # Second failure: consecutive_failures=2 -> triggers _is_currently_down = True
        collector.poll_once()
        assert collector._is_currently_down is True
        assert mock_client.timeout == DEFAULT_DOWN_TIMEOUT

        # Recovery: poll succeeds while _is_currently_down is True
        collector.poll_once()
        assert collector._is_currently_down is False
        assert mock_client.timeout == 15
        assert DEFAULT_DOWN_TIMEOUT in mock_client.timeout_history


def test_traffic_collector_run_adaptive_interval_down(tmp_path: Path) -> None:
    """TrafficCollector.run uses polling_interval_down when DOWN and polling_interval when UP."""
    from unittest.mock import patch

    db = Database(tmp_path / "run_adaptive.db")
    db.initialize()

    # Client stays DOWN on first poll, then runs second poll
    mock_down_client = DummyRouterOSClient(
        [
            ConnectionResetError("Still down"),
            ConnectionResetError("Still down 2"),
        ],
        timeout=15,
    )

    collector_down = TrafficCollector(
        database=db,
        client=mock_down_client,  # type: ignore[arg-type]
    )

    collector_down._is_currently_down = True
    collector_down.config.polling_interval_down = 5
    collector_down.config.polling_interval = 30
    collector_down.config.wa_alert_enabled = False

    sleep_calls = 0

    def mock_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    with (
        patch.object(
            collector_down,
            "_diagnose_failure",
            return_value=("MIKROTIK_OFFLINE", "mocked"),
        ),
        patch("time.sleep", side_effect=mock_sleep),
    ):
        collector_down.run(max_iterations=2)

    assert sleep_calls == 5

    # Client stays UP on first poll, then runs second poll
    mock_up_client = DummyRouterOSClient(
        [
            {"name": "WAN", "rx_bytes": 100, "tx_bytes": 100, "uptime": "1d"},
            {"name": "WAN", "rx_bytes": 200, "tx_bytes": 200, "uptime": "1d"},
        ],
        timeout=15,
    )

    collector_up = TrafficCollector(
        database=db,
        client=mock_up_client,  # type: ignore[arg-type]
    )

    collector_up._is_currently_down = False
    collector_up.config.polling_interval_down = 5
    collector_up.config.polling_interval = 30
    collector_up.config.wa_alert_enabled = False

    sleep_calls = 0

    with patch("time.sleep", side_effect=mock_sleep):
        collector_up.run(max_iterations=2)

    assert sleep_calls == 30


def test_flapping_detection(tmp_path: Path) -> None:
    """Link transitioning DOWN -> UP within FLAPPING_WINDOW_SECONDS is flagged as flapping."""
    db = Database(tmp_path / "flapping.db")
    db.initialize()

    now = 1_700_000_000.0

    mock_client = DummyRouterOSClient(
        [
            {"name": "WAN", "rx_bytes": 100_000, "tx_bytes": 200_000, "uptime": "1d"},
            {"name": "WAN", "rx_bytes": 110_000, "tx_bytes": 210_000, "uptime": "1d"},
        ],
        timeout=15,
    )

    collector = TrafficCollector(
        database=db,
        client=mock_client,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    # 1. Brief outage: down 30s ago (within 120s window)
    collector._is_currently_down = True
    collector._last_down_epoch = now - 30.0
    assert now - collector._last_down_epoch <= FLAPPING_WINDOW_SECONDS
    sample = collector.poll_once()

    assert sample is not None
    assert collector._flapping_detected is True
    assert "flapping" in collector._last_flapping_reason.lower()
    assert collector._is_currently_down is False

    # 2. Sustained outage: down 300s ago (> 120s window)
    now += 500.0
    collector._is_currently_down = True
    collector._last_down_epoch = now - 300.0
    assert now - collector._last_down_epoch > FLAPPING_WINDOW_SECONDS
    sample2 = collector.poll_once()

    assert sample2 is not None
    assert collector._flapping_detected is False
    assert collector._last_flapping_reason == ""
    assert collector._is_currently_down is False

