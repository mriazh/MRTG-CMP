"""FastAPI web application and reporting API for MRTG Traffic Monitor."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..auth import (
    SESSION_COOKIE_NAME,
    create_user_session,
    ensure_admin_user,
    get_current_user_optional,
    get_db,
    require_authenticated_user,
    revoke_session,
    verify_password,
)
from ..config import settings
from ..console import console_manager, execute_routeros_command
from ..db import Database
from ..export import export_csv, export_excel
from ..graph_renderer import format_engineering_bits, render_traffic_graph
from ..logging_setup import audit_log, configure_logging
from ..netcare.query import NetcareQueryTracker, estimate_seconds, partition_for
from ..netcare.service import build_service_from_settings
from ..netcare.targets import (
    DEFAULT_TARGETS,
    REGION_ADDRESSES,
    REGION_ORDER,
    UNCLASSIFIED_SERVICE,
    NetcareTarget,
    NetcareTargetType,
    parse_service_type,
    region_counts,
    region_label,
    resolve_targets,
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
    VALID_STATUSES,
    OrbitModem,
    filter_stats,
    persist_orbit_catalog,
    remove_orbit_modem,
    resolve_orbit_catalog,
    upsert_orbit_modem,
)
from .map_data import MAP_LEGEND, build_map_markers

WIB_OFFSET = timedelta(hours=7)
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
NETCARE_IMAGE_MAX_AGE_SECONDS = 60


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
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan context: configures logging and seeds the admin user.

    Logging is configured here as well as in the CLI so that starting the app
    directly, e.g. ``uvicorn mrtg_cmp.web.app:app``, still writes the log file
    (FR-16.1). The call is idempotent, so the two entrypoints may both run it.
    """
    configure_logging(settings.log_file, settings.log_level)
    db = Database(settings.database_path)
    ensure_admin_user(db, settings)
    yield


app = FastAPI(
    title=settings.app_title,
    description="MikroTik WAN traffic monitoring, historical analysis, and reporting",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# 1. Authentication Endpoints
@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = "/",
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> Any:
    """Render login form or redirect to dashboard if already authenticated."""
    if current_user is not None:
        return RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next_url": next,
            "error": None,
            "current_user": None,
            "site_name": settings.site_name,
            "location_name": settings.location_name,
            "uplink_name": settings.uplink_name,
            "app_title": settings.app_title,
        },
    )


@app.post("/login")
def process_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember_me: bool = Form(False),
    next: str = Form("/"),
    db: Database = Depends(get_db),
) -> Any:
    """Validate credentials, issue session cookie, and redirect."""
    user = db.get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_url": next,
                "error": "Invalid username or password",
                "current_user": None,
                "site_name": settings.site_name,
                "location_name": settings.location_name,
                "uplink_name": settings.uplink_name,
                "app_title": settings.app_title,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token, ttl = create_user_session(
        database=db,
        user_id=user["id"],
        username=user["username"],
        remember_me=remember_me,
    )

    response = RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return response


