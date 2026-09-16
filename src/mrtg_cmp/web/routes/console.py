"""RouterOS Web Console routes: view, auth, execute, terminate, audit logs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from mrtg_cmp.auth import get_db, require_authenticated_user
from mrtg_cmp.config import settings
from mrtg_cmp.console import console_manager, execute_routeros_command
from mrtg_cmp.db import Database

from ..dependencies import _now_wib, templates

router = APIRouter()


class ConsoleAuthRequest(BaseModel):
    username: str
    password: str


class ConsoleExecuteRequest(BaseModel):
    token: str
    command: str


class ConsoleTerminateRequest(BaseModel):
    token: str


@router.get("/console", response_class=HTMLResponse)
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


@router.post("/api/console/auth")
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


@router.post("/api/console/execute")
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


@router.post("/api/console/terminate")
def api_console_terminate(
    payload: ConsoleTerminateRequest,
) -> dict[str, Any]:
    """Immediately invalidate an active console session token."""
    console_manager.terminate(payload.token)
    return {"success": True}


@router.get("/api/console/logs")
def api_console_logs(
    limit: int = Query(50),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Return recent console audit logs."""
    logs = db.get_recent_console_logs(limit=limit)
    return {"logs": logs}

