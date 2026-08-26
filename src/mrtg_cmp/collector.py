"""MikroTik RouterOS traffic collector and rate calculation engine."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import routeros_api

from .config import Settings, settings
from .db import Database, TrafficSample

logger = logging.getLogger(__name__)

DEFAULT_MAX_SANE_BPS = 10_000_000_000.0  # 10 Gbps threshold for sanity check


@dataclass(slots=True)
class RateResult:
    """Calculated bandwidth rate and validation status."""

    rx_bps: float
    tx_bps: float
    status: str
    should_store: bool
    reason: str = ""


def calculate_rate(
    prev_rx_bytes: int | None,
    prev_tx_bytes: int | None,
    prev_epoch: int | float | None,
    curr_rx_bytes: int,
    curr_tx_bytes: int,
    curr_epoch: int | float,
    max_sane_bps: float = DEFAULT_MAX_SANE_BPS,
) -> RateResult:
    """Calculate bandwidth rates in bits per second from successive octet samples.

    Handles first sample baseline, counter rollover/router reboot, and
    anomalous spikes exceeding physical thresholds.
    """
    # 1. Baseline check (first sample)
    if prev_rx_bytes is None or prev_tx_bytes is None or prev_epoch is None:
        return RateResult(
            rx_bps=0.0,
            tx_bps=0.0,
            status="UP",
            should_store=True,
            reason="baseline sample",
        )

    delta_time = float(curr_epoch - prev_epoch)

    # 2. Duplicate or inverted timestamp guard
    if delta_time <= 0:
        return RateResult(
            rx_bps=0.0,
            tx_bps=0.0,
            status="UP",
            should_store=False,
            reason=f"non-positive time delta ({delta_time}s)",
        )

    delta_rx = curr_rx_bytes - prev_rx_bytes
    delta_tx = curr_tx_bytes - prev_tx_bytes

    # 3. Counter reset / reboot detection
    if delta_rx < 0 or delta_tx < 0:
        return RateResult(
            rx_bps=0.0,
            tx_bps=0.0,
            status="RESET",
            should_store=True,
            reason="counter decreased (router reboot or counter wrap)",
        )

    rx_bps = (delta_rx * 8.0) / delta_time
    tx_bps = (delta_tx * 8.0) / delta_time

    # 4. Physical sanity guard
    if rx_bps > max_sane_bps or tx_bps > max_sane_bps:
        return RateResult(
            rx_bps=0.0,
            tx_bps=0.0,
            status="RESET",
            should_store=True,
            reason=f"rate exceeded sanity limit (rx={rx_bps:.0f}, tx={tx_bps:.0f})",
        )

    return RateResult(
        rx_bps=rx_bps,
        tx_bps=tx_bps,
        status="UP",
        should_store=True,
        reason="normal sample",
    )


class RouterOSClient:
    """Managed client connection to MikroTik RouterOS API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 8728,
        timeout: int = 15,
        plaintext_login: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.plaintext_login = plaintext_login
        self._pool: routeros_api.RouterOsApiPool | None = None

    def connect(self) -> Any:
        """Establish or return existing connection pool and API instance."""
        if self._pool is None:
            self._pool = routeros_api.RouterOsApiPool(
                host=self.host,
                username=self.username,
                password=self.password,
                port=self.port,
                plaintext_login=self.plaintext_login,
            )
            self._pool.set_timeout(self.timeout)
        return self._pool.get_api()

    def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool is not None:
            try:
                self._pool.disconnect()
            except Exception as exc:
                logger.debug("Error disconnecting RouterOS API pool: %s", exc)
            finally:
                self._pool = None

    def query_interface(self, interface_name: str) -> dict[str, Any]:
        """Query interface counters and router status."""
        api = self.connect()
        interface_resource = api.get_resource("/interface")
        results = interface_resource.get(name=interface_name)

        if not results:
            raise ValueError(f"Interface '{interface_name}' not found on router")

        wan = results[0]

        # Query system uptime
        uptime = "unknown"
        try:
            resource = api.get_resource("/system/resource").get()
            if resource and "uptime" in resource[0]:
                uptime = str(resource[0]["uptime"])
        except Exception as exc:
            logger.debug("Failed to query router uptime: %s", exc)

        return {
            "name": str(wan.get("name", interface_name)),
            "rx_bytes": int(wan.get("rx-byte", 0)),
            "tx_bytes": int(wan.get("tx-byte", 0)),
            "uptime": uptime,
        }


