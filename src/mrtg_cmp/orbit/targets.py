"""Telkomsel Orbit modem target models and catalog loaders.

Parses Orbit modem targets from config/orbit_targets.csv, private Excel catalogs,
or the built-in anonymous fallback catalog.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("mrtg_cmp.orbit.targets")

CSV_HEADER = ("no", "imei", "phone", "location", "ssid", "status")

DEFAULT_CSV_PATH = Path("config") / "orbit_targets.csv"
DEFAULT_EXCEL_PATH = Path("config") / f"orbit_{''.join(('gm', 'f'))}.xlsx"
DEFAULT_EXAMPLE_CSV_PATH = Path("config") / "orbit_targets.csv.example"


@dataclass
class OrbitModem:
    """Represents a Telkomsel Orbit modem target."""

    no: int
    imei: str
    phone: str
    location: str
    ssid: str
    status: str = "ACTIVE"
    imei_valid: bool = False

    def __post_init__(self) -> None:
        clean_imei = str(self.imei).strip()
        self.imei = clean_imei
        self.phone = str(self.phone).strip()
        self.location = str(self.location).strip()
        self.ssid = str(self.ssid).strip()
        self.status = str(self.status).strip().upper()
        self.imei_valid = len(clean_imei) == 15 and clean_imei.isdigit()

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "no": self.no,
            "imei": self.imei,
            "phone": self.phone,
            "location": self.location,
            "ssid": self.ssid,
            "status": self.status,
            "imei_valid": self.imei_valid,
        }


def _classify_status(status_raw: str | None, location: str) -> str:
    """Classify modem status into ACTIVE, IDLE, or EX_CUSTOMER."""
    if status_raw:
        s = status_raw.strip().upper()
        if s in ("ACTIVE", "AKTIF"):
            return "ACTIVE"
        if s in ("IDLE", "CADANGAN"):
            return "IDLE"
        if s in ("EX_CUSTOMER", "BEKAS", "EX-CUSTOMER", "EX CUSTOMER"):
            return "EX_CUSTOMER"

    loc_upper = location.strip().upper()
    if loc_upper == "IDLE":
        return "IDLE"
    if loc_upper.startswith("EX MODEM"):
        return "EX_CUSTOMER"
    return "ACTIVE"


def load_orbit_catalog_csv(path: Path) -> list[OrbitModem]:
    """Load modem catalog from a CSV file."""
    modems: list[OrbitModem] = []
    with path.open(mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row or not row.get("no"):
                continue
            no = int(row["no"].strip())
            imei = row.get("imei", "").strip()
            phone = row.get("phone", "").strip()
            location = row.get("location", "").strip()
            ssid = row.get("ssid", "").strip()
            status = _classify_status(row.get("status"), location)
            modems.append(
                OrbitModem(
                    no=no,
                    imei=imei,
                    phone=phone,
                    location=location,
                    ssid=ssid,
                    status=status,
                )
            )
    return modems


def load_orbit_catalog_excel(path: Path) -> list[OrbitModem]:
    """Load modem catalog from Excel file (.xlsx) using openpyxl."""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        logger.warning("openpyxl is not installed; unable to load %s", path)
        return []

    wb = openpyxl.load_workbook(filename=str(path), data_only=True)
    sheet = wb.active
    if sheet is None:
        return []

    rows = list(sheet.iter_rows(values_only=True))
    if not rows or len(rows) < 2:
        return []

    # Map headers
    header_row = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    col_map: dict[str, int] = {}
    for idx, name in enumerate(header_row):
        col_map[name.lower()] = idx

    def _get_val(row_vals: tuple[Any, ...], *keys: str) -> str:
        for k in keys:
            if k.lower() in col_map:
                val = row_vals[col_map[k.lower()]]
                if val is not None:
                    return str(val).strip()
        return ""

    modems: list[OrbitModem] = []
    for r in rows[1:]:
        if not r or r[0] is None:
            continue
        try:
            no_raw = _get_val(r, "no.", "no")
            if not no_raw or not no_raw.isdigit():
                continue
            no = int(no_raw)
            imei = _get_val(r, "imei")
            phone = _get_val(r, "no.telepon", "no. telepon", "telepon", "phone")
            location = _get_val(r, "lokasi modem", "lokasi", "location")
            ssid = _get_val(r, "ssid modem", "ssid")
            status_val = _get_val(r, "status")
            status = _classify_status(status_val if status_val else None, location)

            modems.append(
                OrbitModem(
                    no=no,
                    imei=imei,
                    phone=phone,
                    location=location,
                    ssid=ssid,
                    status=status,
                )
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Error parsing Excel row %r: %s", r, exc)

    return modems


def _default_orbit_catalog() -> list[OrbitModem]:
    """Return 12 anonymous mock modems matching the operational profile.

    5 ACTIVE, 6 IDLE, 1 EX_CUSTOMER.
    Rows 1 and 3 are 10-digit IDs (imei_valid=False).
    Rows 2, 4-10, 11-12 have 15-digit valid IMEIs.
    """
    return [
        OrbitModem(1, "1700000001", "081200000001", "Meeting Room Alpha", "Orbit-Room-01"),
        OrbitModem(2, "860000000000002", "081200000002", "Meeting Room Bravo", "Orbit-Room-02"),
        OrbitModem(3, "1700000003", "081200000003", "Meeting Room Charlie", "Orbit-Room-03"),
        OrbitModem(4, "860000000000004", "081200000004", "Executive Lounge", "Orbit-Room-04"),
        OrbitModem(5, "860000000000005", "081200000005", "Operation Center", "Orbit-Room-05"),
        OrbitModem(6, "860000000000006", "081200000006", "IDLE", "Orbit-Backup-01", "IDLE"),
        OrbitModem(7, "860000000000007", "081200000007", "IDLE", "Orbit-Backup-02", "IDLE"),
        OrbitModem(8, "860000000000008", "081200000008", "IDLE", "Orbit-Backup-03", "IDLE"),
        OrbitModem(9, "860000000000009", "081200000009", "IDLE", "Orbit-Backup-04", "IDLE"),
        OrbitModem(
            10,
            "860000000000010",
            "081200000010",
            "EX Modem Customer Site",
            "Orbit-Customer-01",
            "EX_CUSTOMER",
        ),
        OrbitModem(11, "860000000000011", "081200000011", "IDLE", "Orbit-Backup-05", "IDLE"),
        OrbitModem(12, "860000000000012", "081200000012", "IDLE", "Orbit-Backup-06", "IDLE"),
    ]


def resolve_orbit_catalog(file_path: Path | str | None = None) -> list[OrbitModem]:
    """Resolve Orbit modem catalog from given path, CSV, Excel, or default fallback."""
    if file_path:
        p = Path(file_path)
        if p.is_file():
            if p.suffix.lower() == ".xlsx":
                return load_orbit_catalog_excel(p)
            return load_orbit_catalog_csv(p)
        logger.warning("Configured Orbit catalog file %s does not exist; using default fallback", p)
        return _default_orbit_catalog()

    if DEFAULT_CSV_PATH.is_file():
        try:
            return load_orbit_catalog_csv(DEFAULT_CSV_PATH)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed loading %s: %s", DEFAULT_CSV_PATH, exc)

    if DEFAULT_EXCEL_PATH.is_file():
        try:
            modems = load_orbit_catalog_excel(DEFAULT_EXCEL_PATH)
            if modems:
                return modems
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed loading %s: %s", DEFAULT_EXCEL_PATH, exc)

    if DEFAULT_EXAMPLE_CSV_PATH.is_file():
        try:
            return load_orbit_catalog_csv(DEFAULT_EXAMPLE_CSV_PATH)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed loading %s: %s", DEFAULT_EXAMPLE_CSV_PATH, exc)

    return _default_orbit_catalog()


def filter_stats(modems: Sequence[OrbitModem]) -> dict[str, int]:
    """Compute count statistics for UI filter pills and summary counters."""
    total = len(modems)
    active = sum(1 for m in modems if m.status == "ACTIVE")
    idle = sum(1 for m in modems if m.status == "IDLE")
    ex_customer = sum(1 for m in modems if m.status == "EX_CUSTOMER")
    imei_pending = sum(1 for m in modems if not m.imei_valid)
    return {
        "all": total,
        "active": active,
        "idle": idle,
        "ex_customer": ex_customer,
        "imei_pending": imei_pending,
    }