@app.get("/logout")
@app.post("/logout")
def process_logout(
    request: Request,
    db: Database = Depends(get_db),
) -> Response:
    """Revoke current session and redirect to login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(db, token)

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


# 2. Main Dashboard Endpoint
@app.get("/", response_class=HTMLResponse)
def dashboard_view(
    request: Request,
    preset: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    fullday: str | None = Query(None),
    netcare_day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> Any:
    """Render authenticated monitoring dashboard with telemetry and traffic graph."""
    start_epoch, end_epoch, display_st, display_et, active_preset = resolve_time_range(
        preset=preset,
        start_str=start,
        end_str=end,
        fullday=fullday,
    )

    # Telemetry card data
    latest = db.get_latest_sample()
    if latest:
        latest_status = latest["status"]
        latest_uptime = latest.get("uptime") or "unknown"
        ts_utc = datetime.fromtimestamp(latest["epoch"], tz=UTC)
        ts_wib = ts_utc + WIB_OFFSET
        latest_ts_wib = ts_wib.strftime("%Y-%m-%d %H:%M:%S WIB")
        cur_in = format_engineering_bits(latest["rx_bps"])
        cur_out = format_engineering_bits(latest["tx_bps"])
    else:
        latest_status = "NO DATA"
        latest_uptime = "unknown"
        latest_ts_wib = "No data recorded yet"
        cur_in = "0 b"
        cur_out = "0 b"

    # Query strings for graph & downloads
    if active_preset == "custom":
        range_params = f"start={start}&end={end}"
    elif active_preset == "fullday":
        range_params = f"fullday={fullday or display_st[:10]}"
    else:
        range_params = f"preset={active_preset}"

    graph_img_url = f"/api/graph.png?{range_params}"
    export_png_url = f"/api/graph.png?{range_params}&download=1"
    export_excel_url = f"/api/export/excel?{range_params}"
    export_csv_url = f"/api/export/csv?{range_params}"

    # Default custom input values (YYYY-MM-DD HH:MM)
    c_start = display_st[:16]
    c_end = display_et[:16]

    netcare_context = _netcare_context(netcare_day)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "site_name": settings.site_name,
            "location_name": settings.location_name,
            "uplink_name": settings.uplink_name,
            "app_title": settings.app_title,
            "current_user": current_user,
            "active_preset": active_preset,
            "fullday_val": (fullday or display_st[:10]) if active_preset == "fullday" else "",
            "custom_start": c_start,
            "custom_end": c_end,
            "latest_status": latest_status,
            "latest_uptime": latest_uptime,
            "latest_timestamp_wib": latest_ts_wib,
            "current_in_formatted": cur_in,
            "current_out_formatted": cur_out,
            "graph_title": (
                f"Traffic {settings.routeros_interface} ({settings.uplink_name}) - "
                f"{settings.site_name}"
            ),
            "graph_img_url": graph_img_url,
            "export_png_url": export_png_url,
            "export_excel_url": export_excel_url,
            "export_csv_url": export_csv_url,
            "display_start": display_st,
            "display_end": display_et,
            **netcare_context,
            **_map_context(netcare_context["netcare_cards"]),
        },
    )


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


def _map_markers(netcare_cards: Sequence[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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


@app.get("/api/map/points")
def api_map_points(
    netcare_day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Return every located Netcare link and Orbit modem for the Leaflet map.

    The dashboard seeds its map from the server-rendered payload, so this exists
    for the auto-refresh cycle: the browser re-pulls markers on the same cadence
    as the card grids and repaints in place instead of reloading the page.
    """

    markers = _map_markers(_netcare_context(netcare_day)["netcare_cards"])
    return JSONResponse({"markers": markers, "count": len(markers)})