class TrafficCollector:
    """Orchestrates periodic traffic polling from MikroTik into SQLite."""

    def __init__(
        self,
        database: Database | None = None,
        config: Settings | None = None,
        client: RouterOSClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or settings
        self.database = database or Database(self.config.database_path)
        self.database.initialize()
        self.clock = clock

        self.client = client or RouterOSClient(
            host=self.config.routeros_host,
            username=self.config.routeros_username,
            password=self.config.routeros_password,
            port=self.config.routeros_port,
        )

        self._last_rx_bytes: int | None = None
        self._last_tx_bytes: int | None = None
        self._last_epoch: int | float | None = None

        # Watchdog and notification state
        self._consecutive_failures: int = 0
        self._is_currently_down: bool = False
        self._active_down_scenario: str = "MIKROTIK_OFFLINE"
        self._local_net_down_epoch: float | None = None

        # Seed previous state from database if available
        self._seed_last_state()

    def _seed_last_state(self) -> None:
        """Initialize in-memory rate state from the latest recorded sample."""
        latest = self.database.get_latest_sample()
        if latest and latest.get("status") in ("UP", "RESET"):
            self._last_rx_bytes = latest["rx_bytes"]
            self._last_tx_bytes = latest["tx_bytes"]
            self._last_epoch = latest["epoch"]

    def _diagnose_failure(self, exc: Exception) -> tuple[str, str]:
        """Perform 4-layer sequential network triangulation upon polling failure.

        Returns:
            (scenario_code: str, reason_detail: str)
        """
        failure_reason = str(exc)
        host = self.config.routeros_host
        from .notifier import (
            SCENARIO_DEBIAN_NET_DOWN,
            SCENARIO_MIKROTIK_OFFLINE,
            SCENARIO_ROUTEROS_API_DOWN,
            SCENARIO_TUNNEL_PROVIDER_DOWN,
            SCENARIO_TUNNEL_SESSION_ERROR,
        )

        # Layer 1: Test if local host has outbound internet connectivity
        local_net_ok = False
        for test_ip in ("1.1.1.1", "8.8.8.8"):
            try:
                with socket.create_connection((test_ip, 53), timeout=2.0):
                    local_net_ok = True
                    break
            except (TimeoutError, ConnectionRefusedError, OSError):
                continue

        if not local_net_ok:
            return (
                SCENARIO_DEBIAN_NET_DOWN,
                "Local host failed to reach public DNS (1.1.1.1 / 8.8.8.8). Outbound network down.",
            )

        # Layer 2: Inspect tunnel.web.id portal if credentials and service ID are configured.
        portal_available = False
        portal: dict[str, Any] = {}
        if (
            self.config.tunnel_web_email
            and self.config.tunnel_web_password
            and self.config.tunnel_web_service_id
        ):
            try:
                from .tunnel_watchdog import tunnel_watchdog

                portal = tunnel_watchdog.inspect_member_portal()
                portal_available = bool(portal.get("success"))
                if portal.get("code") == "KONEKSI_ERROR":
                    return (
                        SCENARIO_TUNNEL_SESSION_ERROR,
                        f"'Koneksi Error' detected on tunnel #{self.config.tunnel_web_service_id}.",
                    )
                if portal.get("code") == "DISCONNECTED":
                    logger.info(
                        "Tunnel portal reports DISCONNECTED; continuing to MikroTik "
                        "offline classification"
                    )
            except Exception as exc:
                logger.debug("Portal inspection exception during failure diagnosis: %s", exc)

        if portal_available and portal.get("code") == "CONNECTED":
            return (
                SCENARIO_ROUTEROS_API_DOWN,
                (
                    f"Tunnel is connected [OK], but RouterOS API on "
                    f"{host}:{self.config.routeros_port} is closed ({failure_reason})."
                ),
            )

        # A portal response explicitly reporting DISCONNECTED is sufficient to
        # classify the remote router as offline; do not reclassify via transport.
        if portal_available and portal.get("code") == "DISCONNECTED":
            return (
                SCENARIO_MIKROTIK_OFFLINE,
                (
                    f"Tunnel portal reports the remote router disconnected "
                    f"from {self.config.routeros_host}."
                ),
            )

        # Layer 3: Validate provider reachability only after portal inspection.
        try:
            resolved_ip = socket.gethostbyname(host)
        except socket.gaierror:
            if not portal_available:
                return (
                    SCENARIO_TUNNEL_PROVIDER_DOWN,
                    f"Cannot resolve tunnel host '{host}'. Provider DNS outage.",
                )
            resolved_ip = ""

        server_alive = False
        if resolved_ip:
            try:
                with socket.create_connection((resolved_ip, 443), timeout=3.0):
                    server_alive = True
            except (TimeoutError, ConnectionRefusedError, OSError):
                pass

        if not server_alive and not portal_available:
            return (
                SCENARIO_TUNNEL_PROVIDER_DOWN,
                f"Tunnel server '{host}' ({resolved_ip or 'unresolved'}) is unreachable "
                "on port 443.",
            )

        # Layer 4: Portal is healthy/disconnected or provider is reachable, so the
        # RouterOS endpoint itself is treated as offline.
        return (
            SCENARIO_MIKROTIK_OFFLINE,
            f"API connection timed out on {host}:{self.config.routeros_port} ({failure_reason}).",
        )

    def poll_once(self) -> TrafficSample | None:
        """Perform a single polling cycle, compute rates, and persist sample."""
        now_epoch = self.clock()
        now_utc = datetime.fromtimestamp(now_epoch, tz=UTC)
        timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            data = self.client.query_interface(self.config.routeros_interface)
            curr_rx = data["rx_bytes"]
            curr_tx = data["tx_bytes"]
            uptime = data["uptime"]

            rate = calculate_rate(
                prev_rx_bytes=self._last_rx_bytes,
                prev_tx_bytes=self._last_tx_bytes,
                prev_epoch=self._last_epoch,
                curr_rx_bytes=curr_rx,
                curr_tx_bytes=curr_tx,
                curr_epoch=now_epoch,
            )

            sample = TrafficSample(
                timestamp=timestamp_str,
                rx_bytes=curr_rx,
                tx_bytes=curr_tx,
                rx_bps=rate.rx_bps,
                tx_bps=rate.tx_bps,
                epoch=int(now_epoch),
                uptime=uptime,
                status=rate.status,
            )

            if rate.should_store:
                self.database.insert_traffic_sample(sample)
                self._last_rx_bytes = curr_rx
                self._last_tx_bytes = curr_tx
                self._last_epoch = now_epoch

            # Check if recovering from local network outage
            if self._local_net_down_epoch is not None:
                net_outage = now_epoch - self._local_net_down_epoch
                if net_outage >= 30.0:
                    try:
                        from .notifier import (
                            WIB_OFFSET,
                            format_network_restored_notice,
                            send_whatsapp_message,
                        )

                        dis_dt = (
                            datetime.fromtimestamp(self._local_net_down_epoch, tz=UTC) + WIB_OFFSET
                        )
                        rec_dt = datetime.fromtimestamp(now_epoch, tz=UTC) + WIB_OFFSET
                        net_msg = format_network_restored_notice(
                            node_name=f"{self.config.site_name} Monitor Host",
                            disconnected=dis_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            reconnected=rec_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            outage_seconds=net_outage,
                        )
                        send_whatsapp_message(net_msg)
                    except Exception as net_err:
                        logger.debug("Failed to dispatch network restored alert: %s", net_err)
                self._local_net_down_epoch = None

            # Send resolved alert if recovering from a DOWN state
            if self._is_currently_down:
                try:
                    from .notifier import format_resolved_alert, send_whatsapp_message

                    resolved_msg = format_resolved_alert(
                        uptime=uptime or "unknown",
                        rx_bps=rate.rx_bps,
                        tx_bps=rate.tx_bps,
                        router_name=f"WAN ({self.config.uplink_name})",
                        scenario=self._active_down_scenario,
                    )
                    send_whatsapp_message(resolved_msg)
                except Exception as alert_err:
                    logger.warning("Failed to send WhatsApp resolved alert: %s", alert_err)
                self._is_currently_down = False
                self._active_down_scenario = "MIKROTIK_OFFLINE"

            self._consecutive_failures = 0
            return sample

        except Exception as exc:
            logger.warning("Collector failed to poll RouterOS API: %s", exc)
            self._consecutive_failures += 1

            scenario, scenario_reason = self._diagnose_failure(exc)
            if scenario == "DEBIAN_NET_DOWN" and self._local_net_down_epoch is None:
                self._local_net_down_epoch = now_epoch

            # Dispatch DOWN alert when failure threshold is reached
            if (
                self.config.wa_alert_enabled
                and self._consecutive_failures >= self.config.wa_fail_threshold
                and not self._is_currently_down
            ):
                self._is_currently_down = True
                self._active_down_scenario = scenario
                try:
                    from .notifier import format_down_alert, send_whatsapp_message

                    down_msg = format_down_alert(
                        router_name=f"WAN ({self.config.uplink_name})",
                        reason=scenario_reason,
                        scenario=scenario,
                    )
                    send_whatsapp_message(down_msg)
                except Exception as alert_err:
                    logger.warning("Failed to send WhatsApp down alert: %s", alert_err)

            # Check watchdog auto-healing if configured and local internet is available
            if (
                self.config.tunnel_auto_restart
                and self.config.tunnel_web_email
                and scenario != "DEBIAN_NET_DOWN"
            ):
                try:
                    from .tunnel_watchdog import tunnel_watchdog

                    heal_result = tunnel_watchdog.auto_heal_if_needed()
                    if heal_result.get("action") == "RESTARTED":
                        try:
                            from .notifier import format_autoheal_notice, send_whatsapp_message

                            autoheal_msg = format_autoheal_notice(
                                service_id=str(self.config.tunnel_web_service_id or "N/A"),
                                action_message=heal_result.get("message", "VPN restart executed."),
                            )
                            send_whatsapp_message(autoheal_msg)
                        except Exception as wa_err:
                            logger.debug("Failed to dispatch auto-heal WA notice: %s", wa_err)
                except Exception as heal_err:
                    logger.debug("Tunnel auto-heal check error: %s", heal_err)

            # Record a DOWN sample using last known byte counters
            down_sample = TrafficSample(
                timestamp=timestamp_str,
                rx_bytes=self._last_rx_bytes or 0,
                tx_bytes=self._last_tx_bytes or 0,
                rx_bps=0.0,
                tx_bps=0.0,
                epoch=int(now_epoch),
                uptime=None,
                status="DOWN",
            )
            try:
                self.database.insert_traffic_sample(down_sample)
            except Exception as db_exc:
                logger.error("Failed to record DOWN status into database: %s", db_exc)

            # Reset connection pool so next attempt tries a fresh socket
            self.client.disconnect()
            return down_sample

    def run(
        self,
        interval: int | None = None,
        max_iterations: int | None = None,
        stop_event: Any = None,
    ) -> None:
        """Continuously poll at the configured interval until stopped."""
        poll_interval = interval or self.config.polling_interval
        iterations = 0

        logger.info(
            "Starting MRTG collector polling %s:%d (interface: %s, interval: %ds)",
            self.config.routeros_host,
            self.config.routeros_port,
            self.config.routeros_interface,
            poll_interval,
        )

        # Dispatch online startup notice if WhatsApp alerts are enabled
        if self.config.wa_alert_enabled:
            try:
                from .notifier import (
                    WIB_OFFSET,
                    format_power_restored_notice,
                    format_startup_notice,
                    send_whatsapp_message,
                )

                latest_sample = self.database.get_latest_sample()
                now_epoch = self.clock()

                if latest_sample and "epoch" in latest_sample and latest_sample["epoch"]:
                    gap_seconds = now_epoch - float(latest_sample["epoch"])
                    # If gap is 3 minutes (180s) or more, host experienced power outage / cold boot
                    if gap_seconds >= 180.0:
                        last_seen_dt = (
                            datetime.fromtimestamp(latest_sample["epoch"], tz=UTC) + WIB_OFFSET
                        )
                        restored_dt = datetime.fromtimestamp(now_epoch, tz=UTC) + WIB_OFFSET
                        start_msg = format_power_restored_notice(
                            node_name=f"{self.config.site_name} Monitor Host",
                            last_seen=last_seen_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            restored=restored_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            downtime_seconds=gap_seconds,
                        )
                    else:
                        start_msg = format_startup_notice(
                            node_name=f"{self.config.site_name} Monitor Host",
                            target=f"{self.config.routeros_host}:{self.config.routeros_port}",
                        )
                else:
                    start_msg = format_startup_notice(
                        node_name=f"{self.config.site_name} Monitor Host",
                        target=f"{self.config.routeros_host}:{self.config.routeros_port}",
                    )

                send_whatsapp_message(start_msg)
            except Exception as start_err:
                logger.debug("Failed to dispatch startup notification: %s", start_err)

        while True:
            if stop_event is not None and stop_event.is_set():
                break

            self.poll_once()
            iterations += 1

            if max_iterations is not None and iterations >= max_iterations:
                break

            # Sleep in 1-second chunks to respond quickly to stop_event
            for _ in range(poll_interval):
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(1)

        # Dispatch graceful shutdown notice if WhatsApp alerts are enabled
        if self.config.wa_alert_enabled:
            try:
                from .notifier import format_shutdown_notice, send_whatsapp_message

                stop_msg = format_shutdown_notice(
                    node_name=f"{self.config.site_name} Monitor Host",
                    reason="Daemon stopping for maintenance or service reload.",
                )
                send_whatsapp_message(stop_msg)
            except Exception as stop_err:
                logger.debug("Failed to dispatch shutdown notification: %s", stop_err)

        self.client.disconnect()
        logger.info("Collector stopped after %d iterations", iterations)
