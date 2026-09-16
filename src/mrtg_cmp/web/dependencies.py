"""Shared helpers, context builders, and template objects for the web routes.

Every route module in :mod:`mrtg_cmp.web.routes` imports from here, never from
:mod:`mrtg_cmp.web.app`, so the import graph stays acyclic: routes and this
module know about the domain packages; ``app`` only knows about the routers.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..netcare.query import NetcareQueryTracker, estimate_seconds
from ..netcare.service import build_service_from_settings
from ..netcare.targets import (
    DEFAULT_TARGETS,
    REGION_ADDRESSES,
    REGION_ORDER,
    region_counts,
    region_label,
    service_badge_class,
    service_counts,
    service_description,
    service_label,
)
from ..orbit.burn_rate import QuotaBurnRateTracker
from ..orbit.cache import OrbitCache
from ..orbit.scraper import OrbitModemStatus
from ..orbit.service import OrbitService
from ..orbit.targets import (
    filter_stats,
    resolve_orbit_catalog,
)
from .map_data import MAP_LEGEND, build_map_markers

WIB_OFFSET = timedelta(hours=7)
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
NETCARE_IMAGE_MAX_AGE_SECONDS = 60

#: Single Jinja template instance shared by every route module.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _now_wib() -> datetime:
    """Return current datetime in WIB (UTC+7)."""
    return datetime.now(UTC) + WIB_OFFSET


def resolve_time_range(
    preset: str | None = None,
    start_str: str | None = None,
    end_str: str | None = None,
    fullday: str | None = None,
) -> tuple[int, int, str, str, str]:
    """Resolve preset or custom datetime inputs into start/end epochs and labels.

    Returns:
        (start_epoch, end_epoch, display_start, display_end, active_preset).
        All human-facing timestamps assume WIB (UTC+7).
    """
    now = _now_wib()

    if fullday:
        # Full day single-date selection (00:00:00 to 23:59:59 WIB)
        try:
            f_clean = fullday.strip()[:10]
            d = datetime.strptime(f_clean, "%Y-%m-%d")
            st_wib = datetime(d.year, d.month, d.day, 0, 0, 0)
            et_wib = datetime(d.year, d.month, d.day, 23, 59, 59)
            active = "fullday"
        except Exception:
            st_wib = datetime(now.year, now.month, now.day, 0, 0, 0)
            et_wib = now
            active = "today"
    elif preset == "1h":
        et_wib = now
        st_wib = now - timedelta(hours=1)
        active = "1h"
    elif preset == "3h":
        et_wib = now
        st_wib = now - timedelta(hours=3)
        active = "3h"
    elif preset == "6h":
        et_wib = now
        st_wib = now - timedelta(hours=6)
        active = "6h"
    elif preset == "12h":
        et_wib = now
        st_wib = now - timedelta(hours=12)
        active = "12h"
    elif preset == "yesterday":
        yesterday = now - timedelta(days=1)
        st_wib = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0)
        et_wib = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59)
        active = "yesterday"
    elif preset == "24h":
        et_wib = now
        st_wib = now - timedelta(hours=24)
        active = "24h"
    elif preset == "7d":
        et_wib = now
        st_wib = now - timedelta(days=7)
        active = "7d"
    elif preset == "month":
        st_wib = datetime(now.year, now.month, 1, 0, 0, 0)
        et_wib = now
        active = "month"
    elif preset == "today":
        st_wib = datetime(now.year, now.month, now.day, 0, 0, 0)
        et_wib = now
        active = "today"
    elif start_str and end_str:
        # Custom range from form (format: YYYY-MM-DDTHH:MM or full ISO)
        st_clean = start_str.strip().replace("T", " ")
        et_clean = end_str.strip().replace("T", " ")
        try:
            st_wib = datetime.fromisoformat(st_clean)
            et_wib = datetime.fromisoformat(et_clean)
            active = "custom"
        except Exception:
            # Fallback to today on parse failure
            st_wib = datetime(now.year, now.month, now.day, 0, 0, 0)
            et_wib = now
            active = "today"
    else:
        # Default: today (00:00:00 to now)
        st_wib = datetime(now.year, now.month, now.day, 0, 0, 0)
        et_wib = now
        active = "today"

    # Convert WIB naive datetimes to UTC epochs
    # st_wib is at UTC+7, so epoch = (st_wib - 7 hours) in UTC
    st_utc = st_wib - WIB_OFFSET
    et_utc = et_wib - WIB_OFFSET
    start_epoch = int(st_utc.replace(tzinfo=UTC).timestamp())
    end_epoch = int(et_utc.replace(tzinfo=UTC).timestamp())

    display_st = st_wib.strftime("%Y-%m-%d %H:%M:%S")
    display_et = et_wib.strftime("%Y-%m-%d %H:%M:%S")

    return start_epoch, end_epoch, display_st, display_et, active


def _netcare_service() -> Any:
    """Return a NetcareService bound to the currently configured cache directory."""

    return build_service_from_settings(settings)


#: One tracker per cache directory. The daemon process owns the browser sessions,
#: so a web request asks for a fan-out rather than starting its own pool.
_TRACKERS: dict[str, NetcareQueryTracker] = {}


def _netcare_tracker(service: Any) -> NetcareQueryTracker:
    """Return the query tracker bound to a service's cache directory."""

    key = str(service.cache.cache_dir)
    tracker = _TRACKERS.get(key)
    if tracker is None:
        tracker = NetcareQueryTracker(
            lambda **kwargs: service.query(**kwargs),
            total_targets=len(service.targets),
            workers=settings.netcare_workers,
        )
        _TRACKERS[key] = tracker
    return tracker

