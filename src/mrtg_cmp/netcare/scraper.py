"""Netcare graph capture, non-accumulative cache storage, and state manifest.

Each target keeps exactly one PNG on disk (``{target}.png``). A successful
capture is validated with Pillow and then atomically replaces the previous
file, so the cache never accumulates history and the total footprint stays
around 1.5 MB for 18 branches. A failed round never deletes the existing
image: the manifest records the target as ``stale`` so the dashboard keeps
showing the last good graph instead of a broken image icon.

Historical windows are captured into a date partition instead
(``{YYYY-MM-DD}/{target}.png`` plus ``status_{YYYY-MM-DD}.json``) so a day the
operator already fetched is served straight from disk without touching the
browser (FR-15.4).

Every read-modify-write of the manifest and every image swap runs under a
re-entrant lock, because the scraper pool writes from several worker threads
at once (FR-15.1).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .targets import DEFAULT_TARGETS, NetcareTarget, NetcareTargetType

logger = logging.getLogger("mrtg_cmp.netcare.scraper")

CACHE_MANIFEST_NAME = "status.json"
TEMP_PREFIX = ".tmp-"

#: Directory holding the live (today) capture; kept flat for backward
#: compatibility with caches written before the worker pool landed.
LIVE_DAY = "live"

#: Manifest filename template for a date partition.
DAY_MANIFEST_TEMPLATE = "status_{day}.json"

#: Sub-directory template for a date partition.
DAY_DIR_TEMPLATE = "{day}"

STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_ERROR = "error"
STATUS_NO_GRAPH = "no_graph"

SID_GRAPH_PATH = "/mrtgnetcare2/graph/monitoring"
GRAPH_TITLE_GRAPH_PATH = "/mrtgnetcare2/graph"

#: Minimum payload size of a plausible graph capture. Kept low enough that a
#: highly compressible solid-colour capture is still classified as ``blank``
#: rather than ``invalid_image``.
MIN_FILE_BYTES = 200
#: Minimum pixel dimensions of a plausible graph capture.
MIN_WIDTH_PX = 100
MIN_HEIGHT_PX = 100
#: Coloured area a capture must show before it stops looking like a placeholder.
#: A low-traffic branch draws a flat line only a few hundred pixels wide, which
#: lands near 0.0018 of the canvas, so the cutoff stays well under that: a real
#: graph is a graph however little traffic crossed it.
MIN_COLORFUL_RATIO = 0.0005

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_DAY_KEY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: Validation verdicts returned instead of a boolean so the manifest can record why.
VALID = ""


def _iso_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""

    return datetime.now(UTC).isoformat(timespec="seconds")


def _manifest_stamp() -> str:
    """Return the manifest envelope stamp at microsecond resolution.

    ``_iso_now`` is second-resolution because the dashboard parses
    ``last_scraped_at``; at that resolution two captures written inside the same
    second compare equal, which would let the partition promotion skip a round
    that is genuinely the newer one. The envelope stamp is never parsed by the
    browser, so it can afford the finer resolution.
    """

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _empty_entry() -> dict[str, Any]:
    """Return a fresh manifest entry for a target that has never been captured."""

    return {
        "status": STATUS_PENDING,
        "last_scraped_at": None,
        "last_attempt_at": None,
        "last_error": None,
        "file_size": 0,
    }


def _entry_order(entry: dict[str, Any]) -> tuple[str, str]:
    """Return the sort key that decides which manifest entry is newer.

    ``last_scraped_at`` leads because a success is the only thing worth
    promoting over an existing status, so a day that merely failed cannot
    downgrade a branch whose flat image is still readable.
    """

    return (
        str(entry.get("last_scraped_at") or ""),
        str(entry.get("last_attempt_at") or ""),
    )


def normalize_day_key(day: datetime | str) -> str:
    """Return the canonical ``YYYY-MM-DD`` partition key.

    Raises:
        ValueError: when a string is not a calendar date, which keeps a bad
            request from silently creating a junk partition directory.
    """

    if isinstance(day, datetime):
        return day_key(day)
    cleaned = str(day).strip()[:10]
    return datetime.strptime(cleaned, "%Y-%m-%d").strftime("%Y-%m-%d")


def safe_target_filename(target_id: str) -> str:
    """Return a filesystem-safe cache filename fragment for a target id."""

    cleaned = _UNSAFE_NAME.sub("", str(target_id))
    return cleaned or "unknown"


def build_graph_url(target: NetcareTarget, base_url: str) -> str:
    """Return the portal graph URL for the target's lookup mode."""

    root = base_url.rstrip("/")
    path = SID_GRAPH_PATH if target.type is NetcareTargetType.SID else GRAPH_TITLE_GRAPH_PATH
    return f"{root}{path}"


