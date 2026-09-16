"""FastAPI web application and reporting API for MRTG Traffic Monitor.

This module has been refactored: every route lives in
:mod:`mrtg_cmp.web.routes` and is mounted on ``app`` below. The tunnel
watchdog endpoints stay here because they are not part of any domain
route group.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from ..auth import ensure_admin_user, require_authenticated_user
from ..config import settings
from ..db import Database
from ..logging_setup import configure_logging
from .dependencies import (  # noqa: F401  re-exported for test backward-compat
    _TRACKERS,
    NETCARE_IMAGE_MAX_AGE_SECONDS,
    STATIC_DIR,
    TEMPLATES_DIR,
    _netcare_context,
    _netcare_image_urls,
    _netcare_service,
    _netcare_tracker,
    _now_wib,
    _optional_str,
    _orbit_cache,
    _orbit_context,
    _orbit_service,
    _request_json,
    resolve_time_range,
    templates,
)
from .routes import auth, console, dashboard, logs, netcare, orbit

__all__ = [
    "app",
    "lifespan",
]


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

# 1. Authentication Endpoints
app.include_router(auth.router)
# 2. Main Dashboard Endpoint & Map
app.include_router(dashboard.router)
# 2b. TelkomCare Netcare Branch Links & Telkomsel Orbit Page
app.include_router(netcare.router)
app.include_router(orbit.router)
# 4/5. System Logs & RouterOS Console
app.include_router(logs.router)
app.include_router(console.router)


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
