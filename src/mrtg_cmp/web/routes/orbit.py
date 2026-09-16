"""Telkomsel Orbit modem monitoring routes: dashboard view, sync, catalog CRUD."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from mrtg_cmp.auth import require_admin, require_authenticated_user
from mrtg_cmp.config import settings
from mrtg_cmp.logging_setup import audit_log
from mrtg_cmp.orbit.targets import (
    VALID_STATUSES,
    OrbitModem,
    persist_orbit_catalog,
    remove_orbit_modem,
    resolve_orbit_catalog,
    upsert_orbit_modem,
)

from ..dependencies import (
    _orbit_cache,
    _orbit_context,
    _orbit_service,
    templates,
)

router = APIRouter()


# 2b. TelkomCare Netcare Branch Links & Telkomsel Orbit Page
@router.get("/orbit", response_class=HTMLResponse)
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


@router.get("/api/orbit/modems")
def api_orbit_modems(
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Return every Orbit modem with quota status, multi-package breakdown, and expiry info."""
    ctx = _orbit_context()
    return {
        "modems": [m.as_dict() for m in ctx["modems"]],
        "stats": ctx["stats"],
        "summary": ctx["summary"],
    }


@router.get("/api/orbit/sync/status")
def api_orbit_sync_status(
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, str | None]:
    return dict(_orbit_service().sync_status)


@router.post("/api/orbit/sync")
async def api_orbit_sync(
    current_user: dict[str, Any] = Depends(require_admin),
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



@router.post("/api/orbit/modem")
def api_orbit_modem_create(
    payload: OrbitModemRequest,
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Register a new Orbit modem in the catalog."""
    modem = _orbit_catalog_from_request(payload)

    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    conflict = any(m.no == modem.no for m in catalog)
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An Orbit modem with No {modem.no} already exists",
        )

    catalog = upsert_orbit_modem(catalog, modem)
    _persist_orbit_change(catalog)
    audit_log("ORBIT_CREATE", str(current_user.get("username", "system")), f"registered {modem.no}")
    return modem.as_dict()


@router.put("/api/orbit/modem/{modem_no}")
def api_orbit_modem_update(
    modem_no: int,
    payload: OrbitModemRequest,
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Update an existing Orbit modem."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    if not any(m.no == modem_no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Orbit modem No {modem_no} not found",
        )

    modem = _orbit_catalog_from_request(payload)
    if modem_no != payload.no and any(m.no == payload.no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An Orbit modem with No {payload.no} already exists",
        )

    catalog = upsert_orbit_modem(catalog, modem, original_no=modem_no)
    _persist_orbit_change(catalog)
    audit_log("ORBIT_UPDATE", str(current_user.get("username", "system")), f"updated {modem_no}")
    return modem.as_dict()


@router.delete("/api/orbit/modem/{modem_no}")
def api_orbit_modem_delete(
    modem_no: int,
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Remove an Orbit modem from the catalog."""
    catalog = resolve_orbit_catalog(settings.orbit_catalog_file)
    if not any(m.no == modem_no for m in catalog):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Orbit modem No {modem_no} not found",
        )

    catalog = remove_orbit_modem(catalog, modem_no)
    _persist_orbit_change(catalog)
    user = str(current_user.get("username", "system"))
    audit_log("ORBIT_DELETE", user, f"deleted modem {modem_no}")
    ctx = _orbit_context()
    return {"status": "deleted", "modem": modem_no, "stats": ctx["stats"]}


