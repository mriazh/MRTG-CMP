"""TelkomCare Netcare branch target catalog.

The catalog holds the 18 active branch links (master list rows where
``ocr_enabled`` is true) enriched with branch name, target ID, physical
address, and region so the unified dashboard can group and label them.

Region codes are the codes used by the TelkomCare operations topology:

======  ==========  ===
Code    Label       Qty
======  ==========  ===
CGK     CGK Area    7
SUB     Surabaya    2
UPG     Makassar    4
DPS     Denpasar    2
BPN     Balikpapan  1
SENTUL  Sentul VPN  2
======  ==========  ===

Target ordering follows the master configuration order.

Which circuits are actually scraped is deployment data, not source: a deployment
keeps its own ``config/netcare_targets.csv`` (branch names, physical addresses,
region codes, and the ``service_type`` column from the master ``MASTER`` sheet)
which is loaded automatically whenever present and is excluded from version
control, because it carries that deployment's portal identifiers. The committed
``config/netcare_targets.csv.example`` documents the format with the same 18
anonymous circuits, and deployments that keep the master list elsewhere can point
``NETCARE_CATALOG_FILE`` at their own CSV export with the same columns. When
neither is available the built-in catalog below supplies the same anonymous
circuits with neutral regional names.
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("mrtg_cmp.netcare.targets")

CATALOG_HEADER = ("type", "target", "name", "address", "region", "ocr_enabled", "service_type")

#: Project-relative location of the deployment's own catalog, which is ignored by
#: git; :data:`DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH` is the tracked template for it.
DEFAULT_CATALOG_RELATIVE_PATH = Path("config") / "netcare_targets.csv"

#: Project-relative location of the committed, anonymous catalog template.
DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH = Path("config") / "netcare_targets.csv.example"

#: Regional group codes in dashboard display order.
REGION_ORDER: tuple[str, ...] = ("CGK", "SUB", "UPG", "DPS", "BPN", "SENTUL")

#: Human-readable label for each regional group code.
REGION_LABELS: dict[str, str] = {
    "CGK": "CGK Area",
    "SUB": "Surabaya",
    "UPG": "Makassar",
    "DPS": "Denpasar",
    "BPN": "Balikpapan",
    "SENTUL": "Sentul VPN",
}

#: Physical address placeholder per region, used when the master list is absent.
REGION_ADDRESSES: dict[str, str] = {
    "CGK": "Jakarta - CGK",
    "SUB": "Surabaya",
    "UPG": "Makassar",
    "DPS": "Denpasar",
    "BPN": "Balikpapan",
    "SENTUL": "Sentul - VPN Endpoint",
}

#: Required number of active links per region.
EXPECTED_REGION_COUNTS: dict[str, int] = {
    "CGK": 7,
    "SUB": 2,
    "UPG": 4,
    "DPS": 2,
    "BPN": 1,
    "SENTUL": 2,
}

#: Circuit service types in dashboard display order, transcribed from the
#: master ``MASTER`` sheet. The order is also the badge-colour order (cyan,
#: indigo, emerald) so the legend reads the way the pills are laid out.
SERVICE_ORDER: tuple[str, ...] = ("Astinet", "Metro-E", "VPN IP")

#: Bucket for a link whose service type is blank or not one of ``SERVICE_ORDER``.
#: It is grouped last rather than dropped, so an unrecognised value stays
#: visible instead of silently disappearing from the filter legend.
UNCLASSIFIED_SERVICE = "Unclassified"

#: What each service carries, shown next to the filter pill so an operator
#: reading a badge does not have to know the product name.
SERVICE_DESCRIPTIONS: dict[str, str] = {
    "Astinet": "Internet Dedicated",
    "Metro-E": "Ethernet L2",
    "VPN IP": "Intranet",
    UNCLASSIFIED_SERVICE: "Unclassified",
}

#: CSS modifier class per service, referenced by the dashboard badge and legend.
SERVICE_BADGE_CLASSES: dict[str, str] = {
    "Astinet": "svc-astinet",
    "Metro-E": "svc-metro-e",
    "VPN IP": "svc-vpn-ip",
    UNCLASSIFIED_SERVICE: "svc-unclassified",
}

#: Required number of active links per service, transcribed from the master sheet.
EXPECTED_SERVICE_COUNTS: dict[str, int] = {
    "Astinet": 6,
    "Metro-E": 5,
    "VPN IP": 7,
}

_TRUTHY = {"true", "1", "yes", "y", "on"}


class NetcareTargetType(StrEnum):
    """TelkomCare graph lookup mode for a branch link."""

    SID = "SID"
    GRAPH_TITLE = "Graph-title"

    @classmethod
    def parse(cls, raw: str) -> NetcareTargetType | None:
        """Return the matching target type, or None when the value is unknown."""

        normalized = raw.strip().lower().replace("_", "-").replace(" ", "-")
        if normalized in ("sid", "s-i-d"):
            return cls.SID
        if normalized in ("graph-title", "graphtitle", "graph"):
            return cls.GRAPH_TITLE
        return None


def parse_service_type(raw: str) -> str | None:
    """Return the canonical service type, or None when the value is unknown.

    Master exports spell the same service several ways ("Metro-E", "metro e",
    "METROE", "VPNIP"), so separators and case are normalised before matching
    rather than demanding one exact spelling from every export.
    """

    normalized = re.sub(r"[\s_-]+", "", str(raw).strip()).lower()
    if not normalized:
        return None
    for code in SERVICE_ORDER:
        if re.sub(r"[\s_-]+", "", code).lower() == normalized:
            return code
    return None


def service_label(service: str) -> str:
    """Return the display label for a service type."""

    return service if service in SERVICE_ORDER else UNCLASSIFIED_SERVICE


def service_description(service: str) -> str:
    """Return the legend blurb for a service type, or an empty string."""

    if service in SERVICE_ORDER:
        return SERVICE_DESCRIPTIONS.get(service, "")
    return ""


def service_badge_class(service: str) -> str:
    """Return the CSS modifier class that colours a service badge."""

    return SERVICE_BADGE_CLASSES.get(service, SERVICE_BADGE_CLASSES[UNCLASSIFIED_SERVICE])


@dataclass(frozen=True, slots=True)
class NetcareTarget:
    """A single TelkomCare branch graph target."""

    target: str
    type: NetcareTargetType
    name: str
    address: str
    region: str
    ocr_enabled: bool = True
    service_type: str = UNCLASSIFIED_SERVICE
    latitude: float | None = None
    longitude: float | None = None

    @property
    def cache_filename(self) -> str:
        """Filename of the single cached PNG for this target."""

        return f"{self.target}.png"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the target."""

        return {
            "target": self.target,
            "type": self.type.value,
            "name": self.name,
            "address": self.address,
            "region": self.region,
            "region_label": region_label(self.region),
            "ocr_enabled": self.ocr_enabled,
            "service_type": self.service_type,
            "service_label": service_label(self.service_type),
            "service_description": service_description(self.service_type),
            "service_badge_class": service_badge_class(self.service_type),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def _raw_entries() -> tuple[tuple[str, str, str], ...]:
    """Return (target, type, service_type) triples in configuration order.

    The ids are anonymous placeholders, not portal identifiers: the circuit a
    deployment actually scrapes is named by its own ``config/netcare_targets.csv``
    (or ``NETCARE_CATALOG_FILE``), which is kept out of version control. Only the
    per-link lookup mode and service product are transcribed here, because they
    drive the region split and the dashboard filter grouping.
    """

    return (
        ("target-001", "SID", "Astinet"),
        ("target-002", "SID", "Astinet"),
        ("target-003", "SID", "Metro-E"),
        ("target-004", "SID", "Metro-E"),
        ("target-005", "SID", "Metro-E"),
        ("target-006", "SID", "VPN IP"),
        ("target-007", "SID", "Astinet"),
        ("target-008", "SID", "Astinet"),
        ("target-009", "SID", "VPN IP"),
        ("target-010", "SID", "VPN IP"),
        ("target-011", "SID", "Astinet"),
        ("target-012", "Graph-title", "Astinet"),
        ("target-013", "Graph-title", "VPN IP"),
        ("target-014", "Graph-title", "Metro-E"),
        ("target-015", "Graph-title", "Metro-E"),
        ("target-016", "Graph-title", "VPN IP"),
        ("target-017", "Graph-title", "VPN IP"),
        ("target-018", "Graph-title", "VPN IP"),
    )


def _build_default_catalog() -> tuple[NetcareTarget, ...]:
    """Assemble the built-in catalog, distributing regions per specification."""

    remaining = dict(EXPECTED_REGION_COUNTS)
    per_region_index: dict[str, int] = {code: 0 for code in REGION_ORDER}
    built: list[NetcareTarget] = []

    for target_id, raw_type, service_type in _raw_entries():
        region = next((code for code in REGION_ORDER if remaining[code] > 0), "")
        if not region:
            raise ValueError(
                "Builtin catalog exhausted its region allocation before all targets were placed"
            )
        remaining[region] -= 1
        per_region_index[region] += 1
        built.append(
            NetcareTarget(
                target=target_id,
                type=NetcareTargetType.parse(raw_type) or NetcareTargetType.SID,
                name=f"{REGION_LABELS[region]} Link {per_region_index[region]:02d}",
                address=REGION_ADDRESSES[region],
                region=region,
                ocr_enabled=True,
                service_type=parse_service_type(service_type) or UNCLASSIFIED_SERVICE,
            )
        )

    leftovers = {code: count for code, count in remaining.items() if count}
    if leftovers:
        raise ValueError(f"Builtin catalog region allocation mismatch: {leftovers}")
    return tuple(built)


#: The 18 active branch links shipped with the application.
DEFAULT_TARGETS: tuple[NetcareTarget, ...] = _build_default_catalog()


def region_label(region: str) -> str:
    """Return the human-readable label for a region code."""

    return REGION_LABELS.get(region, region)


def active_targets(targets: Sequence[NetcareTarget] | None = None) -> list[NetcareTarget]:
    """Return only targets flagged ``ocr_enabled``."""

    source = DEFAULT_TARGETS if targets is None else targets
    return [t for t in source if t.ocr_enabled]


def region_counts(targets: Sequence[NetcareTarget] | None = None) -> dict[str, int]:
    """Return the number of active targets per region, ordered by region order."""

    active = active_targets(targets)
    return {code: sum(1 for t in active if t.region == code) for code in REGION_ORDER}


def _service_group_order(active: Sequence[NetcareTarget]) -> tuple[str, ...]:
    """Return the service keys to report, in legend order.

    ``SERVICE_ORDER`` always leads so the legend keeps all three products even
    when one currently has no links; the unclassified bucket is appended only
    when a link actually needs it.
    """

    if any(t.service_type not in SERVICE_ORDER for t in active):
        return (*SERVICE_ORDER, UNCLASSIFIED_SERVICE)
    return SERVICE_ORDER


def service_counts(targets: Sequence[NetcareTarget] | None = None) -> dict[str, int]:
    """Return the number of active targets per service type, in legend order."""

    active = active_targets(targets)
    groups = _service_group_order(active)
    return {code: sum(1 for t in active if t.service_type == code) for code in groups}


def targets_by_service(
    targets: Sequence[NetcareTarget] | None = None,
) -> list[tuple[str, list[NetcareTarget]]]:
    """Group active targets by service type, preserving dashboard service order."""

    active = active_targets(targets)
    return [
        (code, [t for t in active if t.service_type == code])
        for code in _service_group_order(active)
    ]


def targets_by_region(
    targets: Sequence[NetcareTarget] | None = None,
) -> list[tuple[str, list[NetcareTarget]]]:
    """Group active targets by region, preserving dashboard region order."""

    active = active_targets(targets)
    return [(code, [t for t in active if t.region == code]) for code in REGION_ORDER]


def find_target(
    target_id: str,
    targets: Sequence[NetcareTarget] | None = None,
) -> NetcareTarget | None:
    """Return the catalog entry matching ``target_id`` (or None)."""

    source = DEFAULT_TARGETS if targets is None else targets
    needle = target_id.strip()
    for target in source:
        if target.target == needle:
            return target
    return None


def _parse_coord(val: Any) -> float | None:
    """Parse coordinate string to float, returning None if empty or invalid."""
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str:
        return None
    try:
        return float(val_str)
    except ValueError:
        return None


def load_catalog(path: Path) -> list[NetcareTarget]:
    """Load a CSV target catalog overriding the built-in list.

    A ``service_type`` column is optional: an export written before the column
    existed, or a value that names no known product, lands in the unclassified
    bucket instead of rejecting the row or guessing a service.

    Raises:
        FileNotFoundError: if the catalog file does not exist.
    """

    resolved = Path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Netcare catalog file not found: {resolved}")

    loaded: list[NetcareTarget] = []
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            target_id = (row.get("target") or "").strip()
            if not target_id:
                logger.warning("Skipping catalog row without a target id: %r", row)
                continue
            parsed_type = NetcareTargetType.parse(row.get("type") or "")
            if parsed_type is None:
                logger.warning("Skipping catalog row with unknown type: %r", row)
                continue
            region = (row.get("region") or REGION_ORDER[0]).strip() or REGION_ORDER[0]
            service_type = parse_service_type(row.get("service_type") or "")
            if service_type is None:
                logger.debug("Catalog row %s has no known service type", target_id)
            latitude = _parse_coord(row.get("latitude"))
            longitude = _parse_coord(row.get("longitude"))
            loaded.append(
                NetcareTarget(
                    target=target_id,
                    type=parsed_type,
                    name=(row.get("name") or target_id).strip(),
                    address=(row.get("address") or REGION_ADDRESSES.get(region, "")).strip(),
                    region=region,
                    ocr_enabled=(row.get("ocr_enabled") or "").strip().lower() in _TRUTHY,
                    service_type=service_type or UNCLASSIFIED_SERVICE,
                    latitude=latitude,
                    longitude=longitude,
                )
            )
    return loaded


def load_catalog_csv(path: Path) -> list[NetcareTarget]:
    """Load a CSV target catalog overriding the built-in list."""
    return load_catalog(path)


def save_catalog_csv(targets: Sequence[NetcareTarget], path: Path) -> Path | None:
    """Save target catalog to CSV with automated timestamped backup.

    - Creates directory config/backups if not present.
    - Prior to writing, if path.is_file(), creates timestamped backup:
      config/backups/netcare_targets_{timestamp}.csv.bak (where timestamp is YYYYMMDD_HHMMSS).
    - Writes to temporary file and atomically replaces path.
    - Header columns: type,target,name,address,region,ocr_enabled,service_type,latitude,longitude.
    - Returns backup path if created.
    """

    target_path = Path(path)
    if target_path.parent.name == "config":
        backup_dir = target_path.parent / "backups"
    else:
        backup_dir = target_path.parent / "config" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if target_path.is_file():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"netcare_targets_{timestamp}.csv.bak"
        shutil.copy2(target_path, backup_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.{uuid.uuid4().hex}.tmp")
    header = (
        "type",
        "target",
        "name",
        "address",
        "region",
        "ocr_enabled",
        "service_type",
        "latitude",
        "longitude",
    )
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for t in targets:
            writer.writerow([
                t.type.value,
                t.target,
                t.name,
                t.address,
                t.region,
                "true" if t.ocr_enabled else "false",
                t.service_type,
                "" if t.latitude is None else str(t.latitude),
                "" if t.longitude is None else str(t.longitude),
            ])

    tmp_path.replace(target_path)
    return backup_path


def resolve_targets(catalog_file: Path | str | None = None) -> list[NetcareTarget]:
    """Return active targets from the override or default catalog, else the built-in list.

    The explicit ``catalog_file`` always wins. Otherwise the deployment's own
    catalog at ``config/netcare_targets.csv`` is used when it exists, so a
    deployment gets its own branch metadata without any extra configuration.
    That file is deliberately absent from version control; the built-in
    anonymous catalog is the last resort.
    """

    for candidate in _catalog_candidates(catalog_file):
        try:
            override = load_catalog(candidate)
        except (OSError, ValueError) as exc:
            logger.warning("Falling back to built-in Netcare catalog: %s", exc)
            continue
        resolved = active_targets(override)
        if not resolved:
            logger.warning("Override catalog %s yielded no active targets", candidate)
            continue
        return resolved
    return active_targets(DEFAULT_TARGETS)


def _catalog_candidates(catalog_file: Path | str | None) -> tuple[Path, ...]:
    """Return the catalog paths to try, in priority order."""

    if catalog_file:
        return (Path(catalog_file),)
    default_path = default_catalog_path()
    return (default_path,) if default_path is not None else ()


def default_catalog_path() -> Path | None:
    """Return the deployment's own catalog path, or None when it is absent.

    A fresh clone has no catalog (it is untracked), so the anonymous built-in
    list is used until the operator copies ``config/netcare_targets.csv.example``
    and fills in their circuits.

    The lookup walks up from the current working directory so the catalog is
    found whether the process runs from the repository root or a subdirectory,
    and falls back to the location relative to this module.
    """

    seen: set[Path] = set()
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        candidates.append(parent / DEFAULT_CATALOG_RELATIVE_PATH)
    candidates.append(Path(__file__).resolve().parents[3] / DEFAULT_CATALOG_RELATIVE_PATH)

    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "CATALOG_HEADER",
    "DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH",
    "DEFAULT_CATALOG_RELATIVE_PATH",
    "DEFAULT_TARGETS",
    "EXPECTED_REGION_COUNTS",
    "EXPECTED_SERVICE_COUNTS",
    "REGION_ADDRESSES",
    "REGION_LABELS",
    "REGION_ORDER",
    "SERVICE_BADGE_CLASSES",
    "SERVICE_DESCRIPTIONS",
    "SERVICE_ORDER",
    "UNCLASSIFIED_SERVICE",
    "NetcareTarget",
    "NetcareTargetType",
    "active_targets",
    "default_catalog_path",
    "find_target",
    "load_catalog",
    "load_catalog_csv",
    "parse_service_type",
    "region_counts",
    "region_label",
    "resolve_targets",
    "save_catalog_csv",
    "service_badge_class",
    "service_counts",
    "service_description",
    "service_label",
    "targets_by_region",
    "targets_by_service",
]
