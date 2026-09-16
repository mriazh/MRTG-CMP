"""TelkomCare Netcare branch-link routes: on-demand queries, graph cache, CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from mrtg_cmp.auth import require_admin, require_authenticated_user
from mrtg_cmp.config import settings
from mrtg_cmp.logging_setup import audit_log
from mrtg_cmp.netcare.query import estimate_seconds, partition_for
from mrtg_cmp.netcare.targets import (
    UNCLASSIFIED_SERVICE,
    NetcareTarget,
    NetcareTargetType,
    parse_service_type,
    resolve_targets,
)

from ..dependencies import (
    NETCARE_IMAGE_MAX_AGE_SECONDS,
    _netcare_context,
    _netcare_image_urls,
    _netcare_service,
    _netcare_tracker,
    _now_wib,
    _optional_str,
    _request_json,
    resolve_netcare_window,
)

router = APIRouter()


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


@router.get("/api/netcare/targets")
def api_netcare_targets(
    day: str | None = Query(None),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Return every Netcare branch target with status, timestamp, and image URL."""
    return _netcare_context(day)


@router.post("/api/netcare/targets")
def api_create_netcare_target(
    payload: NetcareTargetRequest,
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.put("/api/netcare/targets/{target_id}")
def api_update_netcare_target(
    target_id: str,
    payload: NetcareTargetRequest,
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.delete("/api/netcare/targets/{target_id}")
def api_delete_netcare_target(
    target_id: str,
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.post("/api/netcare/query")
async def api_netcare_query(
    request: Request,
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.get("/api/netcare/status")
def api_netcare_status(
    job_id: str = Query(...),
    current_user: dict[str, Any] = Depends(require_admin),
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



@router.post("/api/netcare/cancel")
def api_netcare_cancel(
    job_id: str = Query(...),
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.get("/api/netcare/graph/{filename}")
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


@router.post("/api/netcare/refresh/{target_id}")
def api_netcare_refresh(
    target_id: str,
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> Response:
    """Queue a specific target for the next scrape round."""
    service = _netcare_service()
    if not service.refresh_targets([target_id]):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return JSONResponse({"queued": True, "targets": sorted(service.pending_priority())})

