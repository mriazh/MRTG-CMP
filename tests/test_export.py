"""Unit tests for CSV and Excel export generators."""

from __future__ import annotations

import csv
import io

from openpyxl import load_workbook

from mrtg_cmp.export import export_csv, export_excel


def test_export_csv_validity() -> None:
    """Ensure CSV output adheres to comma-delimited RFC formatting with expected headers."""
    samples = [
        {
            "timestamp": "2026-09-14T10:00:00Z",
            "epoch": 1789376400,
            "rx_bytes": 100_000_000,
            "tx_bytes": 50_000_000,
            "rx_bps": 8_000_000.0,
            "tx_bps": 4_000_000.0,
            "uptime": "1d",
            "status": "UP",
        },
        {
            "timestamp": "2026-09-14T10:05:00Z",
            "epoch": 1789376700,
            "rx_bytes": 105_000_000,
            "tx_bytes": 52_000_000,
            "rx_bps": 10_000_000.0,
            "tx_bps": 5_000_000.0,
            "uptime": "1d 5m",
            "status": "UP",
        },
    ]

    csv_bytes = export_csv(samples, interface_name="WAN")
    csv_text = csv_bytes.decode("utf-8")

    reader = list(csv.reader(io.StringIO(csv_text)))
    assert len(reader) == 3  # 1 header + 2 data rows

    headers = reader[0]
    assert headers[0] == "Timestamp (UTC)"
    assert headers[1] == "Timestamp (WIB)"
    assert headers[2] == "Interface"
    assert headers[5] == "Inbound (bps)"
    assert headers[7] == "Inbound (Mbps)"

    row1 = reader[1]
    assert row1[2] == "WAN"
    assert row1[3] == "100000000"
    assert row1[7] == "8.000"  # 8 Mbps
    assert row1[8] == "4.000"  # 4 Mbps


def test_export_excel_validity() -> None:
    """Ensure Excel output builds an openpyxl workbook with banners, stats, and styled tables."""
    samples = [
        {
            "timestamp": "2026-09-14T00:00:00Z",
            "epoch": 1789340400,
            "rx_bytes": 10_000_000,
            "tx_bytes": 5_000_000,
            "rx_bps": 12_000_000.0,
            "tx_bps": 6_000_000.0,
            "uptime": "3d",
            "status": "UP",
        }
    ]

    xlsx_bytes = export_excel(
        samples,
        interface_name="WAN",
        title="Test Report - Enterprise Gateway",
        start_time="2026-09-14 00:00:00",
        end_time="2026-09-14 23:55:00",
    )

    wb = load_workbook(io.BytesIO(xlsx_bytes))
    assert "Traffic Report" in wb.sheetnames

    ws = wb["Traffic Report"]
    assert ws["A1"].value == "Test Report - Enterprise Gateway"
    assert ws["B3"].value == "WAN"

    # Row 10 headers
    assert ws["A10"].value == "Timestamp (UTC)"
    assert ws["B10"].value == "Timestamp (WIB)"
    assert ws["D10"].value == "Rx Bytes"
    assert ws["H10"].value == "Inbound (Mbps)"

    # Row 11 data
    assert ws["C11"].value == "WAN"
    assert ws["D11"].value == 10_000_000
    assert ws["H11"].value == 12.0


def test_export_neutralises_formula_injection() -> None:
    """Cells starting with =, +, -, or @ are prefixed with an apostrophe (CWE-1236)."""
    samples = [
        {
            "timestamp": "2026-09-14T10:00:00Z",
            "epoch": 1789376400,
            "rx_bytes": 100_000_000,
            "tx_bytes": 50_000_000,
            "rx_bps": 8_000_000.0,
            "tx_bps": 4_000_000.0,
            "uptime": '=cmd|\' /c calc\'!A0',
            "status": "@SUM(1+1)*cmd",
        },
        {
            "timestamp": "2026-09-14T10:05:00Z",
            "epoch": 1789376700,
            "rx_bytes": 105_000_000,
            "tx_bytes": 52_000_000,
            "rx_bps": 10_000_000.0,
            "tx_bps": 5_000_000.0,
            "uptime": "+1+1",
            "status": "UP",
        },
    ]

    csv_text = export_csv(samples, interface_name="-1+cmd").decode("utf-8")
    reader = list(csv.reader(io.StringIO(csv_text)))
    assert reader[1][2] == "'-1+cmd"
    assert reader[1][9] == "'=cmd|' /c calc'!A0"
    assert reader[1][10] == "'@SUM(1+1)*cmd"
    assert reader[2][9] == "'+1+1"
    # Benign values stay untouched.
    assert reader[2][10] == "UP"

    ws = load_workbook(io.BytesIO(export_excel(samples, interface_name="-1+cmd")))["Traffic Report"]
    assert ws["C11"].value == "'-1+cmd"
    assert ws["J11"].value == "'=cmd|' /c calc'!A0"
    assert ws["K11"].value == "'@SUM(1+1)*cmd"
    assert ws["J12"].value == "'+1+1"
    assert ws["K12"].value == "UP"
