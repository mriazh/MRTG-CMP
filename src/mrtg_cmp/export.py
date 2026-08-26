"""Data export modules for MRTG (CSV and Excel formats)."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import settings
from .db import TrafficSample
from .graph_renderer import calculate_statistics

WIB_OFFSET = timedelta(hours=7)


def _format_wib(epoch: int | float | None, iso_utc: str) -> str:
    """Format an epoch or ISO UTC string to WIB (UTC+7) string representation."""
    if epoch is not None:
        dt = datetime.fromtimestamp(epoch, tz=UTC) + WIB_OFFSET
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")) + WIB_OFFSET
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_utc)


def export_csv(
    samples: Sequence[dict[str, Any] | TrafficSample],
    interface_name: str = "WAN",
) -> bytes:
    """Export traffic samples to standard comma-separated values (CSV) bytes."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")

    writer.writerow(
        [
            "Timestamp (UTC)",
            "Timestamp (WIB)",
            "Interface",
            "Rx Bytes",
            "Tx Bytes",
            "Inbound (bps)",
            "Outbound (bps)",
            "Inbound (Mbps)",
            "Outbound (Mbps)",
            "Uptime",
            "Status",
        ]
    )

    for s in samples:
        iso_ts = s["timestamp"] if isinstance(s, dict) else s.timestamp
        epoch = s["epoch"] if isinstance(s, dict) else s.epoch
        rx_bytes = int(s["rx_bytes"] if isinstance(s, dict) else s.rx_bytes)
        tx_bytes = int(s["tx_bytes"] if isinstance(s, dict) else s.tx_bytes)
        rx_bps = float(s["rx_bps"] if isinstance(s, dict) else s.rx_bps)
        tx_bps = float(s["tx_bps"] if isinstance(s, dict) else s.tx_bps)
        uptime = str(s["uptime"] or "") if isinstance(s, dict) else str(s.uptime or "")
        status = s["status"] if isinstance(s, dict) else s.status

        ts_utc_str = iso_ts if isinstance(iso_ts, str) else iso_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_wib_str = _format_wib(epoch, ts_utc_str)

        rx_mbps = rx_bps / 1_000_000.0
        tx_mbps = tx_bps / 1_000_000.0

        writer.writerow(
            [
                ts_utc_str,
                ts_wib_str,
                interface_name,
                rx_bytes,
                tx_bytes,
                f"{rx_bps:.2f}",
                f"{tx_bps:.2f}",
                f"{rx_mbps:.3f}",
                f"{tx_mbps:.3f}",
                uptime,
                status,
            ]
        )

    return output.getvalue().encode("utf-8")