async def _request_json(request: Request) -> dict[str, Any]:
    """Parse a JSON request body, tolerating an empty one.

    Raises:
        ValueError: when the body is present but is not a JSON object.
    """

    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


def _optional_str(value: Any) -> str | None:
    """Return a non-empty string for a request field, or None."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _netcare_image_urls(service: Any, day: str | None) -> dict[str, str]:
    """Return ``{target: image_url}`` for a partition, used after a query ends."""

    return {
        target.target: _netcare_image_url(target.target, day)
        for target in service.targets
    }


def _netcare_image_url(target_id: str, day: str | None) -> str:
    """Return the image URL for a target inside a cache partition."""

    base = f"/api/netcare/graph/{target_id}.png"
    return f"{base}?day={day}" if day else base


def resolve_netcare_window(
    preset: str = "today",
    start_str: str | None = None,
    end_str: str | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a Netcare preset or custom range into a WIB wall-clock window.

    Reuses the dashboard's own preset semantics so a Netcare range and the
    MikroTik range always mean the same thing to the operator (FR-15.2).
    """

    start_epoch, end_epoch, _, _, _ = resolve_time_range(
        preset=preset or "today",
        start_str=start_str,
        end_str=end_str,
    )
    return (
        datetime.fromtimestamp(start_epoch, tz=UTC) + WIB_OFFSET,
        datetime.fromtimestamp(end_epoch, tz=UTC) + WIB_OFFSET,
    )


def _netcare_context(day: str | None = None) -> dict[str, Any]:
    """Build the dashboard context for the Netcare branch-link section."""

    service = _netcare_service()
    cache = service.cache
    if day is None:
        # The default (live) view reads the flat root cache, so the newest date
        # partition is promoted into it here or the operator would keep seeing
        # the stale seed captures while yesterday's fresh graphs sit on disk.
        cache.promote_newest_partition()
    try:
        manifest = cache.read_manifest(day)
    except ValueError:
        day, manifest = None, cache.read_manifest()
    targets = service.targets or list(DEFAULT_TARGETS)

    cards: list[dict[str, Any]] = []
    for target in targets:
        entry = manifest.get(target.target, {})
        payload = target.as_dict()
        payload.update(
            {
                "status": entry.get("status", "pending"),
                "last_scraped_at": entry.get("last_scraped_at"),
                "last_error": entry.get("last_error"),
                "file_size": entry.get("file_size", 0),
                "has_image": cache.image_path(target.target, day).is_file(),
                "image_url": _netcare_image_url(target.target, day),
            }
        )
        cards.append(payload)

    counts = region_counts(targets)
    grouped = [
        {
            "code": code,
            "label": region_label(code),
            "address": REGION_ADDRESSES.get(code, ""),
            "count": counts.get(code, 0),
        }
        for code in REGION_ORDER
    ]

    service_totals = service_counts(targets)
    service_grouped = [
        {
            "code": code,
            "label": service_label(code),
            "description": service_description(code),
            "badge_class": service_badge_class(code),
            "count": count,
        }
        for code, count in service_totals.items()
    ]

    # Calculate status summary counts
    fresh = sum(1 for c in cards if c.get('status') == 'ok')
    stale = sum(1 for c in cards if c.get('status') in ('stale', 'pending'))
    down = sum(1 for c in cards if c.get('status') in ('error', 'down'))
    netcare_status_summary = {'fresh': fresh, 'stale': stale, 'down': down}

    return {
        # Public API shape consumed by the dashboard JavaScript.
        "targets": cards,
        "regions": grouped,
        "services": service_grouped,
        "total": len(cards),
        "day": day,
        "partitions": cache.list_partitions(),
        "workers": settings.netcare_workers,
        # Template variable names.
        "netcare_enabled": settings.netcare_enabled,
        "netcare_cards": cards,
        "netcare_regions": grouped,
        "netcare_services": service_grouped,
        "netcare_total": len(cards),
        "netcare_refresh_seconds": settings.dashboard_refresh_seconds,
        "netcare_poll_interval_seconds": settings.netcare_poll_interval_seconds,
        "netcare_workers": settings.netcare_workers,
        "netcare_day": day or "",
        "netcare_partitions": cache.list_partitions(),
        "netcare_estimated_seconds": estimate_seconds(len(cards), settings.netcare_workers),
        "netcare_status_summary": netcare_status_summary,
    }


