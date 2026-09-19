"""System log viewer routes: tail and download."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from mrtg_cmp.auth import require_admin
from mrtg_cmp.config import settings

from ..dependencies import templates

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.get("/api/logs/tail")
def api_logs_tail(
    lines: int = Query(default=200, ge=10, le=1000),
    level: str = Query(default="ALL"),
    search: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(require_admin),
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


@router.get("/api/logs/download")
def api_logs_download(
    current_user: dict[str, Any] = Depends(require_admin),
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

