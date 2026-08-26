"""Unit tests for the Netcare graph scraper cache engine and state manifest."""

from __future__ import annotations

import io
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from mrtg_cmp.netcare.scraper import (
    CACHE_MANIFEST_NAME,
    DAY_MANIFEST_TEMPLATE,
    LIVE_DAY,
    MIN_COLORFUL_RATIO,
    STATUS_ERROR,
    STATUS_NO_GRAPH,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_STALE,
    NetcareCache,
    ScrapeOutcome,
    build_graph_url,
    day_key,
    format_date_filter,
    format_date_range,
    normalize_day_key,
    validate_image_bytes,
    validate_image_file,
)
from mrtg_cmp.netcare.targets import DEFAULT_TARGETS, NetcareTarget, NetcareTargetType

TARGET = DEFAULT_TARGETS[0]


# --- Image fixtures --------------------------------------------------------


def _graph_image_bytes(width: int = 600, height: int = 300) -> bytes:
    """Render a realistic MRTG-like graph: colored area plus dark legend text."""

    image = Image.new("RGB", (width, height), "white")
    pixels = image.load()
    assert pixels is not None
    for x in range(width):
        shade = 0 if (x // 17) % 2 else 60
        for y in range(height // 2, height - 20):
            pixels[x, y] = (0, 204, 0) if shade == 0 else (0, 140, 0)
    for x in range(0, width, 3):
        for y in range(10, 25):
            pixels[x, y] = (0, 0, 0)
    for y in range(height - 8, height):
        for x in range(width):
            pixels[x, y] = (170, 170, 170)
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _solid_image_bytes(
    color: tuple[int, int, int] = (255, 255, 255),
    size: tuple[int, int] = (600, 300),
) -> bytes:
    import io

    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Image validation ------------------------------------------------------


def test_validate_image_bytes_accepts_realistic_graph() -> None:
    assert validate_image_bytes(_graph_image_bytes()) == ""


def test_validate_image_bytes_rejects_blank_white_image() -> None:
    assert validate_image_bytes(_solid_image_bytes()) == "blank"


def test_validate_image_bytes_rejects_blank_black_image() -> None:
    assert validate_image_bytes(_solid_image_bytes((0, 0, 0))) == "blank"


def _placeholder_image_bytes() -> bytes:
    """Render TelkomCare's 'no graph' placeholder: white page, grey border, dark text."""

    image = Image.new("RGB", (600, 300), "white")
    pixels = image.load()
    assert pixels is not None
    for x in range(0, 600, 3):
        for y in range(140, 155):
            pixels[x, y] = (0, 0, 0)
    for y in range(0, 300, 2):
        pixels[0, y] = (200, 200, 200)
        pixels[599, y] = (200, 200, 200)
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_validate_image_bytes_rejects_no_graph_placeholder() -> None:
    assert validate_image_bytes(_placeholder_image_bytes()) == "no_graph"


def _flat_graph_image() -> Image.Image:
    """Render a low-traffic graph: one thin coloured line, almost no other ink.

    A branch that barely moved all day still produces a real RRDtool render, but
    its coloured area is a sliver of the canvas rather than half of it.
    """

    image = Image.new("RGB", (600, 300), "white")
    pixels = image.load()
    assert pixels is not None
    for x in range(100, 424):
        pixels[x, 210] = (0, 204, 0)
    for x in range(0, 600, 3):
        for y in range(10, 25):
            pixels[x, y] = (0, 0, 0)
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_validate_image_bytes_accepts_a_flat_low_traffic_graph() -> None:
    """A thin flat line is a valid graph, not a missing one.

    The fixture's coloured ratio is 324/180000 = 0.0018, which the old 0.002
    cutoff rejected: a quiet branch was reported as ``no_graph`` every round.
    """

    image = _flat_graph_image()
    hsv = image.convert("HSV").getchannel("S").histogram()
    ratio = sum(hsv[77:]) / (600 * 300)

    assert MIN_COLORFUL_RATIO <= ratio < 0.002, "fixture must sit between the cutoffs"
    assert validate_image_bytes(_png_bytes(image)) == ""



def test_validate_image_bytes_rejects_empty_payload() -> None:
    assert validate_image_bytes(b"") == "invalid_image"


def test_validate_image_bytes_rejects_non_image_payload() -> None:
    assert validate_image_bytes(b"not a png at all") == "invalid_image"


def test_validate_image_bytes_rejects_tiny_image() -> None:
    assert validate_image_bytes(_graph_image_bytes(width=40, height=30)) == "invalid_image"


def test_validate_image_file_accepts_realistic_graph(tmp_path: Path) -> None:
    path = tmp_path / "graph.png"
    path.write_bytes(_graph_image_bytes())
    assert validate_image_file(path) == ""


def test_validate_image_file_rejects_missing_file(tmp_path: Path) -> None:
    assert validate_image_file(tmp_path / "absent.png") == "invalid_image"


def test_validate_image_file_rejects_undersized_file(tmp_path: Path) -> None:
    path = tmp_path / "graph.png"
    path.write_bytes(_graph_image_bytes()[:500])
    assert validate_image_file(path) == "invalid_image"


# --- Date filter -----------------------------------------------------------


def test_format_date_filter_covers_current_day() -> None:
    day = datetime(2026, 9, 18)
    assert format_date_filter(day) == ("18/09/2026 00:00", "18/09/2026 23:55")


# --- URL construction ------------------------------------------------------


def test_build_graph_url_for_sid_mode() -> None:
    url = build_graph_url(TARGET, "https://telkomcare.telkom.co.id")
    assert url == "https://telkomcare.telkom.co.id/mrtgnetcare2/graph/monitoring"


def test_build_graph_url_for_graph_title_mode() -> None:
    target = NetcareTarget(
        target="3784",
        type=NetcareTargetType.GRAPH_TITLE,
        name="N",
        address="A",
        region="BPN",
    )
    url = build_graph_url(target, "https://telkomcare.telkom.co.id/")
    assert url == "https://telkomcare.telkom.co.id/mrtgnetcare2/graph"


# --- Cache storage ---------------------------------------------------------


def test_cache_write_is_atomic_and_overwrites(
    tmp_path: Path,
) -> None:
    cache = NetcareCache(tmp_path)
    first = _graph_image_bytes()
    second = _graph_image_bytes()
    cache.store(TARGET.target, first)
    path = cache.image_path(TARGET.target)
    assert path.read_bytes() == first
    cache.store(TARGET.target, second)
    assert path.read_bytes() == second
    assert cache.image_path(TARGET.target) == tmp_path / f"{TARGET.target}.png"


def test_cache_leaves_no_temp_files_after_store(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    assert [p.name for p in tmp_path.iterdir()] == [f"{TARGET.target}.png"]


def test_cache_does_not_write_invalid_image(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    assert cache.store(TARGET.target, _solid_image_bytes()) is False
    assert not cache.image_path(TARGET.target).exists()
    assert list(tmp_path.glob("*.png")) == []


def test_cache_retains_previous_image_on_invalid_capture(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    good = cache.image_path(TARGET.target).read_bytes()
    assert cache.store(TARGET.target, _solid_image_bytes()) is False
    assert cache.image_path(TARGET.target).read_bytes() == good


def test_cache_stays_non_accumulative_across_many_cycles(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    for _ in range(20):
        cache.store(TARGET.target, _graph_image_bytes())
        cache.mark_ok(TARGET.target)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == sorted([f"{TARGET.target}.png", CACHE_MANIFEST_NAME])


def test_cache_rejects_unsafe_target_ids(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    assert cache.store("../escape", _graph_image_bytes()) is False
    assert cache.image_path("../escape").parent == tmp_path
    assert list(tmp_path.iterdir()) == []


# --- Status manifest -------------------------------------------------------


def test_manifest_defaults_to_pending_for_every_target(tmp_path: Path) -> None:
    manifest = NetcareCache(tmp_path).read_manifest()
    assert set(manifest) == {t.target for t in DEFAULT_TARGETS}
    assert all(entry["status"] == STATUS_PENDING for entry in manifest.values())
    assert all(entry["last_scraped_at"] is None for entry in manifest.values())
    assert all(entry["last_error"] is None for entry in manifest.values())


def test_manifest_records_successful_scrape(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_ok(TARGET.target)
    entry = cache.read_manifest()[TARGET.target]
    assert entry["status"] == STATUS_OK
    assert entry["last_error"] is None
    assert entry["last_scraped_at"] is not None
    assert entry["file_size"] == cache.image_path(TARGET.target).stat().st_size


def test_manifest_records_error_status(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.mark_error(TARGET.target, "portal timeout")
    entry = cache.read_manifest()[TARGET.target]
    assert entry["status"] == STATUS_ERROR
    assert entry["last_error"] == "portal timeout"


def test_manifest_records_no_graph_status(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.mark_no_graph(TARGET.target)
    entry = cache.read_manifest()[TARGET.target]
    assert entry["status"] == STATUS_NO_GRAPH


def test_manifest_marks_failure_as_stale_when_cached_image_exists(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_ok(TARGET.target)
    cache.mark_error(TARGET.target, "portal timeout")
    entry = cache.read_manifest()[TARGET.target]
    assert entry["status"] == STATUS_STALE
    assert entry["last_error"] == "portal timeout"
    assert entry["last_scraped_at"] is not None


def test_manifest_marks_no_graph_as_stale_when_cached_image_exists(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_no_graph(TARGET.target)
    assert cache.read_manifest()[TARGET.target]["status"] == STATUS_STALE


def test_manifest_persists_across_instances(tmp_path: Path) -> None:
    NetcareCache(tmp_path).mark_ok(TARGET.target)
    reloaded = NetcareCache(tmp_path).read_manifest()[TARGET.target]
    assert reloaded["status"] == STATUS_OK


def test_manifest_ignores_corrupt_manifest_file(tmp_path: Path) -> None:
    (tmp_path / CACHE_MANIFEST_NAME).write_text("{broken", encoding="utf-8")
    manifest = NetcareCache(tmp_path).read_manifest()
    assert all(entry["status"] == STATUS_PENDING for entry in manifest.values())


def test_manifest_file_is_valid_json(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.mark_ok(TARGET.target)
    payload = json.loads((tmp_path / CACHE_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["targets"][TARGET.target]["status"] == STATUS_OK


def test_manifest_updates_keep_prior_success_when_failing(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_ok(TARGET.target)
    first_seen = cache.read_manifest()[TARGET.target]["last_scraped_at"]
    cache.mark_error(TARGET.target, "boom")
    assert cache.read_manifest()[TARGET.target]["last_scraped_at"] == first_seen


def test_has_image_reports_file_presence(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    assert cache.has_image(TARGET.target) is False
    cache.store(TARGET.target, _graph_image_bytes())
    assert cache.has_image(TARGET.target) is True


# --- Scrape outcomes -------------------------------------------------------


def test_scrape_outcome_from_success() -> None:
    outcome = ScrapeOutcome.from_ok("1-1")
    assert outcome.target == "1-1"
    assert outcome.status == STATUS_OK
    assert outcome.saved is True


def test_scrape_outcome_from_no_graph() -> None:
    outcome = ScrapeOutcome.from_no_graph("1-1", "No graph placeholder")
    assert outcome.status == STATUS_NO_GRAPH
    assert outcome.saved is False


def test_scrape_outcome_from_error() -> None:
    outcome = ScrapeOutcome.from_error("1-1", "timeout")
    assert outcome.status == STATUS_ERROR
    assert outcome.error == "timeout"


# --- Date-partitioned cache (FR-15.4) --------------------------------------


def test_day_key_formats_a_date_as_iso() -> None:
    assert day_key(datetime(2026, 9, 20, 13, 45)) == "2026-09-20"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20", "2026-09-20"),
        (" 2026-09-20 ", "2026-09-20"),
        ("2026-09-20T13:45:00", "2026-09-20"),
        (datetime(2026, 9, 20, 13, 45), "2026-09-20"),
    ],
)
def test_normalize_day_key_accepts_dates_and_iso_strings(
    value: object,
    expected: str,
) -> None:
    assert normalize_day_key(value) == expected


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-45", "../../etc", "20260920x"])
def test_normalize_day_key_rejects_anything_that_is_not_a_calendar_date(value: str) -> None:
    """A bad key raises instead of creating a junk partition directory."""

    with pytest.raises(ValueError):
        normalize_day_key(value)


def test_format_date_filter_covers_the_whole_day() -> None:
    start, end = format_date_filter(datetime(2026, 9, 20, 13, 45))
    assert start == "20/09/2026 00:00"
    assert end == "20/09/2026 23:55"


def test_format_date_range_uses_the_requested_window() -> None:
    start, end = format_date_range(
        datetime(2026, 9, 20, 8, 15),
        datetime(2026, 9, 20, 17, 30),
    )
    assert start == "20/09/2026 08:15"
    assert end == "20/09/2026 17:30"


def test_format_date_range_clamps_the_end_of_day() -> None:
    """The portal's 5-minute buckets stop at 23:55, so a later end is clamped."""

    _, end = format_date_range(
        datetime(2026, 9, 20, 0, 0),
        datetime(2026, 9, 20, 23, 59, 59),
    )
    assert end == "20/09/2026 23:55"


def test_format_date_range_never_returns_an_inverted_window() -> None:
    """An end before the start is pulled up to the start rather than rejected."""

    start, end = format_date_range(
        datetime(2026, 9, 20, 17, 30),
        datetime(2026, 9, 20, 8, 0),
    )
    assert start == "20/09/2026 17:30"
    assert end == "20/09/2026 17:30"


def test_live_cache_stays_flat_and_backward_compatible(tmp_path: Path) -> None:
    """The live partition keeps the original flat layout (FR-12.4)."""

    cache = NetcareCache(tmp_path)
    assert cache.image_path(TARGET.target) == tmp_path / f"{TARGET.target}.png"
    assert cache.image_path(TARGET.target, None) == tmp_path / f"{TARGET.target}.png"
    assert cache.manifest_path == tmp_path / CACHE_MANIFEST_NAME
    assert cache.day_dir(None) == tmp_path
    assert cache.day_manifest_path(None) == tmp_path / CACHE_MANIFEST_NAME


def test_day_partition_uses_a_subdirectory_and_its_own_manifest(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    day = "2026-09-20"

    assert cache.image_path(TARGET.target, day) == tmp_path / "2026-09-20" / f"{TARGET.target}.png"
    assert cache.day_dir(day) == tmp_path / "2026-09-20"
    assert cache.day_manifest_path(day) == tmp_path / DAY_MANIFEST_TEMPLATE.format(day=day)


def test_store_into_a_day_partition_leaves_the_live_cache_untouched(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    day = "2026-09-20"

    assert cache.store(TARGET.target, _graph_image_bytes(), day) is True

    assert cache.has_image(TARGET.target, day) is True
    assert cache.has_image(TARGET.target) is False
    assert (tmp_path / day / f"{TARGET.target}.png").is_file()


def test_live_and_day_manifests_are_tracked_separately(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    day = "2026-09-20"

    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_ok(TARGET.target)
    cache.store(TARGET.target, _graph_image_bytes(), day)
    cache.mark_ok(TARGET.target, day)

    assert cache.read_manifest()[TARGET.target]["status"] == STATUS_OK
    assert cache.read_manifest(day)[TARGET.target]["status"] == STATUS_OK

    payload = json.loads((tmp_path / DAY_MANIFEST_TEMPLATE.format(day=day)).read_text("utf-8"))
    assert payload["day"] == day
    assert TARGET.target in payload["targets"]


def test_a_day_manifest_records_its_own_partition_key(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.mark_ok(TARGET.target, "2026-09-20")

    payload = json.loads(cache.day_manifest_path("2026-09-20").read_text("utf-8"))
    assert payload["day"] == "2026-09-20"


def test_partition_ready_requires_every_target(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    day = "2026-09-20"
    first, second = DEFAULT_TARGETS[0], DEFAULT_TARGETS[1]
    wanted = [first.target, second.target]

    assert cache.partition_ready(day, wanted) is False

    cache.store(first.target, _graph_image_bytes(), day)
    assert cache.partition_ready(day, wanted) is False

    cache.store(second.target, _graph_image_bytes(), day)
    assert cache.partition_ready(day, wanted) is True


def test_partition_ready_of_an_empty_selection_is_true(tmp_path: Path) -> None:
    assert NetcareCache(tmp_path).partition_ready("2026-09-20", []) is True


def test_list_partitions_returns_only_captured_days(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    assert cache.list_partitions() == []

    cache.store(TARGET.target, _graph_image_bytes(), "2026-09-20")
    cache.store(TARGET.target, _graph_image_bytes(), "2026-09-18")
    # A stray directory is not a partition.
    (tmp_path / "not-a-day").mkdir()

    assert cache.list_partitions() == ["2026-09-18", "2026-09-20"]


def test_list_partitions_of_a_missing_cache_dir_is_empty(tmp_path: Path) -> None:
    assert NetcareCache(tmp_path / "absent").list_partitions() == []


def test_a_rejected_capture_leaves_the_previous_day_partition_intact(tmp_path: Path) -> None:
    """A failed day capture must not destroy what the operator already fetched."""

    cache = NetcareCache(tmp_path)
    day = "2026-09-20"
    cache.store(TARGET.target, _graph_image_bytes(), day)
    cache.mark_ok(TARGET.target, day)
    original = cache.image_path(TARGET.target, day).read_bytes()

    assert cache.store(TARGET.target, _solid_image_bytes(), day) is False
    cache.mark_no_graph(TARGET.target, "No graph placeholder", day)

    # The earlier good capture is still the one on disk and on the manifest.
    assert cache.image_path(TARGET.target, day).read_bytes() == original
    assert cache.read_manifest(day)[TARGET.target]["status"] == STATUS_STALE



def test_day_partition_error_degrades_to_stale(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    day = "2026-09-20"
    cache.store(TARGET.target, _graph_image_bytes(), day)
    cache.mark_ok(TARGET.target, day)

    cache.mark_error(TARGET.target, "portal timeout", day)

    assert cache.read_manifest(day)[TARGET.target]["status"] == STATUS_STALE
    # The live partition is untouched by a day-scoped failure.
    assert TARGET.target not in cache.read_manifest() or (
        cache.read_manifest()[TARGET.target]["status"] == STATUS_PENDING
    )


def test_manifest_writes_are_safe_under_concurrent_workers(tmp_path: Path) -> None:
    """Every worker's status update survives; none is lost to a read-modify-write race."""

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payloads = {t.target: _graph_image_bytes() for t in targets}
    start = threading.Barrier(3, timeout=5)

    def worker(chunk: list) -> None:
        start.wait()
        for target in chunk:
            assert cache.store(target.target, payloads[target.target]) is True
            cache.mark_ok(target.target)

    chunks = [targets[i::3] for i in range(3)]
    threads = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    manifest = cache.read_manifest()
    assert all(manifest[t.target]["status"] == STATUS_OK for t in targets)
    # No temp file is left behind, so no capture was half-written.
    assert not list(tmp_path.glob(".tmp-*"))


def test_concurrent_status_updates_for_one_target_settle_on_a_valid_state(
    tmp_path: Path,
) -> None:
    cache = NetcareCache(tmp_path)
    start = threading.Barrier(3, timeout=5)

    def worker() -> None:
        start.wait()
        cache.mark_ok(TARGET.target)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entry = cache.read_manifest()[TARGET.target]
    assert entry["status"] == STATUS_OK
    assert entry["last_scraped_at"]


# --- Newest-partition auto-promotion (Task 30.4) ----------------------------


def test_promote_newest_partition_copies_the_newest_day_into_the_flat_cache(
    tmp_path: Path,
) -> None:
    """The default view reads the root cache, so the newest day must land there."""

    cache = NetcareCache(tmp_path)
    day = "2026-09-29"
    cache.store(TARGET.target, _graph_image_bytes(), day)
    cache.mark_ok(TARGET.target, day)
    expected = cache.image_path(TARGET.target, day).read_bytes()

    assert cache.promote_newest_partition() == day

    assert cache.has_image(TARGET.target) is True
    assert cache.image_path(TARGET.target).read_bytes() == expected
    assert cache.read_manifest()[TARGET.target]["status"] == STATUS_OK
    assert cache.read_manifest()[TARGET.target]["last_scraped_at"]


def test_promote_newest_partition_picks_the_latest_day_not_the_first(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    old_day, new_day = "2026-09-28", "2026-09-29"
    cache.store(TARGET.target, _graph_image_bytes(600, 300), old_day)
    cache.mark_ok(TARGET.target, old_day)
    newer = _graph_image_bytes(640, 320)
    cache.store(TARGET.target, newer, new_day)
    cache.mark_ok(TARGET.target, new_day)

    assert cache.promote_newest_partition() == new_day

    assert cache.image_path(TARGET.target).read_bytes() == newer


def test_promote_newest_partition_copies_only_the_captured_targets(tmp_path: Path) -> None:
    """A partial round must promote what it has, not invent the rest."""

    cache = NetcareCache(tmp_path)
    captured = DEFAULT_TARGETS[0]
    missing = DEFAULT_TARGETS[1]
    day = "2026-09-29"
    cache.store(captured.target, _graph_image_bytes(), day)
    cache.mark_ok(captured.target, day)

    assert cache.promote_newest_partition() == day

    assert cache.has_image(captured.target) is True
    assert cache.has_image(missing.target) is False
    assert cache.read_manifest()[missing.target]["status"] == STATUS_PENDING


def test_promote_newest_partition_is_a_no_op_once_the_root_is_current(tmp_path: Path) -> None:
    """Every dashboard poll asks for a promotion, so a second call must do nothing."""

    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes(), "2026-09-29")
    cache.mark_ok(TARGET.target, "2026-09-29")

    assert cache.promote_newest_partition() == "2026-09-29"
    promoted = cache.image_path(TARGET.target).read_bytes()

    assert cache.promote_newest_partition() is None
    assert cache.image_path(TARGET.target).read_bytes() == promoted


def test_promote_newest_partition_returns_none_without_any_partition(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())

    assert cache.promote_newest_partition() is None


def test_promote_newest_partition_never_overwrites_a_newer_live_capture(
    tmp_path: Path,
) -> None:
    """A live round that ran after the partition owns the root cache."""

    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes(), "2026-09-29")
    cache.mark_ok(TARGET.target, "2026-09-29")
    live_image = _graph_image_bytes(700, 350)
    cache.store(TARGET.target, live_image)
    cache.mark_ok(TARGET.target)

    assert cache.promote_newest_partition() is None
    assert cache.image_path(TARGET.target).read_bytes() == live_image


def test_promote_newest_partition_merges_partition_statuses_into_the_root(
    tmp_path: Path,
) -> None:
    """A failure recorded in the newest day has to be visible in the default view."""

    cache = NetcareCache(tmp_path)
    day = "2026-09-29"
    cache.store(TARGET.target, _graph_image_bytes(), day)
    cache.mark_ok(TARGET.target, day)
    broken = DEFAULT_TARGETS[1]
    cache.mark_error(broken.target, "portal timeout", day)

    assert cache.promote_newest_partition() == day

    manifest = cache.read_manifest()
    assert manifest[TARGET.target]["status"] == STATUS_OK
    assert manifest[broken.target]["status"] == STATUS_ERROR
    assert manifest[broken.target]["last_error"] == "portal timeout"


def test_promote_newest_partition_keeps_a_good_live_status_over_a_day_failure(
    tmp_path: Path,
) -> None:
    """Promoting a failed capture must not hide an image the operator can still read."""

    cache = NetcareCache(tmp_path)
    cache.store(TARGET.target, _graph_image_bytes())
    cache.mark_ok(TARGET.target)
    live_image = cache.image_path(TARGET.target).read_bytes()
    cache.mark_error(TARGET.target, "portal timeout", "2026-09-29")

    cache.promote_newest_partition()

    assert cache.read_manifest()[TARGET.target]["status"] == STATUS_OK
    assert cache.has_image(TARGET.target) is True
    assert cache.image_path(TARGET.target).read_bytes() == live_image


def test_promote_newest_partition_leaves_no_temp_files(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    for target in DEFAULT_TARGETS[:3]:
        cache.store(target.target, _graph_image_bytes(), "2026-09-29")
        cache.mark_ok(target.target, "2026-09-29")

    cache.promote_newest_partition()

    assert not list(tmp_path.glob(".tmp-*"))


def test_manifest_stamp_is_finer_than_the_displayed_timestamp(tmp_path: Path) -> None:
    """Two captures in the same second must still be orderable.

    ``last_scraped_at`` stays second-resolution because the browser parses it,
    so the manifest envelope carries the sub-second part the promotion check
    needs to tell those two apart.
    """

    cache = NetcareCache(tmp_path)
    cache.mark_ok(TARGET.target, "2026-09-29")

    payload = json.loads(cache.day_manifest_path("2026-09-29").read_text("utf-8"))
    display = payload["targets"][TARGET.target]["last_scraped_at"]

    assert "." in payload["updated_at"].split("+")[0]
    assert payload["updated_at"] > display


def test_concurrent_promotions_settle_on_one_valid_root_manifest(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    for target in DEFAULT_TARGETS[:4]:
        cache.store(target.target, _graph_image_bytes(), "2026-09-29")
        cache.mark_ok(target.target, "2026-09-29")
    start = threading.Barrier(3, timeout=5)

    def worker() -> None:
        start.wait()
        cache.promote_newest_partition()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    manifest = cache.read_manifest()
    assert all(
        manifest[target.target]["status"] == STATUS_OK for target in DEFAULT_TARGETS[:4]
    )
    assert json.loads(cache.manifest_path.read_text("utf-8"))["day"] == LIVE_DAY
    assert not list(tmp_path.glob(".tmp-*"))
