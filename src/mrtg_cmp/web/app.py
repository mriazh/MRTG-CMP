"""FastAPI web application and reporting API for MRTG Traffic Monitor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
from ..logging_setup import configure_logging
from ..netcare.query import NetcareQueryTracker, estimate_seconds, partition_for
from ..netcare.service import build_service_from_settings
from ..netcare.targets import (
    DEFAULT_TARGETS,
    REGION_ADDRESSES,
    REGION_ORDER,
    region_counts,
    region_label,
    resolve_targets,
    service_badge_class,
    service_counts,
    service_description,
    service_label,
)
from ..orbit.cache import OrbitCache
from ..orbit.targets import filter_stats, resolve_orbit_catalog

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
            **_netcare_context(netcare_day),
        },
    )


def _orbit_cache() -> OrbitCache:
    """Return OrbitCache instance configured from settings."""
    return OrbitCache(settings.orbit_cache_dir / "modems.json")


def _orbit_context() -> dict[str, Any]:
    """Assemble context data for the Telkomsel Orbit modem dashboard."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    cache = _orbit_cache()
    modems = cache.get_or_seed(catalog)
    stats = filter_stats([m.target for m in modems])

    total_remaining = round(sum(m.total_remaining_gb for m in modems), 2)
    total_quota = round(sum(m.total_quota_gb for m in modems), 2)
    expiring_soon = sum(
        1 for m in modems if m.earliest_days_left is not None and m.earliest_days_left <= 7
    )
    active_count = sum(1 for m in modems if m.target.status == "ACTIVE")
    imei_pending_count = sum(1 for m in modems if not m.target.imei_valid)

    return {
        "modems": modems,
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


@app.post("/api/orbit/sync")
async def api_orbit_sync(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Trigger on-demand background sync for Orbit modems."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    cache = _orbit_cache()
    cache.get_or_seed(catalog)
    return JSONResponse(
        {
            "status": "accepted",
            "message": "Orbit modem sync triggered",
            "count": len(catalog),
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


@app.get("/api/netcare/targets")
def api_netcare_targets(
    day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Return every Netcare branch target with status, timestamp, and image URL."""
    return _netcare_context(day)


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