def export_excel(
    samples: Sequence[dict[str, Any] | TrafficSample],
    interface_name: str = "WAN",
    title: str | None = None,
    start_time: str | datetime | None = None,
    end_time: str | datetime | None = None,
) -> bytes:
    """Generate a styled Excel workbook (.xlsx) containing traffic records and summaries."""
    if title is None:
        title = f"Traffic Report - WAN ({settings.uplink_name}) - {settings.site_name}"
    wb = Workbook()
    ws = wb.active
    ws.title = "Traffic Report"
    ws.views.sheetView[0].showGridLines = True

    # Color definitions
    navy_header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    summary_fill = PatternFill(start_color="E9EDF4", end_color="E9EDF4", fill_type="solid")
    white_bold = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
    navy_title_font = Font(name="Segoe UI", size=14, bold=True, color="1F497D")
    meta_label_font = Font(name="Segoe UI", size=9, bold=True, color="555555")
    meta_value_font = Font(name="Segoe UI", size=9, color="222222")
    data_font = Font(name="Segoe UI", size=9)

    thin_border_side = Side(style="thin", color="D0D5DD")
    cell_border = Border(
        left=thin_border_side,
        right=thin_border_side,
        top=thin_border_side,
        bottom=thin_border_side,
    )

    # 1. Metadata Header Banner
    ws["A1"] = title
    ws["A1"].font = navy_title_font

    ws["A3"] = "Interface:"
    ws["A3"].font = meta_label_font
    ws["B3"] = interface_name
    ws["B3"].font = meta_value_font

    now_wib = (datetime.now(UTC) + WIB_OFFSET).strftime("%Y-%m-%d %H:%M:%S WIB")
    ws["A4"] = "Generated At:"
    ws["A4"].font = meta_label_font
    ws["B4"] = now_wib
    ws["B4"].font = meta_value_font

    st_display = str(start_time) if start_time else "Start of Log"
    et_display = str(end_time) if end_time else "Current"
    ws["D3"] = "Time Range:"
    ws["D3"].font = meta_label_font
    ws["E3"] = f"{st_display} to {et_display}"
    ws["E3"].font = meta_value_font

    ws["D4"] = "Total Samples:"
    ws["D4"].font = meta_label_font
    ws["E4"] = len(samples)
    ws["E4"].font = meta_value_font

    # 2. Statistics Summary Cards (Rows 6-7)
    stats = calculate_statistics(samples)
    ws["A6"] = "Summary Statistics"
    ws["A6"].font = Font(name="Segoe UI", size=10, bold=True, color="1F497D")

    metric_labels = [
        ("Current Inbound", stats["in"]["current"] / 1_000_000.0),
        ("Average Inbound", stats["in"]["average"] / 1_000_000.0),
        ("Maximum Inbound", stats["in"]["maximum"] / 1_000_000.0),
        ("Current Outbound", stats["out"]["current"] / 1_000_000.0),
        ("Average Outbound", stats["out"]["average"] / 1_000_000.0),
        ("Maximum Outbound", stats["out"]["maximum"] / 1_000_000.0),
    ]

    for col_idx, (label, val_mbps) in enumerate(metric_labels, start=1):
        c_hdr = ws.cell(row=7, column=col_idx, value=label)
        c_hdr.font = Font(name="Segoe UI", size=8, bold=True, color="444444")
        c_hdr.fill = summary_fill
        c_hdr.border = cell_border
        c_hdr.alignment = Alignment(horizontal="center")

        c_val = ws.cell(row=8, column=col_idx, value=f"{val_mbps:.2f} Mbps")
        c_val.font = Font(name="Segoe UI", size=10, bold=True, color="1F497D")
        c_val.fill = summary_fill
        c_val.border = cell_border
        c_val.alignment = Alignment(horizontal="center")

    # 3. Main Data Table (Starting Row 10)
    headers = [
        "Timestamp (UTC)",
        "Timestamp (WIB)",
        "Interface",
        "Rx Bytes",
        "Tx Bytes",
        "Inbound (bps)",
        "Outbound (bps)",
        "Inbound (Mbps)",
        "Outbound (Mbps)",
        "Uptime",
        "Status",
    ]

    table_start_row = 10
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=table_start_row, column=col_idx, value=header)
        cell.fill = navy_header_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = cell_border

    current_row = table_start_row + 1
    for s in samples:
        iso_ts = s["timestamp"] if isinstance(s, dict) else s.timestamp
        epoch = s["epoch"] if isinstance(s, dict) else s.epoch
        rx_bytes = int(s["rx_bytes"] if isinstance(s, dict) else s.rx_bytes)
        tx_bytes = int(s["tx_bytes"] if isinstance(s, dict) else s.tx_bytes)
        rx_bps = float(s["rx_bps"] if isinstance(s, dict) else s.rx_bps)
        tx_bps = float(s["tx_bps"] if isinstance(s, dict) else s.tx_bps)
        uptime = str(s["uptime"] or "") if isinstance(s, dict) else str(s.uptime or "")
        status = s["status"] if isinstance(s, dict) else s.status

        ts_utc_str = iso_ts if isinstance(iso_ts, str) else iso_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_wib_str = _format_wib(epoch, ts_utc_str)

        row_vals = [
            ts_utc_str,
            ts_wib_str,
            interface_name,
            rx_bytes,
            tx_bytes,
            round(rx_bps, 2),
            round(tx_bps, 2),
            round(rx_bps / 1_000_000.0, 3),
            round(tx_bps / 1_000_000.0, 3),
            uptime,
            status,
        ]

        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.font = data_font
            cell.border = cell_border

            # Numeric formatting
            if col_idx in (4, 5):  # Bytes
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right")
            elif col_idx in (6, 7):  # bps
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
            elif col_idx in (8, 9):  # Mbps
                cell.number_format = "#,##0.000"
                cell.alignment = Alignment(horizontal="right")
            elif col_idx in (1, 2, 3, 10, 11):
                cell.alignment = Alignment(horizontal="center")

        current_row += 1

    # Freeze panes below table header
    ws.freeze_panes = f"A{table_start_row + 1}"

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()
