"""Map marker construction for the dashboard's interactive Leaflet view.

Phase 66. This module is deliberately free of web-framework concerns: it turns
the monitoring sources the dashboard already renders -- Netcare branch
links, Telkomsel Orbit modems, and the MikroTik router -- into a flat list
of JSON-ready markers using binary health coloring (health only, no service
dimension).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..orbit.scraper import NO_PACKAGE_EXPIRY_STR, OrbitModemStatus

#: Marker colours. Exported so the dashboard legend and the popup stylesheet
#: read from the same palette rather than re-typing hex codes.
MARKER_COLOR_OK = "#22c55e"  # green
MARKER_COLOR_DOWN = "#ef4444"  # red

#: Legend rows, in the order the dashboard renders them.
MAP_LEGEND: tuple[tuple[str, str], ...] = (
    (MARKER_COLOR_OK, "Up / Active"),
    (MARKER_COLOR_DOWN, "Down / Error"),
)

#: Netcare capture statuses that mean "something is wrong".
_NETCARE_DOWN_STATUSES: frozenset[str] = frozenset({"error", "down"})

#: Router coordinates.
ROUTER_COORDINATES = (-6.3375, 106.7647)

#: ``(0, 0)`` is the classic spreadsheet "no coordinates" placeholder. Dropping
#: the marker is better than planting one in the Gulf of Guinea.
_NULL_ISLAND = (0.0, 0.0)


def coordinate_or_none(
    latitude: object,
    longitude: object,
) -> tuple[float, float] | None:
    """Return a validated ``(lat, lon)`` pair, or ``None`` if unusable.

    Booleans are rejected explicitly because ``bool`` is an ``int`` subclass and
    ``True`` would otherwise plot at latitude 1.
    """

    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    if latitude is None or longitude is None:
        return None
    try:
        lat = float(latitude)  # type: ignore[arg-type]
        lon = float(longitude)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if (lat, lon) == _NULL_ISLAND:
        return None
    return lat, lon


def netcare_marker_color(status: str) -> str:
    """Pick the marker colour for a Netcare link from status."""

    key = (status or "").strip().lower()
    if key in _NETCARE_DOWN_STATUSES:
        return MARKER_COLOR_DOWN
    return MARKER_COLOR_OK


def orbit_marker_color(status: str, error: str = "", earliest_expiry_str: str = "") -> str:
    """Pick the marker colour for an Orbit modem.

    Earliest expiry string equal to NO_PACKAGE_EXPIRY_STR OR truthy error -> DOWN;
    otherwise OK (ACTIVE and IDLE both green).
    """

    if error or earliest_expiry_str == NO_PACKAGE_EXPIRY_STR:
        return MARKER_COLOR_DOWN
    return MARKER_COLOR_OK



#: Default coordinates per regional airport for targets that have not
#: yet had explicit GPS coordinates saved in the catalog.
REGION_FALLBACK_COORDINATES: dict[str, tuple[float, float]] = {
    "CGK": (-6.1256, 106.6559),
    "SUB": (-7.3798, 112.7876),
    "UPG": (-5.0617, 119.5540),
    "DPS": (-8.7482, 115.1672),
    "BPN": (-1.2683, 116.8944),
    "SENTUL": (-6.5414, 106.8778),
}

BASE_STATION_COORDINATES = (-6.1256, 106.6559)


def netcare_marker(card: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build one Netcare marker, or ``None`` when the link has no coordinates.

    ``card`` is the dict produced by ``_netcare_context`` (i.e. the target's
    ``as_dict()`` merged with its cache entry).
    """

    position = coordinate_or_none(card.get("latitude"), card.get("longitude"))
    if position is None:
        return None
    lat, lon = position

    target = str(card.get("target", ""))
    status = str(card.get("status", "") or "pending")
    service_type = str(card.get("service_type", "") or "")
    return {
        "id": f"netcare:{target}",
        "kind": "netcare",
        "name": str(card.get("name", "") or target),
        "identifier_label": "SID",
        "identifier": target,
        "status": status,
        "status_label": status.upper(),
        "service_type": service_type,
        "color": netcare_marker_color(status),
        "latitude": lat,
        "longitude": lon,
        "detail": str(card.get("region_label", "") or service_type),
        "last_polled": str(card.get("last_scraped_at", "") or ""),
        # Cards are rendered client-side, so the map scrolls by selector rather
        # than by a pre-rendered element id.
        "card_selector": f'.netcare-card[data-target="{target}"]',
        "card_url": "/",
    }


def orbit_marker(status: OrbitModemStatus) -> dict[str, Any] | None:
    """Build one Orbit modem marker, or ``None`` without coordinates."""

    modem = status.target
    position = coordinate_or_none(modem.latitude, modem.longitude)
    if position is None:
        return None
    lat, lon = position

    modem_status = modem.status or ""
    color = orbit_marker_color(
        modem_status,
        error=str(status.error or ""),
        earliest_expiry_str=str(status.earliest_expiry_str or ""),
    )
    return {
        "id": f"orbit:{modem.no}",
        "kind": "orbit",
        "name": modem.location or f"Orbit {modem.no}",
        "identifier_label": "IMEI",
        "identifier": modem.imei,
        "status": modem_status,
        "status_label": modem_status.upper() or "UNKNOWN",
        "service_type": "",
        "color": color,
        "latitude": lat,
        "longitude": lon,
        "detail": f"Modem {modem.no}",
        "last_polled": status.last_scraped_at,
        "card_selector": f'.orbit-card[data-modem-no="{modem.no}"]',
        "card_url": "/orbit",
    }


def router_marker(sample: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    """Build the router marker (always plotted, never None)."""

    status = str((sample or {}).get("status", "")).strip() or "NO DATA"
    status_upper = status.upper()
    color = MARKER_COLOR_OK if status_upper in {"UP", "RESET"} else MARKER_COLOR_DOWN
    timestamp = (sample or {}).get("timestamp_wib") or (sample or {}).get("timestamp") or ""
    if not timestamp and sample and "epoch" in sample:
        from datetime import UTC, datetime, timedelta
        ts_utc = datetime.fromtimestamp(sample["epoch"], tz=UTC)
        timestamp = (ts_utc + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")

    return {
        "id": "router:gateway",
        "kind": "router",
        "name": name,
        "identifier_label": "Interface",
        "identifier": "WAN",
        "status": status,
        "status_label": status_upper,
        "service_type": "",
        "color": color,
        "latitude": ROUTER_COORDINATES[0],
        "longitude": ROUTER_COORDINATES[1],
        "detail": "MikroTik WAN uplink",
        "last_polled": str(timestamp or ""),
        "card_selector": "#live-traffic-section",
        "card_url": "/",
    }


def pondok_cabe_marker(sample: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    """Alias for router_marker for backward compatibility."""
    return router_marker(sample, name)


def build_map_markers(
    netcare_cards: Iterable[Mapping[str, Any]],
    orbit_statuses: Iterable[OrbitModemStatus],
    router_sample: Mapping[str, Any] | None = None,
    router_name: str = "MRTG Router",
) -> list[dict[str, Any]]:
    """Return every located Netcare link, Orbit modem, and the router as map markers."""

    markers: list[dict[str, Any]] = []
    for card in netcare_cards:
        marker = netcare_marker(card)
        if marker is not None:
            markers.append(marker)
    for status in orbit_statuses:
        marker = orbit_marker(status)
        if marker is not None:
            markers.append(marker)
    markers.append(router_marker(router_sample, router_name))
    return markers