# 2b. TelkomCare Netcare Branch Links & Telkomsel Orbit Page
@app.get("/orbit", response_class=HTMLResponse)
def orbit_view(
    request: Request,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Any:
    """Render the Telkomsel Orbit modem monitoring dashboard."""
    ctx = _orbit_context()
    return templates.TemplateResponse(
        request=request,
        name="orbit.html",
        context={
            "site_name": settings.site_name,
            "location_name": settings.location_name,
            "uplink_name": settings.uplink_name,
            "app_title": settings.app_title,
            "current_user": current_user,
            "netcare_poll_interval_seconds": settings.netcare_poll_interval_seconds,
            **ctx,
        },
    )


@app.get("/api/orbit/modems")
def api_orbit_modems(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Return every Orbit modem with quota status, multi-package breakdown, and expiry info."""
    ctx = _orbit_context()
    return {
        "modems": [m.as_dict() for m in ctx["modems"]],
        "stats": ctx["stats"],
        "summary": ctx["summary"],
    }


@app.get("/api/orbit/sync/status")
def api_orbit_sync_status(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, str | None]:
    return _orbit_service().sync_status


@app.post("/api/orbit/sync")
async def api_orbit_sync(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Trigger on-demand live sync for Orbit modems."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    cache = _orbit_cache()
    service = _orbit_service()
    started = service.sync_in_background(catalog, cache)
    return JSONResponse(
        {
            "status": "accepted" if started else "already_running",
            "message": "Live Orbit sync queued" if started else "Live Orbit sync already running",
            "count": len(catalog),
        },
            status_code=status.HTTP_202_ACCEPTED,
    )


class OrbitModemRequest(BaseModel):
    no: int
    imei: str = ""
    phone: str = ""
    location: str = ""
    ssid: str = ""
    status: str = "ACTIVE"
    latitude: float | None = None
    longitude: float | None = None


def _orbit_catalog_from_request(payload: OrbitModemRequest) -> OrbitModem:
    """Validate an OrbitModemRequest and build the target modem."""
    if payload.no < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Modem number must be a positive integer",
        )
    status_value = payload.status.strip().upper() or "ACTIVE"
    if status_value not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid status '{payload.status}'; "
                f"expected one of {', '.join(VALID_STATUSES)}"
            ),
        )
    location = payload.location.strip()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Location cannot be empty",
        )
    return OrbitModem(
        no=payload.no,
        imei=payload.imei.strip(),
        phone=payload.phone.strip(),
        location=location,
        ssid=payload.ssid.strip(),
        status=status_value,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )


def _persist_orbit_change(catalog: list[OrbitModem]) -> None:
    """Persist an edited catalog to disk and refresh the Orbit cache.

    The configured catalog file decides the writer: an ``.xlsx`` path is saved
    through openpyxl, a ``.csv`` path through the CSV writer, and with no
    configured file both known catalog locations are updated when they exist.
    """
    configured = settings.orbit_catalog_file
    if configured:
        target = Path(configured)
        if target.suffix.lower() == ".xlsx":
            persist_orbit_catalog(catalog, excel_path=target)
        else:
            persist_orbit_catalog(catalog, csv_path=target)
    else:
        persist_orbit_catalog(catalog)
    _orbit_cache().reconcile(catalog)


@app.post("/api/orbit/modem")
def api_create_orbit_modem(
    payload: OrbitModemRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Create a new Orbit modem target in the catalog."""
    modem = _orbit_catalog_from_request(payload)
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    if any(m.no == modem.no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Modem number '{modem.no}' already exists",
        )
    updated = upsert_orbit_modem(catalog, modem)
    _persist_orbit_change(updated)
    user = str(current_user.get("username", "system"))
    audit_log("ORBIT_CREATE", user, f"created modem {modem.no} ({modem.location})")
    return modem.as_dict()


@app.put("/api/orbit/modem/{no}")
def api_update_orbit_modem(
    no: int,
    payload: OrbitModemRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Update an existing Orbit modem target."""
    modem = _orbit_catalog_from_request(payload)
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    if not any(m.no == no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Modem '{no}' not found",
        )
    if modem.no != no and any(m.no == modem.no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Modem number '{modem.no}' already exists",
        )
    updated = upsert_orbit_modem(catalog, modem, original_no=no)
    _persist_orbit_change(updated)
    user = str(current_user.get("username", "system"))
    audit_log("ORBIT_UPDATE", user, f"updated modem {no} ({modem.location})")
    return modem.as_dict()


@app.delete("/api/orbit/modem/{no}")
def api_delete_orbit_modem(
    no: int,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Delete an Orbit modem target."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    if not any(m.no == no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Modem '{no}' not found",
        )
    updated = remove_orbit_modem(catalog, no)
    _persist_orbit_change(updated)
    user = str(current_user.get("username", "system"))
    audit_log("ORBIT_DELETE", user, f"deleted modem {no}")
    ctx = _orbit_context()
    return {"status": "deleted", "modem": no, "stats": ctx["stats"]}


class NetcareTargetRequest(BaseModel):
    target: str
    type: str
    name: str
    address: str = ""
    region: str
    service_type: str = UNCLASSIFIED_SERVICE
    ocr_enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None


@app.get("/api/netcare/targets")
def api_netcare_targets(
    day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Return every Netcare branch target with status, timestamp, and image URL."""
    return _netcare_context(day)


@app.post("/api/netcare/targets")
def api_create_netcare_target(
    payload: NetcareTargetRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Create a new Netcare branch target in the catalog."""
    service = _netcare_service()
    target_id = payload.target.strip()
    if not target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target ID cannot be empty",
        )
    if any(t.target == target_id for t in service.targets):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target ID '{target_id}' already exists",
        )
    parsed_type = NetcareTargetType.parse(payload.type)
    if parsed_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid target type '{payload.type}'",
        )
    service_type = parse_service_type(payload.service_type) or UNCLASSIFIED_SERVICE
    new_target = NetcareTarget(
        target=target_id,
        type=parsed_type,
        name=payload.name.strip() or target_id,
        address=payload.address.strip(),
        region=payload.region.strip(),
        ocr_enabled=payload.ocr_enabled,
        service_type=service_type,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    service.upsert_target(new_target)
    user = str(current_user.get("username", "system"))
    audit_log("NETCARE_CREATE", user, f"created target {target_id}")
    return new_target.as_dict()


@app.put("/api/netcare/targets/{target_id}")
def api_update_netcare_target(
    target_id: str,
    payload: NetcareTargetRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Update an existing Netcare branch target."""
    service = _netcare_service()
    existing = next((t for t in service.targets if t.target == target_id), None)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target '{target_id}' not found",
        )
    new_target_id = payload.target.strip()
    if not new_target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target ID cannot be empty",
        )
    if new_target_id != target_id and any(t.target == new_target_id for t in service.targets):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target ID '{new_target_id}' already exists",
        )
    parsed_type = NetcareTargetType.parse(payload.type)
    if parsed_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid target type '{payload.type}'",
        )
    service_type = parse_service_type(payload.service_type) or UNCLASSIFIED_SERVICE
    updated_target = NetcareTarget(
        target=new_target_id,
        type=parsed_type,
        name=payload.name.strip() or new_target_id,
        address=payload.address.strip(),
        region=payload.region.strip(),
        ocr_enabled=payload.ocr_enabled,
        service_type=service_type,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    service.upsert_target(updated_target, original_id=target_id)
    user = str(current_user.get("username", "system"))
    audit_log("NETCARE_UPDATE", user, f"updated target {target_id}")
    return updated_target.as_dict()


@app.delete("/api/netcare/targets/{target_id}")
def api_delete_netcare_target(
    target_id: str,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, str]:
    """Delete a Netcare branch target."""
    service = _netcare_service()
    removed = service.remove_target(target_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target '{target_id}' not found",
        )
    user = str(current_user.get("username", "system"))
    audit_log("NETCARE_DELETE", user, f"deleted target {target_id}")
    return {"status": "deleted", "target": target_id}


@app.post("/api/netcare/query")
async def api_netcare_query(
    request: Request,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Start an on-demand Netcare capture for a time range and return its job id.

    The request never blocks: the fan-out runs on a background thread while the
    dashboard polls ``/api/netcare/status`` behind the loading dialog (FR-15.3).
    """

    try:
        body = await _request_json(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)

    window = resolve_netcare_window(
        preset=str(body.get("preset") or "today"),
        start_str=_optional_str(body.get("start")),
        end_str=_optional_str(body.get("end")),
    )
    requested = body.get("targets")
    targets: list[str] = [str(t) for t in requested] if isinstance(requested, list) else []

    service = _netcare_service()
    known = {t.target for t in service.targets}
    if targets:
        targets = [t for t in targets if t in known]
        if not targets:
            return JSONResponse(
                {"error": "No known Netcare targets in request"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    start_wib, end_wib = window
    # The window is WIB wall-clock, so the partition decision must be too.
    partition = partition_for(start_wib, now=_now_wib())
    total = len(targets) if targets else len(service.targets)

    job = _netcare_tracker(service).start(
        preset=str(body.get("preset") or "today"),
        start=start_wib,
        end=end_wib,
        day=partition,
        total=total,
        target_ids=targets or None,
    )
    snapshot = job.snapshot()
    snapshot["estimated_seconds"] = estimate_seconds(total, settings.netcare_workers)
    snapshot["image_url_day"] = partition
    return JSONResponse(snapshot, status_code=status.HTTP_202_ACCEPTED)


@app.get("/api/netcare/status")
def api_netcare_status(
    job_id: str = Query(...),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Return live progress for a Netcare query so the dialog can count down (FR-15.3)."""

    service = _netcare_service()
    job = _netcare_tracker(service).get(job_id)
    if job is None:
        return JSONResponse(
            {"error": "Unknown Netcare query job"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    snapshot = job.snapshot()
    snapshot["image_url_day"] = job.day
    if not job.running and not job.cancel_requested:
        snapshot["image_urls"] = _netcare_image_urls(service, job.day)
    return JSONResponse(snapshot)


@app.post("/api/netcare/cancel")
def api_netcare_cancel(
    job_id: str = Query(...),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Abandon a running Netcare query so the operator is not held hostage.

    Cancellation is cooperative: the scrape loop stops between targets, so a
    capture already in flight still finishes rather than leaving a half-written
    file. The dialog polls ``/api/netcare/status``, which settles the job and
    reports ``cancelled`` so the client can stop cleanly.
    """

    service = _netcare_service()
    job = _netcare_tracker(service).cancel(job_id)
    if job is None:
        return JSONResponse(
            {"error": "No running Netcare query job to cancel"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return JSONResponse(job.snapshot(), status_code=status.HTTP_202_ACCEPTED)


@app.get("/api/netcare/graph/{filename}")
def api_netcare_graph(
    filename: str,
    download: bool = Query(False),
    day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Serve a cached branch graph PNG from the Netcare cache directory."""
    if not filename.endswith(".png"):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    target_id = filename[:-4]
    known = {t.target for t in resolve_targets(settings.netcare_catalog_file)}
    if target_id not in known:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    cache = _netcare_service().cache
    try:
        image_path = cache.image_path(target_id, day)
        root = cache.day_dir(day)
    except ValueError:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if not image_path.is_relative_to(root) or not image_path.is_file():
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    try:
        payload = image_path.read_bytes()
    except OSError:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    headers = {"Cache-Control": f"public, max-age={NETCARE_IMAGE_MAX_AGE_SECONDS}"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{target_id}.png"'
    return Response(content=payload, media_type="image/png", headers=headers)


@app.post("/api/netcare/refresh/{target_id}")
def api_netcare_refresh(
    target_id: str,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Queue a specific target for the next scrape round."""
    service = _netcare_service()
    if not service.refresh_targets([target_id]):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return JSONResponse({"queued": True, "targets": sorted(service.pending_priority())})


# 3. Telemetry & Graph APIs
@app.get("/api/telemetry")
def api_telemetry(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Return JSON telemetry summary."""
    latest = db.get_latest_sample()
    raw_recent = db.get_recent_traffic_samples(limit=10)
    formatted_recent: list[dict[str, Any]] = []
    for sample in raw_recent:
        sample_utc = datetime.fromtimestamp(sample["epoch"], tz=UTC)
        sample_wib = sample_utc + WIB_OFFSET
        formatted_recent.append(
            {
                "timestamp_wib": sample_wib.strftime("%Y-%m-%d %H:%M:%S"),
                "status": sample["status"],
                "inbound_formatted": format_engineering_bits(sample["rx_bps"]),
                "outbound_formatted": format_engineering_bits(sample["tx_bps"]),
                "uptime": sample.get("uptime") or "unknown",
            }
        )

    if not latest:
        return {
            "status": "NO DATA",
            "uptime": "unknown",
            "latest_timestamp_wib": "No data recorded yet",
            "current_in_formatted": "0 b",
            "current_out_formatted": "0 b",
            "rx_bps": 0.0,
            "tx_bps": 0.0,
            "recent_samples": formatted_recent,
        }

    ts_utc = datetime.fromtimestamp(latest["epoch"], tz=UTC)
    ts_wib = ts_utc + WIB_OFFSET
    return {
        "status": latest["status"],
        "uptime": latest.get("uptime") or "unknown",
        "latest_timestamp_wib": ts_wib.strftime("%Y-%m-%d %H:%M:%S WIB"),
        "current_in_formatted": format_engineering_bits(latest["rx_bps"]),
        "current_out_formatted": format_engineering_bits(latest["tx_bps"]),
        "rx_bps": latest["rx_bps"],
        "tx_bps": latest["tx_bps"],
        "recent_samples": formatted_recent,
    }


@app.get("/api/graph.png")
def api_graph_png(
    preset: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    fullday: str | None = Query(None),
    width: int = Query(800),
    height: int = Query(360),
    download: bool = Query(False),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> Response:
    """Render traffic graph for the selected time range."""
    start_epoch, end_epoch, display_st, display_et, _ = resolve_time_range(
        preset=preset,
        start_str=start,
        end_str=end,
        fullday=fullday,
    )

    samples = db.get_traffic_samples(start=start_epoch, end=end_epoch)
    png_bytes = render_traffic_graph(
        samples=samples,
        title=(
            f"Traffic {settings.routeros_interface} ({settings.uplink_name}) - {settings.site_name}"
        ),
        start_time=display_st,
        end_time=display_et,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        width_px=max(400, min(width, 2400)),
        height_px=max(200, min(height, 1200)),
    )

    headers = {}
    if download:
        safe_st = display_st.replace(":", "").replace(" ", "_")
        safe_et = display_et.replace(":", "").replace(" ", "_")
        filename = f"mrtg_wan_{safe_st}_to_{safe_et}.png"
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    return Response(content=png_bytes, media_type="image/png", headers=headers)


# 4. Reporting & Data Exports
@app.get("/api/export/csv")
def api_export_csv(
    preset: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    fullday: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> Response:
    """Export tabular traffic records as CSV."""
    start_epoch, end_epoch, display_st, display_et, _ = resolve_time_range(
        preset=preset,
        start_str=start,
        end_str=end,
        fullday=fullday,
    )

    samples = db.get_traffic_samples(start=start_epoch, end=end_epoch)
    csv_bytes = export_csv(samples, interface_name=settings.routeros_interface)

    safe_st = display_st.replace(":", "").replace(" ", "_")
    safe_et = display_et.replace(":", "").replace(" ", "_")
    filename = f"traffic_wan_{safe_st}_to_{safe_et}.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/excel")
def api_export_excel(
    preset: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    fullday: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> Response:
    """Export formatted traffic report as Excel (.xlsx)."""
    start_epoch, end_epoch, display_st, display_et, _ = resolve_time_range(
        preset=preset,
        start_str=start,
        end_str=end,
        fullday=fullday,
    )

    samples = db.get_traffic_samples(start=start_epoch, end=end_epoch)
    excel_bytes = export_excel(
        samples=samples,
        interface_name=settings.routeros_interface,
        title=(
            f"Traffic Report - {settings.routeros_interface} ({settings.uplink_name}) - "
            f"{settings.site_name}"
        ),
        start_time=f"{display_st} WIB",
        end_time=f"{display_et} WIB",
    )

    safe_st = display_st.replace(":", "").replace(" ", "_")
    safe_et = display_et.replace(":", "").replace(" ", "_")
    filename = f"traffic_wan_{safe_st}_to_{safe_et}.xlsx"

    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# 5. RouterOS Remote Console Endpoints
class ConsoleAuthRequest(BaseModel):
    username: str
    password: str


class ConsoleExecuteRequest(BaseModel):
    token: str
    command: str


class ConsoleTerminateRequest(BaseModel):
    token: str


@app.get("/console", response_class=HTMLResponse)
def console_view(
    request: Request,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Any:
    """Render authenticated RouterOS Web Console page."""
    return templates.TemplateResponse(
        request=request,
        name="console.html",
        context={
            "site_name": settings.site_name,
            "location_name": settings.location_name,
            "uplink_name": settings.uplink_name,
            "app_title": settings.app_title,
            "current_user": current_user,
            "router_target": f"{settings.routeros_host}:{settings.routeros_port}",
        },
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Any:
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "site_name": settings.site_name,
            "app_title": settings.app_title,
            "current_user": current_user,
            "log_file": str(settings.log_file) if settings.log_file else "",
        },
    )


@app.get("/api/logs/tail")
def api_logs_tail(
    lines: int = Query(default=200, ge=10, le=1000),
    level: str = Query(default="ALL"),
    search: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Return the tail of the system log file with level and substring filters."""
    log_path = Path(settings.log_file) if settings.log_file else None
    if not log_path or not log_path.is_file():
        return {
            "lines": [],
            "total_count": 0,
            "log_file": str(settings.log_file) if settings.log_file is not None else "",
        }

    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        raw_lines = [line for line in content.splitlines() if line.strip()]
    except Exception:
        raw_lines = []

    # Extracts last `lines` records
    tail_lines = raw_lines[-lines:] if lines > 0 else raw_lines

    # Filters by `level` (matching `[INFO]`, `[WARNING]`, `[ERROR]` in line if level != "ALL")
    level_filter = level.strip().upper()
    if level_filter != "ALL":
        level_tag = f"[{level_filter}]"
        tail_lines = [line for line in tail_lines if level_tag in line]

    # Filters by `search` (case-insensitive substring)
    if search:
        search_term = search.strip().lower()
        if search_term:
            tail_lines = [line for line in tail_lines if search_term in line.lower()]

    return {
        "lines": tail_lines,
        "total_count": len(tail_lines),
        "log_file": str(settings.log_file) if settings.log_file is not None else "",
    }


@app.get("/api/logs/download")
def api_logs_download(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Download the system log file as a text/plain attachment."""
    log_path = Path(settings.log_file) if settings.log_file else None
    if not log_path or not log_path.is_file():
        return Response(
            content="",
            media_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="mrtg-cmp.log"'},
        )

    filename = log_path.name or "mrtg-cmp.log"
    return FileResponse(
        path=str(log_path),
        media_type="text/plain",
        filename=filename,
    )


@app.post("/api/console/auth")
def api_console_auth(
    payload: ConsoleAuthRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Authenticate directly with MikroTik RouterOS API."""
    success, token_or_err, identity = console_manager.authenticate(
        host=settings.routeros_host,
        port=settings.routeros_port,
        username=payload.username,
        password=payload.password,
    )
    if not success:
        return {"success": False, "error": token_or_err}

    return {
        "success": True,
        "token": token_or_err,
        "identity": identity,
        "username": payload.username,
        "prompt": f"[{payload.username}@{identity}] > ",
    }


@app.post("/api/console/execute")
def api_console_execute(
    payload: ConsoleExecuteRequest,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Execute a RouterOS command within an authenticated console session."""
    session = console_manager.get_session(payload.token)
    if not session:
        return {
            "success": False,
            "error": "Console session expired or invalid. Please login again.",
            "session_expired": True,
        }

    cmd = payload.command.strip()
    success, output = execute_routeros_command(session, cmd)

    # Log to SQLite audit trail
    if cmd:
        db.insert_console_log(
            username=session.username,
            command=cmd,
            status="OK" if success else "ERROR",
            output_preview=output,
        )

    ts_wib = _now_wib().strftime("%H:%M:%S")
    return {
        "success": success,
        "output": output,
        "timestamp": ts_wib,
        "prompt": f"[{session.username}@{session.identity}] > ",
    }


@app.post("/api/console/terminate")
def api_console_terminate(
    payload: ConsoleTerminateRequest,
) -> dict[str, Any]:
    """Immediately invalidate an active console session token."""
    console_manager.terminate(payload.token)
    return {"success": True}


@app.get("/api/console/logs")
def api_console_logs(
    limit: int = Query(50),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Return recent console audit logs."""
    logs = db.get_recent_console_logs(limit=limit)
    return {"logs": logs}


# 6. Tunnel Watchdog Diagnostics & Auto-Healing
@app.get("/api/tunnel/diagnose")
def api_tunnel_diagnose(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Perform live triangulation diagnostic on the RouterOS tunnel connection."""
    from ..tunnel_watchdog import tunnel_watchdog

    diag = tunnel_watchdog.diagnose()
    portal_info = None
    if settings.tunnel_web_email and settings.tunnel_web_password:
        portal_info = tunnel_watchdog.inspect_member_portal()
        # Remove internal session state before JSON serialization
        if portal_info:
            portal_info.pop("client", None)
            portal_info.pop("cookies", None)
            portal_info.pop("raw_body", None)

    return {
        "diagnosis": diag,
        "portal": portal_info,
        "target": f"{settings.routeros_host}:{settings.routeros_port}",
    }


@app.post("/api/tunnel/restart")
def api_tunnel_restart(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Safely trigger VPN restart with cooldown guard."""
    from ..tunnel_watchdog import tunnel_watchdog

    return tunnel_watchdog.auto_heal_if_needed()
