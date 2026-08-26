"""Integration tests for FastAPI web dashboard and reporting endpoints."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mrtg_cmp.auth import hash_password
from mrtg_cmp.config import settings
from mrtg_cmp.db import Database, TrafficSample
from mrtg_cmp.netcare.targets import NetcareTarget
from mrtg_cmp.web.app import app

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "mrtg_cmp" / "web" / "templates"


@pytest.fixture
def client_with_db(tmp_path: Path) -> TestClient:
    """Fixture providing a test client configured with a temporary database."""
    test_db_path = tmp_path / "web_test.db"
    db = Database(test_db_path)
    db.initialize()

    # Seed test user
    db.create_user("admin", hash_password("admin123"))

    # Seed some sample traffic records
    db.insert_traffic_sample(
        TrafficSample(
            timestamp="2026-09-15T08:00:00Z",
            rx_bytes=100_000_000,
            tx_bytes=50_000_000,
            rx_bps=10_000_000.0,
            tx_bps=5_000_000.0,
            epoch=1789459200,
            uptime="5d",
            status="UP",
        )
    )

    # Override app dependency
    settings.database_path = test_db_path
    client = TestClient(app, follow_redirects=False)
    return client

def test_login_page_renders(client_with_db: TestClient) -> None:
    """GET /login returns 200 with the HTML login form."""
    resp = client_with_db.get("/login")
    assert resp.status_code == 200
    assert "Authentication" in resp.text
    assert "username" in resp.text

def test_dashboard_unauthenticated_redirects(client_with_db: TestClient) -> None:
    """Unauthenticated browser request to / redirects to /login."""
    resp = client_with_db.get("/", headers={"Accept": "text/html"})
    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers.get("location", "")

def test_login_failure(client_with_db: TestClient) -> None:
    """POST /login with wrong password returns 401."""
    resp = client_with_db.post(
        "/login",
        data={"username": "admin", "password": "WrongPassword!"},
    )
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text

def test_login_success_and_session(client_with_db: TestClient) -> None:
    """POST /login with correct credentials sets session cookie and redirects."""
    resp = client_with_db.post(
        "/login",
        data={"username": "admin", "password": "admin123", "remember_me": "true"},
    )
    assert resp.status_code == 303
    assert "mrtg_session" in resp.cookies

    # Access protected dashboard using authenticated cookie
    dash_resp = client_with_db.get("/", cookies=resp.cookies)
    assert dash_resp.status_code == 200
    assert "MRTG Traffic Monitor" in dash_resp.text
    assert "Traffic WAN" in dash_resp.text
    assert "theme-toggle" in dash_resp.text
    assert "countdown-timer" in dash_resp.text

def test_api_telemetry_endpoint(client_with_db: TestClient) -> None:
    """GET /api/telemetry returns structured JSON metrics and recent samples."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    resp = client_with_db.get("/api/telemetry", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "UP"
    assert "current_in_formatted" in data
    assert "current_out_formatted" in data
    assert "recent_samples" in data
    assert isinstance(data["recent_samples"], list)
    assert len(data["recent_samples"]) > 0
    first_sample = data["recent_samples"][0]
    assert "timestamp_wib" in first_sample
    assert "inbound_formatted" in first_sample
    assert "outbound_formatted" in first_sample
    assert "status" in first_sample

def test_api_graph_png_endpoint(client_with_db: TestClient) -> None:
    """GET /api/graph.png returns binary image/png content."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    resp = client_with_db.get("/api/graph.png?preset=today", cookies=cookies)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")

def test_api_exports_endpoints(client_with_db: TestClient) -> None:
    """GET /api/export/csv and /api/export/excel stream valid reports."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    # CSV
    csv_resp = client_with_db.get("/api/export/csv?preset=today", cookies=cookies)
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert b"Inbound (Mbps)" in csv_resp.content

    # Excel
    xlsx_resp = client_with_db.get("/api/export/excel?preset=today", cookies=cookies)
    assert xlsx_resp.status_code == 200
    assert "openxmlformats" in xlsx_resp.headers["content-type"]
    assert len(xlsx_resp.content) > 1000

def test_logout_clears_cookie(client_with_db: TestClient) -> None:
    """GET /logout revokes session and deletes cookie."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    logout_resp = client_with_db.get("/logout", cookies=cookies)
    assert logout_resp.status_code == 303
    assert "/login" in logout_resp.headers["location"]

def test_resolve_time_range_hourly_presets() -> None:
    """resolve_time_range accurately calculates sub-day time spans."""
    from mrtg_cmp.web.app import resolve_time_range

    for preset_name, expected_hours in [("1h", 1), ("3h", 3), ("6h", 6), ("12h", 12)]:
        st_ep, et_ep, disp_st, disp_et, active = resolve_time_range(preset=preset_name)
        assert active == preset_name
        assert (et_ep - st_ep) == expected_hours * 3600

def test_resolve_time_range_fullday() -> None:
    """resolve_time_range sets 00:00:00 to 23:59:59 WIB for a single selected date."""
    from mrtg_cmp.web.app import resolve_time_range

    st_ep, et_ep, disp_st, disp_et, active = resolve_time_range(fullday="2026-09-15")
    assert active == "fullday"
    assert disp_st == "2026-09-15 00:00:00"
    assert disp_et == "2026-09-15 23:59:59"
    assert (et_ep - st_ep) == 86399

    # Fallback on invalid format
    _, _, _, _, fallback_active = resolve_time_range(fullday="invalid-date")
    assert fallback_active == "today"

def test_dashboard_with_subday_presets_and_fullday(client_with_db: TestClient) -> None:
    """Dashboard handles sub-day presets and fullday parameter, rendering matrix modal elements."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    # Test preset 1h
    resp_1h = client_with_db.get("/?preset=1h", cookies=cookies)
    assert resp_1h.status_code == 200
    assert "active" in resp_1h.text
    assert "1 Hour" in resp_1h.text
    assert "matrix-modal-backdrop" in resp_1h.text
    assert "matrix-hours-grid" in resp_1h.text
    assert "matrix-minutes-grid" in resp_1h.text
    assert "btn-quick-now" in resp_1h.text
    assert "btn-fullday-modal" in resp_1h.text

    # Test fullday parameter
    resp_fd = client_with_db.get("/?fullday=2026-09-15", cookies=cookies)
    assert resp_fd.status_code == 200
    assert "2026-09-15" in resp_fd.text
    assert "fullday=2026-09-15" in resp_fd.text

