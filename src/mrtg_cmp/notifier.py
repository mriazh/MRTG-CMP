"""WhatsApp notification dispatch engine using GOWA REST gateway."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import settings
from .graph_renderer import format_engineering_bits

logger = logging.getLogger(__name__)

WIB_OFFSET = timedelta(hours=7)


def _now_wib_str() -> str:
    """Return formatted current time in WIB."""
    now_wib = datetime.now(UTC) + WIB_OFFSET
    return now_wib.strftime("%Y-%m-%d %H:%M:%S")


def is_recent_reboot(uptime: str | None) -> bool:
    """Determine whether router recently rebooted (e.g. power outage) vs long-running uptime."""
    if not uptime:
        return False

    u = uptime.strip().lower()

    # If it contains weeks ('w') or days ('d'), it has been running for days/weeks
    if "w" in u or "d" in u:
        return False

    # If format is HH:MM:SS (e.g. 00:04:12)
    if ":" in u:
        parts = u.split(":")
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            # If less than 1 hour, treat as fresh reboot
            return hours == 0 and minutes < 30
        except (ValueError, IndexError):
            pass

    # If format is XhYmZs or XmYs
    if "h" in u:
        try:
            hours_val = int(u.split("h")[0].strip())
            return hours_val == 0
        except ValueError:
            return False

    # Only minutes/seconds (e.g. 5m20s or 45s)
    return bool("m" in u or "s" in u)


# Scenario Constants
SCENARIO_DEBIAN_NET_DOWN = "DEBIAN_NET_DOWN"
SCENARIO_TUNNEL_PROVIDER_DOWN = "TUNNEL_PROVIDER_DOWN"
SCENARIO_TUNNEL_SESSION_ERROR = "TUNNEL_SESSION_ERROR"
SCENARIO_MIKROTIK_OFFLINE = "MIKROTIK_OFFLINE"
SCENARIO_ROUTEROS_API_DOWN = "ROUTEROS_API_DOWN"


def format_down_alert(
    router_name: str | None = None,
    location: str | None = None,
    reason: str = "Tunnel link dropped / Router API polling unresponsive.",
    timestamp: str | None = None,
    scenario: str = SCENARIO_MIKROTIK_OFFLINE,
    node_name: str | None = None,
) -> str:
    """Compose structured incident alert message tailored to the diagnosed failure scenario."""
    ts = timestamp or _now_wib_str()
    r_name = router_name or f"WAN ({settings.uplink_name})"
    loc = location or f"{settings.site_name} ({settings.location_name})"
    node = node_name or f"{settings.site_name} Monitor Host"

    if scenario == SCENARIO_DEBIAN_NET_DOWN:
        return (
            "🚨 *[ALERT] DEBIAN MONITOR NETWORK DOWN!*\n\n"
            f"💻 *Node:* {node}\n"
            "🏢 *Location:* Office LAN / Headquarters\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* OUTBOUND NETWORK UNREACHABLE\n\n"
            f"⚠️ *Details:* {reason}\n"
            "🔍 *Probable Cause:* Office network / ISP connection lost or local gateway down.\n"
            "🔧 *Action:* Check local office router / switch / internet connection.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_TUNNEL_PROVIDER_DOWN:
        return (
            "🚨 *[ALERT] TUNNEL PROVIDER SERVICE DOWN!*\n\n"
            f"📍 *Target:* {r_name}\n"
            "🏢 *Provider:* tunnel.web.id Infrastructure\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* TUNNEL GATEWAY UNREACHABLE\n\n"
            f"⚠️ *Details:* {reason}\n"
            "🔍 *Probable Cause:* tunnel.web.id server outage or DNS resolution failure.\n"
            "🔧 *Action:* Check https://status.tunnel.web.id or contact provider support.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_TUNNEL_SESSION_ERROR:
        return (
            "⚠️ *[ALERT] TUNNEL SESSION SUSPENDED!*\n\n"
            f"📍 *Target:* {r_name}\n"
            "🏢 *Provider:* tunnel.web.id Portal\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* SESSION ERROR (Koneksi Error)\n\n"
            f"⚠️ *Details:* {reason}\n"
            "🔍 *Probable Cause:* Ghost session collision on tunnel.web.id server.\n"
            "🤖 *Auto-Healing:* Auto-restart triggered via portal API.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_ROUTEROS_API_DOWN:
        return (
            "🚨 *[ALERT] MIKROTIK API SERVICE UNRESPONSIVE!*\n\n"
            f"📍 *Device:* {r_name}\n"
            f"🏢 *Location:* {loc}\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* API SERVICE CLOSED (WAN Internet Online)\n\n"
            f"⚠️ *Details:* {reason}\n"
            "🔍 *Probable Cause:* RouterOS API service (/ip service api port 8728) is hung, "
            "crashed, or blocked by firewall, while branch WAN internet and SSTP tunnel "
            "remain connected.\n"
            "🔧 *Action:* Verify RouterOS API service status via Winbox or "
            "console (/ip service print).\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    # Default: SCENARIO_MIKROTIK_OFFLINE
    return (
        "🚨 *[ALERT] MIKROTIK GATEWAY OFFLINE!*\n\n"
        f"📍 *Device:* {r_name}\n"
        f"🏢 *Location:* {loc}\n"
        f"⏱ *Time:* {ts} WIB\n"
        "🔌 *Status:* DOWN (Unreachable)\n\n"
        f"⚠️ *Details:* {reason}\n"
        f"🔍 *Probable Cause:* Facility power outage at {settings.location_name} "
        "OR ISP uplink drop.\n"
        f"🔧 *Action:* Inquire on-site {settings.location_name} facility power status "
        "or contact ISP support.\n\n"
        "📊 *Dashboard:* https://mrtg.mriazh.my.id"
    )


def format_resolved_alert(
    uptime: str,
    rx_bps: float = 0.0,
    tx_bps: float = 0.0,
    router_name: str | None = None,
    location: str | None = None,
    timestamp: str | None = None,
    scenario: str = SCENARIO_MIKROTIK_OFFLINE,
    node_name: str | None = None,
) -> str:
    """Compose structured resolution message tailored to the initial failure scenario."""
    ts = timestamp or _now_wib_str()
    r_name = router_name or f"WAN ({settings.uplink_name})"
    loc = location or f"{settings.site_name} ({settings.location_name})"
    node = node_name or f"{settings.site_name} Monitor Host"
    in_fmt = format_engineering_bits(rx_bps)
    out_fmt = format_engineering_bits(tx_bps)

    if scenario == SCENARIO_TUNNEL_PROVIDER_DOWN:
        return (
            "✅ *[RESOLVED] TUNNEL PROVIDER SERVICE RESTORED!*\n\n"
            f"📍 *Target:* {r_name}\n"
            "🏢 *Provider:* tunnel.web.id Infrastructure\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* UP (Connected)\n"
            f"⏳ *Uptime:* {uptime}\n"
            f"📈 *Live Traffic:* In: {in_fmt} | Out: {out_fmt}\n\n"
            "📝 *System Diagnosis:*\n"
            "🌐 *TUNNEL GATEWAY RESTORED*\n"
            "tunnel.web.id server connectivity recovered. "
            "SSTP tunnel and RouterOS API polling are fully re-established.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_TUNNEL_SESSION_ERROR:
        return (
            "✅ *[RESOLVED] TUNNEL SESSION RECOVERED!*\n\n"
            f"📍 *Target:* {r_name}\n"
            "🏢 *Provider:* tunnel.web.id Portal\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* UP (Connected)\n"
            f"⏳ *Uptime:* {uptime}\n"
            f"📈 *Live Traffic:* In: {in_fmt} | Out: {out_fmt}\n\n"
            "📝 *System Diagnosis:*\n"
            "🔄 *TUNNEL SESSION RESTORED*\n"
            "Tunnel session on tunnel.web.id recovered successfully. "
            "RouterOS API communication is active.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_DEBIAN_NET_DOWN:
        return (
            "✅ *[RESOLVED] DEBIAN MONITOR NETWORK RESTORED!*\n\n"
            f"💻 *Node:* {node}\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* UP (Connected)\n"
            f"⏳ *Uptime:* {uptime}\n"
            f"📈 *Live Traffic:* In: {in_fmt} | Out: {out_fmt}\n\n"
            "📝 *System Diagnosis:*\n"
            "🌐 *LOCAL NETWORK RECOVERED*\n"
            "Debian host outbound internet connectivity restored. "
            "RouterOS API polling resumed.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    if scenario == SCENARIO_ROUTEROS_API_DOWN:
        return (
            "✅ *[RESOLVED] MIKROTIK API SERVICE RESTORED!*\n\n"
            f"📍 *Device:* {r_name}\n"
            f"🏢 *Location:* {loc}\n"
            f"⏱ *Time:* {ts} WIB\n"
            "🔌 *Status:* UP (Connected)\n"
            f"⏳ *Uptime:* {uptime}\n"
            f"📈 *Live Traffic:* In: {in_fmt} | Out: {out_fmt}\n\n"
            "📝 *System Diagnosis:*\n"
            "⚙️ *ROUTEROS API SERVICE RESTORED*\n"
            "RouterOS API service (/ip service api) resumed accepting connections. "
            "WAN uplink remained stable throughout.\n\n"
            "📊 *Dashboard:* https://mrtg.mriazh.my.id"
        )

    # Default: SCENARIO_MIKROTIK_OFFLINE
    is_reboot = is_recent_reboot(uptime)
    if is_reboot:
        diagnosis = (
            "⚡ *RECENT POWER OUTAGE / REBOOT*\n"
            "Router has just completed cold boot. "
            "Network uplink and tunnel have fully recovered."
        )
    else:
        diagnosis = (
            "🌐 *TRANSIENT ISP / TUNNEL DROPOUT*\n"
            "Router remained powered on (system uptime sustained). "
            f"ISP uplink ({settings.uplink_name}) or tunnel session dropped momentarily "
            "and is now re-established."
        )

    return (
        "✅ *[RESOLVED] MIKROTIK GATEWAY ONLINE!*\n\n"
        f"📍 *Device:* {r_name}\n"
        f"🏢 *Location:* {loc}\n"
        f"⏱ *Time:* {ts} WIB\n"
        "🔌 *Status:* UP (Connected)\n"
        f"⏳ *Uptime:* {uptime}\n"
        f"📈 *Live Traffic:* In: {in_fmt} | Out: {out_fmt}\n\n"
        f"📝 *System Diagnosis:*\n{diagnosis}\n\n"
        "📊 *Dashboard:* https://mrtg.mriazh.my.id"
    )


def format_startup_notice(
    node_name: str | None = None,
    target: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Compose informational daemon startup / system online notice."""
    ts = timestamp or _now_wib_str()
    node = node_name or f"{settings.site_name} Monitor Host"
    tgt = target or f"Router Gateway ({settings.uplink_name})"
    return (
        "🟢 *[SYSTEM ONLINE] MRTG Collector Daemon Started*\n\n"
        f"💻 *Node:* {node}\n"
        f"🎯 *Target:* {tgt}\n"
        f"⏱ *Time:* {ts} WIB\n"
        "Status: Collector daemon active and monitoring traffic samples.\n\n"
        "📊 *Dashboard:* https://mrtg.mriazh.my.id"
    )


