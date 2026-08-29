"""Map marker construction for the dashboard's interactive Leaflet view.

Phase 61. This module is deliberately free of web-framework concerns: it turns
the two monitoring sources the dashboard already renders -- Netcare branch
links and Telkomsel Orbit modems -- into a flat list of JSON-ready markers,
and it owns the only interesting rule in the feature, which colour a marker
gets.

Colour precedence (FR-61.2), highest first:

1. **Red / orange** for anything an operator would act on. A failed capture or
   a link that is down is red; a stale capture or an idle modem is orange.
2. **Blue** for a healthy link whose service type is ``Astinet`` or
   ``Metro-E``, so the dedicated-access footprint reads differently from the
   shared VPN-IP estate at a glance.
3. **Green** for everything else that is healthy.

Health outranks service on purpose. A degraded Astinet link is drawn orange,
not blue, because "is it up" is the question the map is answered with before
"what does it carry".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..orbit.scraper import OrbitModemStatus

#: Marker colours. Exported so the dashboard legend and the popup stylesheet
#: read from the same palette rather than re-typing hex codes.
MARKER_COLOR_OK = "#22c55e"  # green
MARKER_COLOR_DEGRADED = "#f59e0b"  # orange
MARKER_COLOR_DOWN = "#ef4444"  # red
MARKER_COLOR_SERVICE = "#3b82f6"  # blue

#: Legend rows, in the order the dashboard renders them.
MAP_LEGEND: tuple[tuple[str, str], ...] = (
    (MARKER_COLOR_OK, "OK / Active"),
    (MARKER_COLOR_SERVICE, "Astinet / Metro-E"),
    (MARKER_COLOR_DEGRADED, "Degraded / Idle"),
    (MARKER_COLOR_DOWN, "Down / Error"),
)

#: Service types drawn in the service blue rather than plain green.
SERVICE_COLOR_TYPES: frozenset[str] = frozenset({"Astinet", "Metro-E"})

#: Netcare capture statuses that mean "something is wrong" (FR-61.2).
_NETCARE_DOWN_STATUSES: frozenset[str] = frozenset({"error", "down"})
_NETCARE_DEGRADED_STATUSES: frozenset[str] = frozenset({"stale", "pending"})

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


def netcare_marker_color(status: str, service_type: str) -> str:
    """Pick the marker colour for a Netcare link from status and service."""

    key = (status or "").strip().lower()
    if key in _NETCARE_DOWN_STATUSES:
        return MARKER_COLOR_DOWN
    if key in _NETCARE_DEGRADED_STATUSES:
        return MARKER_COLOR_DEGRADED
    if service_type in SERVICE_COLOR_TYPES:
        return MARKER_COLOR_SERVICE
    return MARKER_COLOR_OK


def orbit_marker_color(status: str) -> str:
    """Pick the marker colour for an Orbit modem.

    Orbit has no service dimension, so this is purely ``ACTIVE`` vs anything
    else; ``IDLE`` is the common non-active state and lands on orange.
    """

    return MARKER_COLOR_OK if (status or "").strip().upper() == "ACTIVE" else MARKER_COLOR_DEGRADED


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
        "color": netcare_marker_color(status, service_type),
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
    return {
        "id": f"orbit:{modem.no}",
        "kind": "orbit",
        "name": modem.location or f"Orbit {modem.no}",
        "identifier_label": "IMEI",
        "identifier": modem.imei,
        "status": modem_status,
        "status_label": modem_status.upper() or "UNKNOWN",
        "service_type": "",
        "color": orbit_marker_color(modem_status),
        "latitude": lat,
        "longitude": lon,
        "detail": f"Modem {modem.no}",
        "last_polled": status.last_scraped_at,
        "card_selector": f'.orbit-card[data-modem-no="{modem.no}"]',
        "card_url": "/orbit",
    }


def build_map_markers(
    netcare_cards: Iterable[Mapping[str, Any]],
    orbit_statuses: Iterable[OrbitModemStatus],
) -> list[dict[str, Any]]:
    """Return every located Netcare link and Orbit modem as a map marker.

    Netcare comes first so the map opens on the branch-link estate, which is
    the larger and higher-priority footprint.
    """

    markers: list[dict[str, Any]] = []
    for card in netcare_cards:
        marker = netcare_marker(card)
        if marker is not None:
            markers.append(marker)
    for status in orbit_statuses:
        marker = orbit_marker(status)
        if marker is not None:
            markers.append(marker)
    return markers