def format_date_filter(day: datetime) -> tuple[str, str]:
    """Return the portal date-range filter strings covering the whole of ``day``."""

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=55, second=0, microsecond=0)
    return format_date_range(start, end)


def format_date_range(start: datetime, end: datetime) -> tuple[str, str]:
    """Return the portal date-range filter strings for an arbitrary window.

    The portal caps the end of day at ``23:55`` because its 5-minute buckets
    end there, so a later end time is clamped rather than rejected.
    """

    end_minute = min(end, end.replace(hour=23, minute=55, second=0, microsecond=0))
    if end_minute < start:
        end_minute = start
    return (start.strftime("%d/%m/%Y %H:%M"), end_minute.strftime("%d/%m/%Y %H:%M"))


def day_key(day: datetime) -> str:
    """Return the ``YYYY-MM-DD`` partition key for a date."""

    return day.strftime("%Y-%m-%d")


def validate_image_bytes(payload: bytes) -> str:
    """Validate captured graph bytes.

    Returns an empty string when the image looks like a real graph, otherwise a
    short verdict: ``blank``, ``no_graph``, or ``invalid_image``.
    """

    if not payload or len(payload) < MIN_FILE_BYTES:
        return "invalid_image"
    try:
        import io

        with Image.open(io.BytesIO(payload)) as image:
            return _validate_image(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("Unreadable Netcare graph capture: %s", exc)
        return "invalid_image"


def _validate_image(image: Image.Image) -> str:
    width, height = image.size
    if width < MIN_WIDTH_PX or height < MIN_HEIGHT_PX:
        logger.warning("Netcare graph capture is too small: %sx%s", width, height)
        return "invalid_image"

    rgb = image.convert("RGB")
    channels: tuple[tuple[int, int], ...] = rgb.getextrema()  # type: ignore[assignment]
    if all(high - low < 10 for low, high in channels):
        logger.warning("Netcare graph capture is a solid colour (blank)")
        return "blank"

    gray = image.convert("L")
    histogram = gray.histogram()
    total = max(1, width * height)
    white_ratio = sum(histogram[231:]) / total
    dark_ratio = sum(histogram[:100]) / total
    colorful_ratio = _colorful_ratio(rgb, total)

    if colorful_ratio < MIN_COLORFUL_RATIO and dark_ratio < 0.12 and white_ratio > 0.70:
        logger.warning(
            "Netcare graph capture looks like a no-graph placeholder "
            "(white=%.3f dark=%.3f colorful=%.5f)",
            white_ratio,
            dark_ratio,
            colorful_ratio,
        )
        return "no_graph"
    return VALID


def _colorful_ratio(rgb: Image.Image, total: int) -> float:
    """Return the fraction of pixels carrying a clearly non-grey colour."""

    saturated = rgb.convert("HSV").getchannel("S")
    histogram = saturated.histogram()
    return sum(histogram[77:]) / total


def validate_image_file(path: Path) -> str:
    """Validate an on-disk capture. Returns the same verdict shape as bytes."""

    resolved = Path(path)
    try:
        if not resolved.is_file():
            return "invalid_image"
        if resolved.stat().st_size < MIN_FILE_BYTES:
            return "invalid_image"
        with Image.open(resolved) as image:
            return _validate_image(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("Unreadable Netcare graph file %s: %s", resolved, exc)
        return "invalid_image"


@dataclass(frozen=True, slots=True)
class ScrapeOutcome:
    """Result of capturing one target's graph."""

    target: str
    status: str
    saved: bool = False
    error: str | None = None

    @classmethod
    def from_ok(cls, target: str) -> ScrapeOutcome:
        """Build a successful outcome."""

        return cls(target=target, status=STATUS_OK, saved=True)

    @classmethod
    def from_no_graph(cls, target: str, error: str) -> ScrapeOutcome:
        """Build a portal-reported no-graph outcome."""

        return cls(target=target, status=STATUS_NO_GRAPH, saved=False, error=error)

    @classmethod
    def from_error(cls, target: str, error: str) -> ScrapeOutcome:
        """Build a capture-failure outcome."""

        return cls(target=target, status=STATUS_ERROR, saved=False, error=error)


class NetcareCache:
    """Single-latest-file cache plus status manifest for all branch targets.

    The live capture lives flat in ``cache_dir``; a historical window is written
    to the ``{YYYY-MM-DD}`` partition alongside its own manifest. A re-entrant
    lock serialises the manifest read-modify-write and the temp-file swaps so
    concurrent pool workers cannot lose each other's status updates.
    """

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir)
        self.manifest_path = self.cache_dir / CACHE_MANIFEST_NAME
        self._lock = threading.RLock()

    # -- paths -------------------------------------------------------------

    def day_dir(self, day: datetime | str | None) -> Path:
        """Return the partition directory for a date, or the live directory."""

        if day is None:
            return self.cache_dir
        return self.cache_dir / DAY_DIR_TEMPLATE.format(day=normalize_day_key(day))

    def day_manifest_path(self, day: datetime | str | None) -> Path:
        """Return the manifest path for a partition, or the live manifest."""

        if day is None:
            return self.manifest_path
        key = normalize_day_key(day)
        return self.cache_dir / DAY_MANIFEST_TEMPLATE.format(day=key)

    def image_path(self, target_id: str, day: datetime | str | None = None) -> Path:
        """Return the cached PNG path for a target inside a partition."""

        return self.day_dir(day) / f"{safe_target_filename(target_id)}.png"

    def has_image(self, target_id: str, day: datetime | str | None = None) -> bool:
        """Return True when a usable cached image already exists."""

        return self.image_path(target_id, day).is_file()

    def partition_ready(self, day: datetime | str, target_ids: Iterable[str]) -> bool:
        """Return True when every requested target already has a day capture."""

        with self._lock:
            if day is None:
                return all(self.has_image(t) for t in target_ids)
            return all(self.has_image(t, day) for t in target_ids)

    def list_partitions(self) -> list[str]:
        """Return the sorted ``YYYY-MM-DD`` keys that hold a day capture."""

        if not self.cache_dir.is_dir():
            return []
        keys = [
            entry.name
            for entry in self.cache_dir.iterdir()
            if entry.is_dir() and _DAY_KEY_RE.fullmatch(entry.name)
        ]
        return sorted(keys)

    def promote_newest_partition(self) -> str | None:
        """Copy the newest date partition into the flat live cache.

        A scrape round writes into the partition of the window it captured, but
        the dashboard's default view reads the flat root cache. Without this the
        default view would keep serving whatever was last written flat while the
        fresh graphs sit untouched in their partition directory.

        Returns the promoted day key, or None when the root cache already carries
        a manifest at least as new as the newest partition - which is also what
        keeps this cheap enough to call on every dashboard poll.
        """

        partitions = self.list_partitions()
        if not partitions:
            return None
        newest = partitions[-1]
        if self._manifest_written_at(None) >= self._manifest_written_at(newest):
            return None
        return self.promote_partition(newest)

    def promote_partition(self, day: datetime | str) -> str | None:
        """Copy one partition's captures and statuses into the flat live cache.

        Freshness is decided from the manifests rather than from file timestamps,
        because a filesystem's modification-time resolution is coarse enough to
        make two captures written in the same second look equally new. A target
        is promoted only when its partition entry is newer than its flat entry,
        so a live round that ran after the partition is never rolled back, and a
        day that merely failed cannot downgrade a readable flat image.

        Returns the promoted day key, or None when the partition does not exist.
        """

        key = normalize_day_key(day)
        source_dir = self.day_dir(key)
        if not source_dir.is_dir():
            return None

        with self._lock:
            live = self.read_manifest(None)
            changed = False
            for target_id, entry in self.read_manifest(key).items():
                if _entry_order(entry) <= _entry_order(live.get(target_id, {})):
                    continue
                live[target_id] = entry
                changed = True
                image = source_dir / f"{safe_target_filename(target_id)}.png"
                if image.is_file():
                    self._copy_file(image, self.cache_dir / image.name)
            if changed:
                self._write_manifest(live, None)
        return key

    def _manifest_written_at(self, day: datetime | str | None) -> str:
        """Return a manifest's ``updated_at`` stamp, or "" when it has none.

        The stamps are same-format ISO-8601 UTC strings, so comparing them as
        text is the same as comparing them as instants.
        """

        raw = self._load_manifest(day)
        return str(raw.get("updated_at") or "")

    def _copy_file(self, source: Path, destination: Path) -> bool:
        """Replace ``destination`` with ``source`` through a temp file."""

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = destination.parent / f"{TEMP_PREFIX}{destination.name}"
            shutil.copyfile(source, temp_path)
            temp_path.replace(destination)
        except OSError as exc:
            logger.error("Failed promoting Netcare image %s: %s", source, exc)
            return False
        return True

    # -- image storage -----------------------------------------------------

    def store(self, target_id: str, payload: bytes, day: datetime | str | None = None) -> bool:
        """Validate and atomically replace the cached image for a target.

        Returns True when the image was written. An invalid capture leaves any
        previously cached image untouched.
        """

        verdict = validate_image_bytes(payload)
        if verdict != VALID:
            logger.warning("Rejecting Netcare capture for %s: %s", target_id, verdict)
            return False

        with self._lock:
            final_path = self.image_path(target_id, day)
            if _UNSAFE_NAME.sub("", str(target_id)) != str(target_id):
                logger.error("Refusing to cache unsafe target id %r", target_id)
                return False

            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = final_path.parent / f"{TEMP_PREFIX}{final_path.name}"
                temp_path.write_bytes(payload)
                temp_path.replace(final_path)
            except OSError as exc:
                logger.error("Failed storing Netcare image for %s: %s", target_id, exc)
                return False
        return True

    # -- manifest ----------------------------------------------------------

    def read_manifest(self, day: datetime | str | None = None) -> dict[str, dict[str, Any]]:
        """Return the per-target status manifest, defaulting unknown targets."""

        with self._lock:
            raw = self._load_manifest(day)
            targets = raw.get("targets")
            if not isinstance(targets, dict):
                targets = {}

            normalized: dict[str, dict[str, Any]] = {}
            for key, value in targets.items():
                if isinstance(value, dict):
                    normalized[str(key)] = dict(value)

            for target in [t.target for t in DEFAULT_TARGETS]:
                normalized.setdefault(target, _empty_entry())
            return normalized

    def _load_manifest(self, day: datetime | str | None) -> dict[str, Any]:
        path = self.day_manifest_path(day)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Discarding unreadable Netcare manifest: %s", exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_manifest(
        self,
        manifest: dict[str, dict[str, Any]],
        day: datetime | str | None,
    ) -> None:
        path = self.day_manifest_path(day)
        payload = {
            "version": 1,
            "updated_at": _manifest_stamp(),
            "day": normalize_day_key(day) if day is not None else LIVE_DAY,
            "targets": manifest,
        }
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = path.parent / f"{TEMP_PREFIX}{path.name}"
                temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                temp_path.replace(path)
            except (OSError, TypeError, ValueError) as exc:
                logger.error("Failed writing Netcare manifest: %s", exc)

    def _update(
        self,
        target_id: str,
        day: datetime | str | None = None,
        **changes: Any,
    ) -> dict[str, dict[str, Any]]:
        with self._lock:
            manifest = self.read_manifest(day)
            entry = manifest.setdefault(target_id, _empty_entry())
            entry.update(changes)
            entry["last_attempt_at"] = _iso_now()
            self._write_manifest(manifest, day)
            return manifest

    def mark_ok(self, target_id: str, day: datetime | str | None = None) -> None:
        """Record a successful capture with the cached file size."""

        with self._lock:
            size = 0
            path = self.image_path(target_id, day)
            if path.is_file():
                size = path.stat().st_size
            now = _iso_now()
            self._update(
                target_id,
                day,
                status=STATUS_OK,
                last_error=None,
                last_scraped_at=now,
                file_size=size,
            )

    def mark_no_graph(
        self,
        target_id: str,
        error: str = "No graph",
        day: datetime | str | None = None,
    ) -> None:
        """Record a portal no-graph result, degrading to stale when cached."""

        with self._lock:
            status = STATUS_STALE if self.has_image(target_id, day) else STATUS_NO_GRAPH
            self._update(target_id, day, status=status, last_error=error)

    def mark_error(self, target_id: str, error: str, day: datetime | str | None = None) -> None:
        """Record a capture failure, degrading to stale when an image is cached."""

        with self._lock:
            status = STATUS_STALE if self.has_image(target_id, day) else STATUS_ERROR
            self._update(target_id, day, status=status, last_error=error)


__all__ = [
    "CACHE_MANIFEST_NAME",
    "DAY_DIR_TEMPLATE",
    "DAY_MANIFEST_TEMPLATE",
    "GRAPH_TITLE_GRAPH_PATH",
    "LIVE_DAY",
    "SID_GRAPH_PATH",
    "STATUS_ERROR",
    "STATUS_NO_GRAPH",
    "STATUS_OK",
    "STATUS_PENDING",
    "STATUS_STALE",
    "VALID",
    "NetcareCache",
    "ScrapeOutcome",
    "build_graph_url",
    "day_key",
    "format_date_filter",
    "format_date_range",
    "normalize_day_key",
    "safe_target_filename",
    "validate_image_bytes",
    "validate_image_file",
]