def format_shutdown_notice(
    node_name: str | None = None,
    reason: str = "Daemon stopping for update or maintenance restart.",
    timestamp: str | None = None,
) -> str:
    """Compose informational daemon graceful shutdown notice."""
    ts = timestamp or _now_wib_str()
    node = node_name or f"{settings.site_name} Monitor Host"
    return (
        "⏸️ *[SYSTEM STOPPED] MRTG Collector Daemon Stopping*\n\n"
        f"💻 *Node:* {node}\n"
        f"⏱ *Time:* {ts} WIB\n"
        f"ℹ️ *Note:* {reason}\n"
        "If intentional (deploy/update), recovery notice will follow automatically."
    )


def format_autoheal_notice(
    service_id: str,
    action_message: str,
    timestamp: str | None = None,
) -> str:
    """Compose informational auto-heal notification when tunnel restart is triggered."""
    ts = timestamp or _now_wib_str()
    return (
        "🔄 *[AUTO-HEAL] TUNNEL RESTART TRIGGERED!*\n\n"
        f"📍 *Service ID:* #{service_id}\n"
        f"⏱ *Time:* {ts} WIB\n"
        "⚠️ *Issue:* 'Koneksi Error' detected on tunnel.web.id portal.\n"
        f"ℹ️ *Action:* {action_message}\n"
        "Waiting for MikroTik to reconnect SSTP tunnel..."
    )


