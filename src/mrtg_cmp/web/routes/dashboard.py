"""Dashboard, telemetry/graph, map, and export routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mrtg_cmp.auth import get_db, require_authenticated_user
from mrtg_cmp.config import settings
from mrtg_cmp.db import Database
from mrtg_cmp.export import export_csv, export_excel
from mrtg_cmp.graph_renderer import format_engineering_bits, render_traffic_graph

from ..dependencies import (
    WIB_OFFSET,
    _map_context,
    _map_markers,
    _netcare_context,
    resolve_time_range,
    templates,
)

router = APIRouter()


# 2. Main Dashboard Endpoint
@router.get("/", response_class=HTMLResponse)
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


@router.get("/api/map/points")
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


# 3. Telemetry & Graph APIs
@router.get("/api/telemetry")
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


@router.get("/api/graph.png")
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
@router.get("/api/export/csv")
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


@router.get("/api/export/excel")
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