def test_api_graph_and_exports_with_fullday(client_with_db: TestClient) -> None:
    """Graph rendering and exports work seamlessly with fullday and subday presets."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    # Graph with fullday
    graph_resp = client_with_db.get("/api/graph.png?fullday=2026-09-15", cookies=cookies)
    assert graph_resp.status_code == 200
    assert graph_resp.headers["content-type"] == "image/png"

    # CSV with fullday
    csv_resp = client_with_db.get("/api/export/csv?fullday=2026-09-15", cookies=cookies)
    assert csv_resp.status_code == 200
    assert "2026-09-15" in csv_resp.headers["content-disposition"]

    # Excel with fullday
    xlsx_resp = client_with_db.get("/api/export/excel?fullday=2026-09-15", cookies=cookies)
    assert xlsx_resp.status_code == 200
    assert "2026-09-15" in xlsx_resp.headers["content-disposition"]

def _resolved_targets() -> list[NetcareTarget]:
    """The catalog the endpoints themselves resolve, for asserting parity with it.

    ``resolve_targets()`` called bare ignores ``NETCARE_CATALOG_FILE`` and falls
    back to discovering ``config/netcare_targets.csv`` in the checkout, which on
    a deployment is a different catalog from the one under test. Tests read the
    effective catalog through this helper so both sides use the same one.
    """

    from mrtg_cmp.netcare.targets import resolve_targets

    return resolve_targets(settings.netcare_catalog_file)


def _seed_netcare_cache(cache_dir: Path, target_ids: list[str]) -> dict[str, Path]:
    """Write one valid graph image per target into the Netcare cache."""

    import io

    from PIL import Image

    from mrtg_cmp.netcare.scraper import NetcareCache

    cache = NetcareCache(cache_dir)
    written: dict[str, Path] = {}
    for target_id in target_ids:
        image = Image.new("RGB", (600, 300), "white")
        pixels = image.load()
        assert pixels is not None
        for x in range(600):
            for y in range(150, 280):
                pixels[x, y] = (0, 204, 0) if (x // 17) % 2 else (0, 140, 0)
        for x in range(0, 600, 3):
            for y in range(10, 25):
                pixels[x, y] = (0, 0, 0)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        assert cache.store(target_id, buffer.getvalue()) is True
        cache.mark_ok(target_id)
        written[target_id] = cache.image_path(target_id)
    return written

def test_api_netcare_targets_requires_auth(client_with_db: TestClient) -> None:
    """GET /api/netcare/targets rejects unauthenticated API requests."""
    resp = client_with_db.get("/api/netcare/targets")
    assert resp.status_code == 401

def test_api_netcare_targets_returns_eighteen_entries(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/netcare/targets returns every catalog target with metadata."""

    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "empty-netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies)

    assert resp.status_code == 200
    payload = resp.json()
    targets = payload["targets"]
    assert len(targets) == 18
    assert [t["target"] for t in targets] == [t.target for t in _resolved_targets()]

    first = targets[0]
    assert first["name"]
    assert first["address"]
    assert first["region"]
    assert first["region_label"]
    assert first["status"] == "pending"
    assert first["last_scraped_at"] is None
    assert first["image_url"] == f"/api/netcare/graph/{first['target']}.png"

def test_api_netcare_targets_serve_the_resolved_catalog(
    client_with_db: TestClient,
) -> None:
    """The API reports whichever catalog is in effect, verbatim.

    Which catalog that is depends on the deployment, so the assertion is parity
    with :func:`resolve_targets` rather than a fixed set of names: a local
    ``config/netcare_targets.csv`` must be served as it is, and the anonymous
    built-in catalog must be served as it is.
    """
    from mrtg_cmp.netcare.targets import region_label

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    targets = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies).json()[
        "targets"
    ]

    by_target = {t["target"]: t for t in targets}
    expected = {t.target: t for t in _resolved_targets()}
    assert set(by_target) == set(expected)
    for target_id, target in expected.items():
        assert by_target[target_id]["name"] == target.name
        assert by_target[target_id]["address"] == target.address
        assert by_target[target_id]["region"] == target.region
        assert by_target[target_id]["region_label"] == region_label(target.region)