def format_duration(seconds: float | int) -> str:
    """Format duration in seconds to a human-readable English string."""
    total_sec = max(0, int(seconds))
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def format_power_restored_notice(
    node_name: str | None = None,
    last_seen: str = "",
    restored: str = "",
    downtime_seconds: float = 0.0,
) -> str:
    """Compose alert when host boots up after prolonged downtime / power outage."""
    node = node_name or f"{settings.site_name} Monitor Host"
    dur_str = format_duration(downtime_seconds)
    return (
        "🟢 *[SYSTEM RESTORED] DEBIAN HOST POWER RECOVERED!*\n\n"
        f"💻 *Node:* {node}\n"
        f"⏱ *Last Sample:* {last_seen} WIB\n"
        f"⏱ *Restored:* {restored} WIB\n"
        f"⏳ *Downtime Duration:* {dur_str}\n\n"
        "📝 *Diagnosis:*\n"
        "⚡ Host workstation experienced power outage or cold boot restart.\n"
        "Monitoring daemon has resumed active polling.\n\n"
        "📊 *Dashboard:* https://mrtg.mriazh.my.id"
    )


def format_network_restored_notice(
    node_name: str | None = None,
    disconnected: str = "",
    reconnected: str = "",
    outage_seconds: float = 0.0,
) -> str:
    """Compose alert when local office internet connectivity recovers."""
    node = node_name or f"{settings.site_name} Monitor Host"
    dur_str = format_duration(outage_seconds)
    return (
        "🌐 *[NETWORK RESTORED] OFFICE INTERNET RECOVERED!*\n\n"
        f"💻 *Node:* {node}\n"
        f"⏱ *Disconnect Time:* {disconnected} WIB\n"
        f"⏱ *Reconnect Time:* {reconnected} WIB\n"
        f"⏳ *Outage Duration:* {dur_str}\n\n"
        "📝 *Diagnosis:*\n"
        "🌐 Local office network / ISP connection restored while host remained powered on.\n"
        "Outbound connectivity to public DNS and tunnel is re-established.\n\n"
        "📊 *Dashboard:* https://mrtg.mriazh.my.id"
    )


def send_whatsapp_message(
    message: str,
    target_jid: str | None = None,
    gateway_url: str | None = None,
    device_id: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """Send text message via GOWA WhatsApp REST gateway."""
    if not settings.wa_alert_enabled and target_jid is None:
        return False

    url_base = gateway_url or settings.wa_gateway_url
    target = target_jid or settings.wa_target_jid
    dev_id = device_id or settings.wa_device_id

    if not url_base or not target:
        logger.warning("WhatsApp alert skipped: gateway URL or target JID not set")
        return False

    endpoint = f"{url_base.rstrip('/')}/send/message"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if dev_id:
        headers["X-Device-Id"] = dev_id

    payload: dict[str, Any] = {
        "phone": target,
        "message": message,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                logger.info("WhatsApp notification sent successfully to %s", target)
                return True
            else:
                logger.warning(
                    "WhatsApp gateway returned HTTP %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
    except Exception as e:
        logger.warning("Failed to dispatch WhatsApp message via %s: %s", endpoint, e)
        return False