def _orbit_cache() -> OrbitCache:
    """Return OrbitCache instance configured from settings."""
    return OrbitCache(settings.orbit_cache_dir / "modems.json")


_orbit_service_instance: OrbitService | None = None
_orbit_service_lock = threading.Lock()


def _orbit_service() -> OrbitService:
    """Return singleton OrbitService instance."""
    global _orbit_service_instance
    with _orbit_service_lock:
        if _orbit_service_instance is None:
            _orbit_service_instance = OrbitService()
        return _orbit_service_instance


def _orbit_burn_tracker() -> QuotaBurnRateTracker:
    """Return QuotaBurnRateTracker instance configured from settings."""
    return QuotaBurnRateTracker(settings.orbit_cache_dir / "burn_rate_history.json")


def _orbit_context() -> dict[str, Any]:
    """Assemble context data for the Telkomsel Orbit modem dashboard."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    cache = _orbit_cache()
    modems = cache.get_or_seed(catalog)
    stats = filter_stats([m.target for m in modems])

    tracker = _orbit_burn_tracker()
    tracker.record_statuses(modems)
    burn_results = {r.imei: r for r in tracker.compute_all(modems)}

    total_remaining = round(sum(m.total_remaining_gb for m in modems), 2)
    total_quota = round(sum(m.total_quota_gb for m in modems), 2)
    expiring_soon = sum(
        1 for m in modems if m.earliest_days_left is not None and m.earliest_days_left <= 7
    )
    active_count = sum(1 for m in modems if m.target.status == "ACTIVE")
    imei_pending_count = sum(1 for m in modems if not m.target.imei_valid)

    return {
        "modems": modems,
        "burn_results": burn_results,
        "stats": stats,
        "summary": {
            "total_modems": len(modems),
            "active_modems": active_count,
            "total_remaining_gb": total_remaining,
            "total_quota_gb": total_quota,
            "expiring_soon_count": expiring_soon,
            "imei_pending_count": imei_pending_count,
        },
    }


def _map_orbit_statuses() -> list[OrbitModemStatus]:
    """Return Orbit modem statuses for the map, without seeding the cache.

    ``_orbit_context`` calls ``get_or_seed``, which *writes* demo data on first
    view. The map is part of the dashboard and should not create cache files as
    a side effect of rendering, so it reads the cache and falls back to bare
    catalog modems (coordinates included) when there is nothing cached yet.
    """

    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    cached = _orbit_cache().load()
    if cached is not None:
        return cached
    return [OrbitModemStatus(target=modem) for modem in catalog]


def _map_markers(
    netcare_cards: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the dashboard map markers from Netcare links and Orbit modems."""

    return build_map_markers(netcare_cards or [], _map_orbit_statuses())


def _map_context(netcare_cards: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Build the dashboard context for the interactive Leaflet map section."""

    markers = _map_markers(netcare_cards)
    return {
        "map_markers": markers,
        "map_marker_count": len(markers),
        "map_legend": MAP_LEGEND,
        "map_enabled": bool(markers),
    }