def test_api_netcare_targets_serve_the_deployment_catalog_override(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's own catalog must reach the dashboard without a rebuild.

    ``config/netcare_targets.csv`` is gitignored, so this is the path a public
    clone never exercises on its own: the catalog is supplied at runtime through
    ``NETCARE_CATALOG_FILE`` and its branch names have to come straight through.
    """
    catalog = tmp_path / "netcare_targets.csv"
    catalog.write_text(
        "type,target,name,address,region,ocr_enabled,service_type\n"
        "SID,own-circuit-1,Own Branch One,Own Address One,CGK,true,Astinet\n"
        "Graph-title,own-circuit-2,Own Branch Two,Own Address Two,SUB,true,Metro-E\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "netcare_catalog_file", str(catalog))

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    targets = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies).json()[
        "targets"
    ]

    by_target = {t["target"]: t for t in targets}
    assert set(by_target) == {"own-circuit-1", "own-circuit-2"}
    assert by_target["own-circuit-1"]["name"] == "Own Branch One"
    assert by_target["own-circuit-1"]["region_label"] == "CGK Area"
    assert by_target["own-circuit-2"]["service_label"] == "Metro-E"
    # The rendered dashboard has to show the override too, not just the API.
    page = client_with_db.get("/", cookies=login_resp.cookies)
    assert page.status_code == 200
    assert "Own Branch One" in page.text
    assert "Own Branch Two" in page.text

def test_api_netcare_targets_groups_by_region(client_with_db: TestClient) -> None:
    """GET /api/netcare/targets returns region groups with display labels."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies)

    payload = resp.json()
    regions = payload["regions"]
    assert [r["code"] for r in regions] == ["CGK", "SUB", "UPG", "DPS", "BPN", "SENTUL"]
    assert [r["label"] for r in regions] == [
        "CGK Area",
        "Surabaya",
        "Makassar",
        "Denpasar",
        "Balikpapan",
        "Sentul VPN",
    ]
    assert [r["count"] for r in regions] == [7, 2, 4, 2, 1, 2]
    assert sum(r["count"] for r in regions) == len(payload["targets"])

def test_api_netcare_targets_reports_scrape_status(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful scrape surfaces status ok, a timestamp, and a file size."""
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    target = DEFAULT_TARGETS[0]
    _seed_netcare_cache(tmp_path / "netcare", [target.target])
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies)

    entry = next(t for t in resp.json()["targets"] if t["target"] == target.target)
    assert entry["status"] == "ok"
    assert entry["last_scraped_at"] is not None
    assert entry["file_size"] > 0
    assert entry["has_image"] is True

def test_api_netcare_graph_serves_cached_png(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/netcare/graph/{target}.png streams the cached image with cache headers."""
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    target = DEFAULT_TARGETS[0]
    written = _seed_netcare_cache(tmp_path / "netcare", [target.target])
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get(f"/api/netcare/graph/{target.target}.png", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == written[target.target].read_bytes()
    assert "max-age" in resp.headers.get("cache-control", "")

def test_api_netcare_graph_returns_404_for_unknown_target(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown target id never resolves to a file outside the cache."""
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/graph/not-a-target.png", cookies=login_resp.cookies)
    assert resp.status_code == 404

def test_api_netcare_graph_rejects_path_traversal(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path traversal attempts cannot escape the cache directory."""
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get(
        "/api/netcare/graph/..%2F..%2Ftraffic.db.png", cookies=login_resp.cookies
    )
    assert resp.status_code == 404

def test_api_netcare_graph_download_sets_filename(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download flag returns the PNG as an attachment."""
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    target = DEFAULT_TARGETS[0]
    _seed_netcare_cache(tmp_path / "netcare", [target.target])
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get(
        f"/api/netcare/graph/{target.target}.png?download=1", cookies=login_resp.cookies
    )

    assert resp.status_code == 200
    assert f'filename="{target.target}.png"' in resp.headers["content-disposition"]

def test_api_netcare_refresh_queues_target(client_with_db: TestClient) -> None:
    """POST /api/netcare/refresh/{target} queues a priority scrape."""
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    target = DEFAULT_TARGETS[0]
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.post(
        f"/api/netcare/refresh/{target.target}", cookies=login_resp.cookies
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    assert target.target in body["targets"]

def test_api_netcare_refresh_rejects_unknown_target(client_with_db: TestClient) -> None:
    """An unknown target id is not queued."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.post("/api/netcare/refresh/not-a-target", cookies=login_resp.cookies)

    assert resp.status_code == 404

def test_orbit_route_renders_placeholder(client_with_db: TestClient) -> None:
    """GET /orbit is an authenticated placeholder for the Telkomsel Orbit dashboard."""
    unauth = client_with_db.get("/orbit", headers={"Accept": "text/html"})
    assert unauth.status_code in (302, 303, 307)
    assert "/login" in unauth.headers.get("location", "")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/orbit", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert "Telkomsel Orbit" in resp.text
    assert "Reserved" in resp.text or "reserved" in resp.text

def test_dashboard_renders_netcare_section(client_with_db: TestClient) -> None:
    """The unified dashboard renders the Netcare branch grid and filter pills."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert "TelkomCare Netcare" in resp.text
    assert "netcare-filter-pills" in resp.text
    assert "netcare-card-grid" in resp.text
    assert "netcare-modal" in resp.text
    assert "netcare-refresh-all" in resp.text
    # 18 branch cards plus the regional filter pills
    assert resp.text.count("netcare-card") >= 18
    for label in ("CGK Area", "Surabaya", "Makassar", "Denpasar", "Balikpapan", "Sentul VPN"):
        assert label in resp.text

def test_dashboard_renders_netcare_time_preset_toolbar(client_with_db: TestClient) -> None:
    """The Netcare section carries its own full preset toolbar (FR-15.2)."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'id="netcare-toolbar"' in resp.text
    assert 'id="netcare-preset-group"' in resp.text
    assert 'id="netcare-range-form"' in resp.text
    assert 'id="netcare-start"' in resp.text
    assert 'id="netcare-end"' in resp.text
    assert 'id="netcare-btn-now"' in resp.text
    assert "YYYY-MM-DD HH:MM" in resp.text

    # Every preset named in FR-15.2 is present, scoped to the Netcare toolbar.
    for preset in (
        "1h",
        "3h",
        "6h",
        "12h",
        "24h",
        "today",
        "yesterday",
        "7d",
        "month",
        "fullday",
    ):
        assert f'data-preset="{preset}"' in resp.text

    for label in (
        "1 Hour",
        "3 Hours",
        "6 Hours",
        "12 Hours",
        "24 Hours",
        "Today",
        "Yesterday",
        "7 Days",
        "This Month",
        "Select Day (24h)",
    ):
        assert label in resp.text

    # The worker count is surfaced so the operator knows the pool width.
    assert "netcare-workers-badge" in resp.text
    assert "3 workers" in resp.text

def test_dashboard_netcare_toolbar_matches_the_shared_presets(
    client_with_db: TestClient,
) -> None:
    """Both toolbars expose the same preset set, so the ranges mean the same thing."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    netcare_html = resp.text[
        resp.text.index('id="netcare-toolbar"') : resp.text.index('id="netcare-filter-pills"')
    ]
    mikrotik_html = resp.text[resp.text.index('class="controls-card"') :]

    mikrotik_presets = set(re.findall(r'href="/\?preset=([a-z0-9]+)"', mikrotik_html))
    netcare_presets = set(re.findall(r'data-preset="([a-z0-9]+)"', netcare_html))

    assert mikrotik_presets == {"1h", "3h", "6h", "12h", "24h", "today", "yesterday", "7d", "month"}
    assert mikrotik_presets.issubset(netcare_presets)
    # Netcare adds the full-day picker on top of the shared set.
    assert netcare_presets - mikrotik_presets == {"fullday"}

def test_dashboard_renders_netcare_progress_dialog(client_with_db: TestClient) -> None:
    """The polite live loading dialog ships its spinner and progress bar (FR-15.3).

    Task 31.0: the speculative estimate is gone. TelkomCare's portal latency
    varies enough that a countdown the dialog cannot keep was a lie the
    operator watched, so the dialog states only what has already happened.
    """

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'id="netcare-progress-backdrop"' in resp.text
    assert 'id="netcare-progress-spinner"' in resp.text
    assert 'id="netcare-progress-bar"' in resp.text
    assert 'id="netcare-progress-count"' in resp.text
    assert 'id="netcare-progress-cancel"' in resp.text

    # No speculative estimate is rendered anywhere in the dialog.
    assert 'id="netcare-progress-eta"' not in resp.text
    assert "Estimasi selesai" not in resp.text

    assert "Mengambil data grafik dari TelkomCare..." in resp.text
    assert "cabang selesai" in resp.text

    # Client-side wiring: dialog control, polling, and the auto-dismiss timer.
    assert "openNetcareProgress" in resp.text
    assert "pollNetcareStatus" in resp.text
    assert "runNetcareQuery" in resp.text
    assert "finishNetcareProgress" in resp.text
    assert "/api/netcare/query" in resp.text
    assert "/api/netcare/status" in resp.text
    assert "setTimeout(closeNetcareProgress, 1000)" in resp.text

    # The dialog is polite: hidden until a query starts, and the copy reassures
    # the operator that the page stays usable.
    assert 'class="netcare-progress-backdrop"' in resp.text
    assert "halaman tetap bisa digunakan" in resp.text


def test_dashboard_dialog_ships_only_an_elapsed_stopwatch_and_a_stage_badge(
    client_with_db: TestClient,
) -> None:
    """The dialog counts time up and narrates the stage; it never counts down.

    The countdown is asserted absent, not merely unreferenced: a leftover
    element would keep showing a figure the operator has no reason to trust.
    """

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'id="netcare-progress-elapsed"' in resp.text
    assert 'id="netcare-progress-stage"' in resp.text
    assert "Waktu berjalan:" in resp.text
    assert "00:00" in resp.text

    # The countdown is gone, element and formatter both.
    assert 'id="netcare-progress-remaining"' not in resp.text
    assert "netcare-progress-remaining" not in resp.text
    assert "netcareProgressRemaining" not in resp.text
    assert "netcareProgressEta" not in resp.text
    assert "formatRemaining" not in resp.text
    assert "formatEta" not in resp.text
    assert "netcareEtaSeconds" not in resp.text
    assert "Sisa:" not in resp.text

    # One ticker drives the stopwatch, and it counts up.
    assert "setNetcareElapsed" in resp.text
    assert "formatClock" in resp.text
    assert "setNetcareStage" in resp.text
    assert "netcareElapsed += 1" in resp.text
    assert "setNetcareTimers" not in resp.text
    assert 'role="status" aria-live="polite">Menyiapkan' in resp.text


def test_dashboard_dialog_cancel_button_reaches_the_cancel_endpoint(
    client_with_db: TestClient,
) -> None:
    """Batal must actually abandon the job, not just close the dialog."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert "/api/netcare/cancel" in resp.text
    assert "cancelNetcareQuery" in resp.text
    # Cancelling and hiding stay distinct: the operator can still watch a job run.
    assert 'id="netcare-progress-dismiss"' in resp.text
    assert "Batalkan" in resp.text
    assert "Sembunyikan" in resp.text
    assert "data.cancelled" in resp.text


def test_dashboard_omits_recent_traffic_samples_table(client_with_db: TestClient) -> None:
    """The collapsible Recent Traffic Samples table and its toggle are gone."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    for removed in (
        "Recent Traffic Samples",
        "recent-samples-toggle",
        "recent-samples-body",
        "recent-samples-arrow",
        "recent-samples-tbody",
        "No traffic samples recorded yet",
    ):
        assert removed not in resp.text

def test_dashboard_places_netcare_above_live_traffic(client_with_db: TestClient) -> None:
    """Netcare section 1 renders above the live WAN section 2 traffic graph."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    netcare_index = resp.text.index('id="netcare-section"')
    live_index = resp.text.index('id="live-traffic-section"')
    graph_index = resp.text.index('id="netcare-card-grid"')
    wan_graph_index = resp.text.index('id="traffic-graph-img"')

    assert netcare_index < live_index
    assert graph_index < wan_graph_index
    assert "TelkomCare Netcare" in resp.text[netcare_index:live_index]
    assert resp.text.count('id="netcare-card-grid"') == 1

def test_dashboard_renders_seeded_netcare_images(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached branch graphs render as card images instead of the empty placeholder."""

    cache_dir = tmp_path / "netcare"
    _seed_netcare_cache(cache_dir, [t.target for t in _resolved_targets()])
    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'class="netcare-card-image"' in resp.text
    assert "No graph captured yet" not in resp.text

def test_dashboard_card_images_keep_a_single_question_mark_for_a_day_partition(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day-partitioned card URL already carries ``?day=...``.

    Appending the cache-busting stamp with another ``?`` produced
    ``?day=2026-09-20?t=0``, so the portal path was never re-read after a query.
    """

    from mrtg_cmp.netcare.scraper import NetcareCache

    cache_dir = tmp_path / "partitioned"
    cache = NetcareCache(cache_dir)
    day = "2026-09-20"
    for target in _resolved_targets():
        written = _seed_netcare_cache(cache_dir, [target.target])
        assert cache.store(target.target, written[target.target].read_bytes(), day)
        cache.mark_ok(target.target, day)
    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get(f"/?netcare_day={day}", cookies=login_resp.cookies)

    assert resp.status_code == 200
    sources = re.findall(r'<img src="([^"]*netcare[^"]*)"', resp.text)
    assert sources, "no Netcare card images were rendered"
    for src in sources:
        # The browser requests the unescaped attribute value, so that is the
        # contract: one query string carrying both parameters.
        requested = html.unescape(src)
        assert requested.count("?") == 1, src
        assert f"day={day}&t=0" in requested, src

def test_dashboard_never_joins_a_bare_question_mark_onto_an_image_url() -> None:
    """Every stamp and download parameter goes through the one joining helper."""

    template = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert "?t=${" not in template
    assert "{{ card.image_url }}?t=0" not in template
    assert "?download=1" not in template
    assert template.count("netcareImageSrc(") >= 4


def _login(client: TestClient):
    """Authenticate the throwaway test client and return the login response."""

    return client.post("/login", data={"username": "admin", "password": "admin123"})


def test_dashboard_template_carries_no_speculative_estimate() -> None:
    """Task 31.0: the template must not interpolate an estimate at all.

    Task 28.0 pinned the ``default(120)`` fallback literal because a stale
    fallback is only reached on an unusual render. The stronger fix is that the
    template references no estimate in the first place, so there is no second
    place for a stale figure to survive in.
    """

    template = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert "netcare_estimated_seconds" not in template
    assert "NETCARE_ESTIMATE_SECONDS" not in template
    assert "default(45)" not in template
    assert "default(120)" not in template


def test_api_estimate_survives_while_the_dialog_hides_it(
    client_with_db: TestClient,
    netcare_query_client: TestClient,
) -> None:
    """18 branches over the default 3 workers still estimate 120s for API clients.

    The endpoint keeps its contract; only the dialog stopped rendering the
    figure, so the number is still worth pinning somewhere.
    """

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    query_resp = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    )
    assert query_resp.status_code == 202
    assert query_resp.json()["estimated_seconds"] == 120

    resp = client_with_db.get("/", cookies=_login(client_with_db).cookies)
    assert resp.status_code == 200
    assert "Estimasi selesai" not in resp.text
    assert "120 detik" not in resp.text

def test_dashboard_uses_three_minute_refresh(client_with_db: TestClient) -> None:
    """FR-13.1 / Task 30.4: the dashboard auto-refresh timer is 180 seconds."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'id="countdown-timer"' in resp.text
    assert "const NETCARE_REFRESH_SECONDS = 180;" in resp.text
    # The template fallback has to agree with the served value, or a render that
    # omits the setting opens the countdown on a contradictory figure.
    assert "default(180)" in (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

def test_navbar_links_cover_mrtg_orbit_and_console(client_with_db: TestClient) -> None:
    """FR-13.4: navigation exposes MRTG Monitoring, Telkomsel Orbit, and Web Console."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert 'href="/"' in resp.text
    assert 'href="/orbit"' in resp.text
    assert 'href="/console"' in resp.text

def test_console_page_and_endpoints(client_with_db: TestClient) -> None:
    """GET /console renders terminal UI and API endpoints enforce auth and validation."""
    # Unauthenticated GET /console redirects to /login
    unauth_resp = client_with_db.get("/console", headers={"Accept": "text/html"})
    assert unauth_resp.status_code == 307
    assert "/login" in unauth_resp.headers["location"]

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    cookies = login_resp.cookies

    # Authenticated GET /console
    console_resp = client_with_db.get("/console", cookies=cookies)
    assert console_resp.status_code == 200
    assert "RouterOS Authentication" in console_resp.text
    assert "terminal-screen" in console_resp.text
    assert "terminal-input" in console_resp.text
    assert "terminal-suggestions-bar" in console_resp.text

    # Execute with invalid/expired token returns session_expired: True
    exec_resp = client_with_db.post(
        "/api/console/execute",
        json={"token": "invalid_fake_token", "command": "/ip address print"},
        cookies=cookies,
    )
    assert exec_resp.status_code == 200
    data = exec_resp.json()
    assert data["success"] is False
    assert data["session_expired"] is True

    # Terminate token endpoint
    term_resp = client_with_db.post(
        "/api/console/terminate",
        json={"token": "any_token"},
        cookies=cookies,
    )
    assert term_resp.status_code == 200
    assert term_resp.json()["success"] is True

    # Get console logs endpoint
    logs_resp = client_with_db.get("/api/console/logs", cookies=cookies)
    assert logs_resp.status_code == 200
    assert "logs" in logs_resp.json()


# --- Netcare on-demand query API (FR-15.2 / FR-15.3) ------------------------


@pytest.fixture
def netcare_query_calls() -> list[dict[str, object]]:
    """Recorder for the arguments each Netcare query runner was invoked with."""

    return []


@pytest.fixture
def netcare_query_client(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    netcare_query_calls: list[dict[str, object]],
) -> TestClient:
    """Authenticated client whose Netcare queries are served by a stub runner.

    The stub reports every target as captured so a job settles quickly, and any
    test that needs a different behaviour re-patches ``NetcareService.query``.
    """

    from mrtg_cmp.netcare.scraper import ScrapeOutcome
    from mrtg_cmp.netcare.service import NetcareService
    from mrtg_cmp.web import app as app_module

    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "netcare-query")
    app_module._TRACKERS.clear()

    def stub_query(
        self: NetcareService,
        window: object = None,
        target_ids: object = None,
        pool: object = None,
        base_url: object = None,
        workers: int = 3,
        progress: object = None,
        day: str | None = None,
        stage: object = None,
        cancelled: object = None,
    ) -> list[ScrapeOutcome]:
        netcare_query_calls.append({"window": window, "day": day, "workers": workers})
        total = len(target_ids) if target_ids else 18
        for index in range(total):
            if stage is not None:
                stage(
                    f"Mengambil link: Branch {index + 1} ({index + 1}/{total})...",
                    index + 1,
                    total,
                )
            if progress is not None:
                progress(ScrapeOutcome.from_ok(f"target-{index}"), index + 1, total)
        return []

    monkeypatch.setattr(NetcareService, "query", stub_query)
    try:
        yield client_with_db
    finally:
        app_module._TRACKERS.clear()


def test_api_netcare_query_requires_auth(client_with_db: TestClient) -> None:
    """POST /api/netcare/query rejects unauthenticated requests."""
    resp = client_with_db.post("/api/netcare/query", json={"preset": "today"})
    assert resp.status_code == 401


def test_api_netcare_query_returns_a_job_without_blocking(
    netcare_query_client: TestClient,
) -> None:
    """POST /api/netcare/query accepts the request and answers 202 with a job id."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    resp = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"]
    assert body["preset"] == "today"
    assert body["total"] == 18
    # The live window writes the flat cache, so no day partition is assigned.
    assert body["day"] is None
    # The dialog needs an opening estimate before any target completes.
    assert body["estimated_seconds"] > 0


@pytest.mark.parametrize(
    ("preset", "expect_partition"),
    [
        ("yesterday", True),
        ("7d", True),
        ("month", True),
        # A 24h window reaches back into yesterday, so it is partitioned by its
        # start date rather than overwriting the live "today" graph.
        ("24h", True),
        ("1h", False),
        ("3h", False),
        ("today", False),
    ],
)
def test_api_netcare_query_routes_historical_presets_to_a_day_partition(
    netcare_query_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
    expect_partition: bool,
) -> None:
    """A past window gets its own date partition; a current one stays live."""

    from mrtg_cmp.web import app as app_module

    # Pin WIB to 14:00. The partition decision compares the window start date to
    # now, so an unpinned clock flips the "1h"/"3h" rows into a partition during
    # the 00:00-01:00 WIB window and the suite flakes once a day. 14:00 also
    # keeps the "month" row on a month-start that is genuinely in the past.
    frozen = datetime(2026, 9, 15, 14, 0, 0)
    monkeypatch.setattr(app_module, "_now_wib", lambda: frozen)

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    resp = netcare_query_client.post(
        "/api/netcare/query", json={"preset": preset}, cookies=login_resp.cookies
    )

    assert resp.status_code == 202
    day = resp.json()["day"]
    if expect_partition:
        assert day is not None
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
    else:
        assert day is None


def test_api_netcare_query_accepts_a_custom_range(netcare_query_client: TestClient) -> None:
    """A custom From/To range is honoured and routed to its own day partition."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    resp = netcare_query_client.post(
        "/api/netcare/query",
        json={"preset": "custom", "start": "2026-09-20 08:00", "end": "2026-09-20 17:00"},
        cookies=login_resp.cookies,
    )

    assert resp.status_code == 202
    assert resp.json()["day"] == "2026-09-20"


def test_api_netcare_query_passes_the_worker_count_to_the_runner(
    netcare_query_client: TestClient,
    netcare_query_calls: list[dict[str, object]],
) -> None:
    """The endpoint hands the configured pool width and partition to the runner."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    )

    _await_calls(netcare_query_calls, 1)
    assert netcare_query_calls[0]["workers"] == settings.netcare_workers
    assert netcare_query_calls[0]["day"] is None



def test_api_netcare_query_restricts_to_known_targets(netcare_query_client: TestClient) -> None:
    """A target subset narrows the job; a fully unknown subset is rejected."""


    known = [t.target for t in _resolved_targets()]
    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )

    subset = netcare_query_client.post(
        "/api/netcare/query",
        json={"preset": "today", "targets": known[:2]},
        cookies=login_resp.cookies,
    )
    assert subset.status_code == 202
    assert subset.json()["total"] == 2

    unknown = netcare_query_client.post(
        "/api/netcare/query",
        json={"preset": "today", "targets": ["not-a-target"]},
        cookies=login_resp.cookies,
    )
    assert unknown.status_code == 400
    assert "No known Netcare targets" in unknown.json()["error"]


def test_api_netcare_query_rejects_a_non_json_body(netcare_query_client: TestClient) -> None:
    """A malformed body is a 400, not a stack trace."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    resp = netcare_query_client.post(
        "/api/netcare/query",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
        cookies=login_resp.cookies,
    )

    assert resp.status_code == 400
    assert "must be JSON" in resp.json()["error"]


def test_api_netcare_query_defaults_to_today_with_an_empty_body(
    netcare_query_client: TestClient,
) -> None:
    """An empty body is a valid "today" query so the refresh button needs no payload."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    resp = netcare_query_client.post("/api/netcare/query", cookies=login_resp.cookies)

    assert resp.status_code == 202
    assert resp.json()["preset"] == "today"


def _await_calls(calls: list[dict[str, object]], count: int) -> None:
    """Block until the background runner has been invoked ``count`` times."""

    import time

    for _ in range(200):
        if len(calls) >= count:
            return
        time.sleep(0.02)
    raise AssertionError("Netcare query runner was never invoked")


def _await_job(client: TestClient, cookies: object, job_id: str) -> dict:
    """Poll a query job until it settles, then return the final status body."""

    import time

    for _ in range(200):
        body = client.get(
            f"/api/netcare/status?job_id={job_id}", cookies=cookies  # type: ignore[arg-type]
        ).json()
        if not body["running"]:
            return body
        time.sleep(0.02)
    raise AssertionError("Netcare query job never finished")


def test_api_netcare_status_reports_progress_then_completion(
    netcare_query_client: TestClient,
) -> None:
    """Status polling walks completed counts to the total, then hands back image URLs."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]

    final = _await_job(netcare_query_client, login_resp.cookies, job_id)

    assert final["running"] is False
    assert final["completed"] == final["total"] == 18
    assert final["percent"] == 100.0
    assert final["ok"] == 18
    assert final["error"] is None
    assert final["eta_seconds"] == 0
    assert len(final["image_urls"]) == 18
    assert all(url.startswith("/api/netcare/graph/") for url in final["image_urls"].values())


def test_api_netcare_status_keeps_a_countdown_while_running(
    netcare_query_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that has not finished yet reports a positive countdown (FR-15.3)."""

    import threading
    import time

    from mrtg_cmp.netcare.service import NetcareService

    release = threading.Event()

    def slow_query(self: NetcareService, **kwargs: object) -> list:
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(NetcareService, "query", slow_query)
    try:
        login_resp = netcare_query_client.post(
            "/login", data={"username": "admin", "password": "admin123"}
        )
        job_id = netcare_query_client.post(
            "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
        ).json()["job_id"]

        time.sleep(0.1)
        body = netcare_query_client.get(
            f"/api/netcare/status?job_id={job_id}", cookies=login_resp.cookies
        ).json()

        assert body["running"] is True
        assert body["completed"] == 0
        assert body["eta_seconds"] > 0
        assert body["elapsed_seconds"] >= 0
    finally:
        release.set()




def test_api_netcare_status_404s_for_an_unknown_job(client_with_db: TestClient) -> None:
    """Polling an id that was never issued is a 404."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/status?job_id=nope", cookies=login_resp.cookies)
    assert resp.status_code == 404
    assert "Unknown Netcare query job" in resp.json()["error"]


def test_api_netcare_status_requires_auth(client_with_db: TestClient) -> None:
    """GET /api/netcare/status rejects unauthenticated requests."""
    assert client_with_db.get("/api/netcare/status?job_id=anything").status_code == 401


def test_api_netcare_query_surfaces_a_runner_failure(
    netcare_query_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that raises settles the job with an error instead of hanging."""

    from mrtg_cmp.netcare.service import NetcareService

    def boom(self: NetcareService, **kwargs: object) -> list:
        raise RuntimeError("browser session refused")

    monkeypatch.setattr(NetcareService, "query", boom)
    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]

    final = _await_job(netcare_query_client, login_resp.cookies, job_id)
    assert final["running"] is False
    assert "browser session refused" in (final["error"] or "")


# --- Stage reporting and cancellation over the API -------------------------


def test_api_netcare_status_streams_the_current_stage(netcare_query_client: TestClient) -> None:
    """The dialog renders verbatim what the scraper is doing right now."""

    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]

    final = _await_job(netcare_query_client, login_resp.cookies, job_id)

    assert final["stage"]
    assert isinstance(final["stage"], str)


def test_api_netcare_status_exposes_the_cancellation_flag(
    netcare_query_client: TestClient,
) -> None:
    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]

    final = _await_job(netcare_query_client, login_resp.cookies, job_id)
    assert final["cancelled"] is False


def test_api_netcare_status_reports_a_live_stage_while_running(
    netcare_query_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that is still working publishes a non-placeholder stage."""

    import threading
    import time

    from mrtg_cmp.netcare.query import STAGE_OPENING_BROWSER
    from mrtg_cmp.netcare.service import NetcareService

    release = threading.Event()

    def staged_query(self: NetcareService, **kwargs: object) -> list:
        stage = kwargs.get("stage")
        if stage is not None:
            stage(STAGE_OPENING_BROWSER, 0, 18)
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(NetcareService, "query", staged_query)
    try:
        login_resp = netcare_query_client.post(
            "/login", data={"username": "admin", "password": "admin123"}
        )
        job_id = netcare_query_client.post(
            "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
        ).json()["job_id"]

        time.sleep(0.1)
        body = netcare_query_client.get(
            f"/api/netcare/status?job_id={job_id}", cookies=login_resp.cookies
        ).json()

        assert body["running"] is True
        assert body["stage"] == STAGE_OPENING_BROWSER
        assert body["cancelled"] is False
    finally:
        release.set()


def test_api_netcare_cancel_requires_auth(client_with_db: TestClient) -> None:
    assert client_with_db.post("/api/netcare/cancel?job_id=anything").status_code == 401


def test_api_netcare_cancel_404s_for_an_unknown_job(client_with_db: TestClient) -> None:
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.post("/api/netcare/cancel?job_id=nope", cookies=login_resp.cookies)
    assert resp.status_code == 404
    assert "No running Netcare query job" in resp.json()["error"]


def test_api_netcare_cancel_stops_a_running_job(
    netcare_query_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling settles the job and withholds the image URLs it never wrote."""

    import threading

    from mrtg_cmp.netcare.service import NetcareService

    release = threading.Event()
    seen_cancel: list[object] = []

    def blocking_query(self: NetcareService, **kwargs: object) -> list:
        cancelled = kwargs.get("cancelled")
        while not release.wait(timeout=0.05):
            if cancelled is not None and cancelled():
                seen_cancel.append(True)
                break
        return []

    monkeypatch.setattr(NetcareService, "query", blocking_query)
    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]

    resp = netcare_query_client.post(
        f"/api/netcare/cancel?job_id={job_id}", cookies=login_resp.cookies
    )
    assert resp.status_code == 202
    assert resp.json()["cancelled"] is True

    try:
        final = _await_job(netcare_query_client, login_resp.cookies, job_id)
        assert final["cancelled"] is True
        assert "image_urls" not in final
    finally:
        release.set()

    assert seen_cancel == [True]


def test_api_netcare_cancel_404s_once_a_job_has_settled(
    netcare_query_client: TestClient,
) -> None:
    login_resp = netcare_query_client.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    job_id = netcare_query_client.post(
        "/api/netcare/query", json={"preset": "today"}, cookies=login_resp.cookies
    ).json()["job_id"]
    _await_job(netcare_query_client, login_resp.cookies, job_id)

    resp = netcare_query_client.post(
        f"/api/netcare/cancel?job_id={job_id}", cookies=login_resp.cookies
    )
    assert resp.status_code == 404


def test_api_netcare_targets_scopes_image_urls_to_a_day_partition(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a historical partition points card images at that day."""

    from mrtg_cmp.netcare.scraper import NetcareCache
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    cache_dir = tmp_path / "partitioned"
    cache = NetcareCache(cache_dir)
    day = "2026-09-20"
    for target in DEFAULT_TARGETS:
        _seed_netcare_cache(cache_dir, [target.target])  # writes the live partition
        assert cache.store(target.target, cache.image_path(target.target).read_bytes(), day)
        cache.mark_ok(target.target, day)

    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})

    live = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies).json()
    assert live["day"] is None
    assert "day=" not in live["targets"][0]["image_url"]

    historical = client_with_db.get(
        f"/api/netcare/targets?day={day}", cookies=login_resp.cookies
    ).json()
    assert historical["day"] == day
    assert f"day={day}" in historical["targets"][0]["image_url"]
    assert all(entry["has_image"] for entry in historical["targets"])


def test_api_netcare_graph_serves_a_day_partition_image(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical PNG is served from its partition directory."""

    from mrtg_cmp.netcare.scraper import NetcareCache
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    cache_dir = tmp_path / "partitioned"
    cache = NetcareCache(cache_dir)
    target = DEFAULT_TARGETS[0]
    written = _seed_netcare_cache(cache_dir, [target.target])
    day = "2026-09-20"
    assert cache.store(target.target, written[target.target].read_bytes(), day)

    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client_with_db.get(
        f"/api/netcare/graph/{target.target}.png?day={day}", cookies=login_resp.cookies
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == written[target.target].read_bytes()


def test_api_netcare_graph_404s_for_a_missing_partition(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for a day that was never captured is a clean 404."""

    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    cache_dir = tmp_path / "partitioned"
    target = DEFAULT_TARGETS[0]
    _seed_netcare_cache(cache_dir, [target.target])
    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get(
        f"/api/netcare/graph/{target.target}.png?day=1999-01-01", cookies=login_resp.cookies
    )
    assert resp.status_code == 404


def test_api_netcare_graph_rejects_a_malformed_day(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A junk day parameter cannot create a directory or escape the cache root."""

    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    cache_dir = tmp_path / "partitioned"
    target = DEFAULT_TARGETS[0]
    _seed_netcare_cache(cache_dir, [target.target])
    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    for day in ("../../etc", "not-a-date", "2026-13-45"):
        resp = client_with_db.get(
            f"/api/netcare/graph/{target.target}.png?day={day}", cookies=login_resp.cookies
        )
        assert resp.status_code == 404

    # A rejected day must not create anything on disk.
    assert sorted(p.name for p in cache_dir.iterdir()) == sorted(
        [f"{target.target}.png", "status.json"]
    )



def test_dashboard_renders_the_cached_day_pill(
    client_with_db: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previously captured day is offered as an instant (cache) shortcut."""

    from mrtg_cmp.netcare.scraper import NetcareCache
    from mrtg_cmp.netcare.targets import DEFAULT_TARGETS

    cache_dir = tmp_path / "partitioned"
    cache = NetcareCache(cache_dir)
    target = DEFAULT_TARGETS[0]
    written = _seed_netcare_cache(cache_dir, [target.target])
    assert cache.store(target.target, written[target.target].read_bytes(), "2026-09-20")

    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert 'id="netcare-cached-pill"' in resp.text
    assert "2026-09-20" in resp.text


# ---------------------------------------------------------------------------
# Task 29.0: widescreen container, 2-column Netcare grid, configurable branding
# ---------------------------------------------------------------------------


def _css_rule(css: str, selector: str) -> str:
    """Return the declaration body of the first ``selector`` rule in ``css``.

    The rules are asserted as raw text rather than through a render because a
    page renders identically whether the browser reads the intended rule or a
    stale duplicate of it.
    """
    match = re.search(rf"(?m)^[^{{}}]*{re.escape(selector)}\s*\{{([^{{}}]*)\}}", css)
    assert match, f"{selector!r} rule not found"
    return match.group(1)


def _css_media_block(css: str, query: str) -> str:
    """Return the body of the ``@media (query)`` block, braces balanced."""

    opening = re.search(rf"@media\s*\({re.escape(query)}\)\s*\{{", css)
    assert opening, f"@media ({query}) block not found"
    depth = 1
    index = opening.end()
    while depth and index < len(css):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"@media ({query}) block is not closed"
    return css[opening.end() : index - 1]


def test_container_is_fluid_widescreen() -> None:
    """The shell must not leave 500px of dead margin on a 1080p or 4K display."""

    template = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

    rule = _css_rule(template, ".container")

    assert "max-width: 1720px" in rule
    assert "width: 95%" in rule
    assert "padding: 0 1.5rem" in rule
    # A 95% width is only centred while the auto margins survive.
    assert "margin: 1.5rem auto" in rule
    assert "max-width: 1200px" not in rule


def test_netcare_card_grid_shows_two_graphs_per_row() -> None:
    """Two ~800px cards per row, so MRTG graph text is readable without zooming."""

    template = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    rule = _css_rule(template, ".netcare-card-grid")

    assert "grid-template-columns: repeat(2, 1fr)" in rule
    assert "gap: 1.25rem" in rule
    # The 3-up auto-fill track is what made the cards ~360px and unreadable.
    assert "minmax(300px, 1fr)" not in template


def test_netcare_card_grid_collapses_to_one_column_on_narrow_screens() -> None:
    """Below 992px a 2-column grid would crop the graphs instead of stacking them."""

    template = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    block = _css_media_block(template, "max-width: 992px")

    assert "grid-template-columns: 1fr" in _css_rule(block, ".netcare-card-grid")


def test_netcare_card_graphs_stretch_to_the_wide_card() -> None:
    """The image has to fill its track, and nothing may cap the card itself."""

    template = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    image_rule = _css_rule(template, ".netcare-card-image")
    assert "width: 100%" in image_rule
    assert "height: auto" in image_rule

    card_rule = _css_rule(template, ".netcare-card")
    assert "max-width" not in card_rule
    assert "width" not in card_rule


def test_navbar_brand_pairs_app_title_with_site_name(
    client_with_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header reads both values off the settings, so branding is a config choice.

    The names are arbitrary here on purpose: the public repository ships the
    neutral ``Enterprise Gateway`` default, and a deployment overrides it in its
    own ``.env``. Asserting one literal would pin the public template to a
    customer's branding, so the test pins the wiring instead.
    """

    monkeypatch.setattr(settings, "app_title", "MRTG Traffic Monitor")
    monkeypatch.setattr(settings, "site_name", "Example Site Name")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    brand = re.search(r'<div class="brand">(.*?)</div>', resp.text, re.DOTALL)
    assert brand, "the header no longer renders a brand block"
    assert "MRTG Traffic Monitor" in brand.group(1)
    assert "Example Site Name" in brand.group(1)


# ---------------------------------------------------------------------------
# Task 30.0: service badges/filter bar, equal-height cards, cache auto-sync
# ---------------------------------------------------------------------------


def _dashboard_template() -> str:
    return (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")


def test_netcare_cards_are_equal_height_flex_columns() -> None:
    """Adjacent cards must share a row height so their footers sit on one line."""

    card_rule = _css_rule(_dashboard_template(), ".netcare-card")

    assert "display: flex" in card_rule
    assert "flex-direction: column" in card_rule
    assert "height: 100%" in card_rule


def test_netcare_card_head_reserves_a_uniform_two_line_block() -> None:
    """A one-line and a two-line branch name would otherwise offset the graph."""

    head_rule = _css_rule(_dashboard_template(), ".netcare-card-head")

    assert "min-height: 48px" in head_rule


def test_netcare_card_footer_is_pushed_to_the_card_bottom() -> None:
    """`margin-top: auto` is what puts every footer on the same baseline."""

    footer_rule = _css_rule(_dashboard_template(), ".netcare-card-footer")

    assert "margin-top: auto" in footer_rule
    assert "padding-top: 0.75rem" in footer_rule
    assert "border-top: 1px solid var(--border-color)" in footer_rule
    assert "margin-top: 0.5rem" not in footer_rule


def test_service_badges_are_wired_into_the_card_header() -> None:
    template = _dashboard_template()

    assert 'class="netcare-service-badge {{ card.service_badge_class }}"' in template
    assert "{{ card.service_label }}" in template
    # The badge sits beside the status pill, not replacing it.
    head = re.search(
        r'<div class="netcare-card-head">(.*?)</div>\s*<div class="netcare-card-meta">',
        template,
        re.DOTALL,
    )
    assert head, "the card header no longer matches the expected structure"
    assert "netcare-service-badge" in head.group(1)
    assert "status-badge" in head.group(1)


def test_each_service_gets_its_own_colour() -> None:
    """Three products share a row, so the hues have to be told apart at a glance."""

    template = _dashboard_template()

    hues = set()
    for modifier in (".svc-astinet", ".svc-metro-e", ".svc-vpn-ip"):
        rule = _css_rule(template, modifier)
        assert "--svc-color" in rule, f"{modifier} defines no service colour"
        hues.add(re.search(r"--svc-color:\s*([^;]+);", rule).group(1).strip())

    assert len(hues) == 3
    # The hue is declared once per service and consumed via the custom property,
    # so a legend dot and a card badge can never drift apart.
    assert "background: var(--svc-color" in template


def test_service_filter_bar_lists_every_service_with_its_legend() -> None:
    template = _dashboard_template()

    assert 'id="netcare-service-filter"' in template
    assert 'aria-label="Filter branches by service type"' in template
    assert 'data-service="ALL"' in template
    assert "Semua Layanan ({{ netcare_total }})" in template
    assert "{% for service in netcare_services %}" in template
    assert "{{ service.label }} ({{ service.description }} - {{ service.count }})" in template
    assert 'class="netcare-legend-dot {{ service.badge_class }}"' in template


def test_cards_expose_their_service_type_for_filtering() -> None:
    template = _dashboard_template()

    card = re.search(r'<div class="netcare-card"\s(.*?)>', template, re.DOTALL)
    assert card, "the card markup no longer matches the expected structure"
    assert 'data-service="{{ card.service_type }}"' in card.group(1)


def test_region_and_service_filters_combine_rather_than_replace_each_other() -> None:
    """Both axes must narrow the grid; picking one has to leave the other standing."""

    script = _dashboard_template()

    assert "let netcareRegionFilter = 'ALL';" in script
    assert "let netcareServiceFilter = 'ALL';" in script
    assert "function applyNetcareFilters()" in script
    match = re.search(
        r"const regionMatch = [^;]*;.*?const serviceMatch = [^;]*;.*?"
        r"const match = regionMatch && serviceMatch;",
        script,
        re.DOTALL,
    )
    assert match, "the filter no longer intersects the region and service axes"
    assert "card.dataset.service" in script
    # Both pill rows drive the one filter, otherwise a click on a service pill
    # would only work if it happened to be wired up too.
    assert "document.querySelectorAll('#netcare-filter-pills .netcare-pill')" in script
    assert "document.querySelectorAll('#netcare-service-filter .netcare-pill')" in script


def test_empty_message_no_longer_blames_the_region_alone(client_with_db: TestClient) -> None:
    """The empty state is reachable from the service filter too."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert "No branch links match this filter." in resp.text


def test_dashboard_renders_the_service_legend_with_real_counts(
    client_with_db: TestClient,
) -> None:
    """The legend is only useful if it is rendered from the catalog, not hardcoded."""

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert "Semua Layanan (18)" in resp.text
    assert "Astinet (Internet Dedicated - 6)" in resp.text
    assert "Metro-E (Ethernet L2 - 5)" in resp.text
    assert "VPN IP (Intranet - 7)" in resp.text


def test_dashboard_renders_a_badge_per_service_class(
    client_with_db: TestClient,
) -> None:
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert resp.text.count("netcare-service-badge svc-astinet") == 6
    assert resp.text.count("netcare-service-badge svc-metro-e") == 5
    assert resp.text.count("netcare-service-badge svc-vpn-ip") == 7


def test_api_netcare_targets_exposes_service_metadata(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "netcare_cache_dir", tmp_path / "empty-netcare")

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/api/netcare/targets", cookies=login_resp.cookies)

    assert resp.status_code == 200
    payload = resp.json()
    first = payload["targets"][0]
    assert first["service_type"] == "Astinet"
    assert first["service_label"] == "Astinet"
    assert first["service_description"] == "Internet Dedicated"
    assert first["service_badge_class"] == "svc-astinet"
    assert {group["code"]: group["count"] for group in payload["services"]} == {
        "Astinet": 6,
        "Metro-E": 5,
        "VPN IP": 7,
    }
    assert all(group["badge_class"] for group in payload["services"])


def test_dashboard_default_view_promotes_the_newest_partition(
    client_with_db: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default view reads the flat cache, so the fresh partition must be lifted."""

    from mrtg_cmp.netcare.scraper import NetcareCache

    cache_dir = tmp_path / "netcare"
    cache = NetcareCache(cache_dir)
    target = _resolved_targets()[0]
    payload = _seed_netcare_cache(cache_dir / "2026-09-29", [target.target])[
        target.target
    ].read_bytes()
    assert cache.store(target.target, payload, "2026-09-29") is True
    cache.mark_ok(target.target, "2026-09-29")
    monkeypatch.setattr(settings, "netcare_cache_dir", cache_dir)

    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/", cookies=login_resp.cookies)

    assert resp.status_code == 200
    assert cache.has_image(target.target) is True
    assert cache.image_path(target.target).read_bytes() == payload
    assert cache.read_manifest()[target.target]["status"] == "ok"


# ---------------------------------------------------------------------------
# Task 31.0: honest stopwatch loading modal (no speculative countdown)
# ---------------------------------------------------------------------------


def _progress_dialog_html(template: str) -> str:
    """Return the loading dialog markup only."""

    start = template.index('<div id="netcare-progress-backdrop"')
    end = template.index("<!-- Section 2: Live MikroTik WAN Traffic -->")
    return template[start:end]


def test_progress_dialog_markup_has_no_eta_or_remaining_elements() -> None:
    """The dialog markup is the surface the operator reads; it must be clean."""

    dialog = _progress_dialog_html(_dashboard_template())

    assert 'id="netcare-progress-eta"' not in dialog
    assert 'id="netcare-progress-remaining"' not in dialog
    assert "Estimasi selesai" not in dialog
    assert "Sisa:" not in dialog
    assert "netcare_estimated_seconds" not in dialog
    # The one remaining timer is the factual elapsed stopwatch.
    assert 'id="netcare-progress-elapsed"' in dialog
    assert "Waktu berjalan:" in dialog
    assert dialog.count('class="netcare-timer-value"') == 1


def test_elapsed_stopwatch_is_centered_not_pushed_to_the_edges() -> None:
    """A single timer in a ``space-between`` row renders hard left, not centered.

    ``space-between`` was correct while the row held two spans; with only the
    stopwatch left it would have left-aligned the value, so the rule is pinned
    as raw CSS for the same reason the Task 29/30 layout rules are.
    """

    timers_rule = _css_rule(_dashboard_template(), ".netcare-timers")

    assert "justify-content: center" in timers_rule
    assert "space-between" not in timers_rule


def test_progress_script_keeps_no_eta_or_countdown_state() -> None:
    """The removed countdown must be gone from the script, not only the markup.

    A dead formatter or a leftover ``netcareEtaSeconds`` would keep the
    speculative arithmetic alive in the file even with nothing rendering it.
    """

    template = _dashboard_template()

    for removed in (
        "netcareProgressEta",
        "netcareProgressRemaining",
        "formatEta",
        "formatRemaining",
        "netcareEtaSeconds",
        "NETCARE_ESTIMATE_SECONDS",
    ):
        assert removed not in template
    # The stopwatch survives, and one ticker still drives it.
    assert "setNetcareElapsed" in template
    assert "netcareElapsed += 1" in template
    # No estimate is read off either API response any more.
    assert "data.eta_seconds" not in template
    assert "data.estimated_seconds" not in template


def test_progress_helpers_are_called_with_the_new_signatures() -> None:
    """`setNetcareProgress` dropped its eta/label args; callers must match.

    A stale four-argument call would still run, so the arity is pinned at each
    call site rather than only on the definition.
    """

    template = _dashboard_template()

    assert "function setNetcareProgress(completed, total) {" in template
    assert "function openNetcareProgress() {" in template
    for call in (
        "setNetcareProgress(0, NETCARE_TOTAL);",
        "setNetcareProgress(state === 'done' ? NETCARE_TOTAL : 0, NETCARE_TOTAL);",
        "setNetcareProgress(data.completed, data.total);",
        "setNetcareProgress(data.completed || 0, data.total || NETCARE_TOTAL);",
        "openNetcareProgress();",
    ):
        assert call in template
