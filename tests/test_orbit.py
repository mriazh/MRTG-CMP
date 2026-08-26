"""Unit and integration tests for Telkomsel Orbit monitoring engine."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from mrtg_cmp.orbit.cache import OrbitCache
from mrtg_cmp.orbit.scraper import (
    OrbitModemStatus,
    OrbitPackage,
    OrbitScraper,
    parse_expiry_date,
    parse_package_cards_html,
    parse_quota_string,
)
from mrtg_cmp.orbit.targets import (
    OrbitModem,
    _default_orbit_catalog,
    filter_stats,
    load_orbit_catalog_csv,
    load_orbit_catalog_excel,
    resolve_orbit_catalog,
)


def test_orbit_modem_model_and_imei_validation() -> None:
    """15-digit numeric IMEIs are valid; 10-digit IDs or strings are invalid."""
    valid_modem = OrbitModem(
        no=1,
        imei="860000000000001",
        phone="081200000001",
        location="Room 101",
        ssid="Orbit-Test-01",
        status="ACTIVE",
    )
    assert valid_modem.imei_valid is True
    assert valid_modem.as_dict()["imei_valid"] is True

    # 10-digit ID
    invalid_10digit = OrbitModem(
        no=2,
        imei="1701669787",
        phone="081200000002",
        location="Room 102",
        ssid="Orbit-Test-02",
    )
    assert invalid_10digit.imei_valid is False

    # Non-digit or wrong length
    invalid_chars = OrbitModem(
        no=3,
        imei="86000000000000A",
        phone="081200000003",
        location="Room 103",
        ssid="Orbit-Test-03",
    )
    assert invalid_chars.imei_valid is False


def test_default_catalog_structure_and_counts() -> None:
    """Fallback catalog provides 12 anonymous modems with exact target profile."""
    modems = _default_orbit_catalog()
    assert len(modems) == 12

    stats = filter_stats(modems)
    assert stats["all"] == 12
    assert stats["active"] == 5
    assert stats["idle"] == 6
    assert stats["ex_customer"] == 1
    assert stats["imei_pending"] == 2

    # Rows 1 & 3 are 10-digit IDs
    assert modems[0].imei_valid is False
    assert len(modems[0].imei) == 10
    assert modems[2].imei_valid is False
    assert len(modems[2].imei) == 10

    # Other rows have 15-digit IMEIs
    for idx in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert modems[idx].imei_valid is True
        assert len(modems[idx].imei) == 15


def test_load_orbit_catalog_csv(tmp_path: Path) -> None:
    """CSV loader parses headers and normalizes status."""
    csv_file = tmp_path / "test_targets.csv"
    csv_content = """no,imei,phone,location,ssid,status
1,1700000001,081200000001,Meeting Room 1,SSID-01,ACTIVE
2,860000000000002,081200000002,IDLE ,SSID-02,IDLE
3,860000000000003,081200000003,EX Modem Customer,SSID-03,EX_CUSTOMER
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    modems = load_orbit_catalog_csv(csv_file)
    assert len(modems) == 3
    assert modems[0].status == "ACTIVE"
    assert modems[0].imei_valid is False
    assert modems[1].status == "IDLE"
    assert modems[1].imei_valid is True
    assert modems[2].status == "EX_CUSTOMER"
    assert modems[2].imei_valid is True


def test_load_orbit_catalog_excel_if_present() -> None:
    """If private orbit excel is present, load and verify 12 rows."""
    excel_path = Path("config") / f"orbit_{''.join(('gm', 'f'))}.xlsx"
    if not excel_path.is_file():
        return

    modems = load_orbit_catalog_excel(excel_path)
    assert len(modems) == 12

    stats = filter_stats(modems)
    assert stats["all"] == 12
    assert stats["active"] == 5
    assert stats["idle"] == 6
    assert stats["ex_customer"] == 1
    assert stats["imei_pending"] == 2

    # Row 1 and 3 are 10-digit IDs
    assert modems[0].imei_valid is False
    assert len(modems[0].imei) == 10
    assert modems[2].imei_valid is False
    assert len(modems[2].imei) == 10

    # Row 2, 4-10, 11, 12 are valid 15-digit IMEIs
    for i in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert modems[i].imei_valid is True
        assert len(modems[i].imei) == 15


def test_resolve_orbit_catalog_fallback(tmp_path: Path) -> None:
    """Non-existent file falls back to default anonymous catalog."""
    modems = resolve_orbit_catalog(tmp_path / "non_existent.csv")
    assert len(modems) == 12
    assert modems[0].phone.startswith("0812")


def test_parse_quota_string() -> None:
    """Parses various quota text combinations."""
    rem, tot = parse_quota_string("28.56GB / 30GB")
    assert rem == 28.56
    assert tot == 30.0

    rem, tot = parse_quota_string("14.2 GB / 50 GB")
    assert rem == 14.2
    assert tot == 50.0

    # Comma decimal
    rem, tot = parse_quota_string("28,50GB / 30,00GB")
    assert rem == 28.5
    assert tot == 30.0

    # MB to GB conversion
    rem, tot = parse_quota_string("512 MB / 10 GB")
    assert rem == 0.5
    assert tot == 10.0

    # Empty or unparseable
    assert parse_quota_string("") == (0.0, 0.0)
    assert parse_quota_string("No Quota Available") == (0.0, 0.0)


def test_parse_expiry_date() -> None:
    """Parses Indonesian and English month strings and computes days left."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)

    # DD Mon YYYY
    date_str, days_left = parse_expiry_date("Berlaku s.d 08 Oct 2026", now=ref_now)
    assert date_str == "08 Oct 2026"
    assert days_left == 7

    # Indonesian month 'Okt'
    date_str, days_left = parse_expiry_date("Berlaku s.d. 08 Okt 2026", now=ref_now)
    assert date_str == "08 Oct 2026"
    assert days_left == 7

    # Indonesian month 'Des'
    date_str, days_left = parse_expiry_date("Berlaku s/d 25 Des 2026", now=ref_now)
    assert date_str == "25 Dec 2026"
    assert days_left == 85

    # Slash format DD/MM/YYYY
    date_str, days_left = parse_expiry_date("08/10/2026", now=ref_now)
    assert date_str == "08 Oct 2026"
    assert days_left == 7

    # ISO format YYYY-MM-DD
    date_str, days_left = parse_expiry_date("2026-10-08", now=ref_now)
    assert date_str == "08 Oct 2026"
    assert days_left == 7

    # Invalid string
    date_str, days_left = parse_expiry_date("Unlimited")
    assert date_str == "Unlimited"
    assert days_left is None


def test_parse_package_cards_html() -> None:
    """Parses stacked packages markup from HTML."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    mock_html = """
    <div class="package-card">
        <h4 class="title">Internet Orbit 30GB</h4>
        <div class="quota-val">25.0 GB / 25.0 GB</div>
        <span class="expiry">Berlaku s.d 08 Oct 2026</span>
    </div>
    <div class="package-card">
        <h4 class="title">Kuota FantaSIX</h4>
        <div class="quota-val">3.56 GB / 5.0 GB</div>
        <span class="expiry">Berlaku s.d 22 Oct 2026</span>
    </div>
    """
    packages = parse_package_cards_html(mock_html, now=ref_now)
    assert len(packages) == 2

    p1 = packages[0]
    assert p1.name == "Internet Orbit 30GB"
    assert p1.remaining_gb == 25.0
    assert p1.total_gb == 25.0
    assert p1.expiry_str == "08 Oct 2026"
    assert p1.days_left == 7

    p2 = packages[1]
    assert p2.name == "Kuota FantaSIX"
    assert p2.remaining_gb == 3.56
    assert p2.total_gb == 5.0
    assert p2.expiry_str == "22 Oct 2026"
    assert p2.days_left == 21


def test_orbit_scraper_invalid_imei_and_ex_customer_skips_portal() -> None:
    """Modems with invalid IMEI or EX_CUSTOMER status resolve without driver calls."""
    scraper = OrbitScraper()

    # 10-digit IMEI modem
    modem_invalid = OrbitModem(1, "1701669787", "081200000001", "Room A", "Orbit-A")
    status_invalid = scraper.scrape_modem(modem_invalid)
    assert status_invalid.error == "IMEI_PENDING"
    assert status_invalid.total_quota_gb == 0.0
    assert status_invalid.packages == []

    # EX_CUSTOMER modem
    modem_ex = OrbitModem(
        10, "860000000000010", "081200000010", "Old Site", "Orbit-Old", status="EX_CUSTOMER"
    )
    status_ex = scraper.scrape_modem(modem_ex)
    assert status_ex.error == "EX_CUSTOMER"
    assert status_ex.total_quota_gb == 0.0


def test_orbit_scraper_with_mock_driver() -> None:
    """Driver automation steps extract quota and multi-package breakdown."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    scraper = OrbitScraper()
    modem = OrbitModem(2, "860000000000002", "081200000002", "Room B", "Orbit-B")

    mock_driver = MagicMock()
    mock_driver.page_source = """
    <div>
        <h2>Info Kuota</h2>
        <div class="quota-total">28.56 GB / 30 GB</div>
        <div class="package-item">
            <span class="name">Internet Orbit 30GB</span>
            <span>28.56 GB / 30 GB</span>
            <span>Berlaku s.d 08 Oct 2026</span>
        </div>
    </div>
    """

    status = scraper.scrape_modem(modem, driver=mock_driver, now=ref_now)
    assert status.error is None
    assert status.total_remaining_gb == 28.56
    assert status.total_quota_gb == 30.0
    assert status.earliest_expiry_str == "08 Oct 2026"
    assert status.earliest_days_left == 7
    assert len(status.packages) >= 1
    assert "Internet Orbit" in status.packages[0].name


def test_orbit_cache_atomic_read_write(tmp_path: Path) -> None:
    """OrbitCache persists data atomically and recovers it cleanly."""
    cache_file = tmp_path / "orbit_cache" / "modems.json"
    cache = OrbitCache(cache_file)
    assert not cache.is_cached()
    assert cache.load() is None

    modem = OrbitModem(1, "860000000000001", "081200000001", "Room A", "SSID-A")
    status_list = [
        OrbitModemStatus(
            target=modem,
            total_remaining_gb=25.0,
            total_quota_gb=30.0,
            multimedia_active=True,
            packages=[OrbitPackage("Orbit Internet", 25.0, 30.0, "15 Oct 2026", 14)],
            earliest_expiry_str="15 Oct 2026",
            earliest_days_left=14,
            last_scraped_at="2026-10-01 10:00:00",
        )
    ]

    cache.save(status_list)
    assert cache.is_cached()

    loaded = cache.load()
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].target.no == 1
    assert loaded[0].total_remaining_gb == 25.0
    assert loaded[0].packages[0].name == "Orbit Internet"


def test_orbit_cache_get_or_seed(tmp_path: Path) -> None:
    """get_or_seed populates initial demo data on fresh installs."""
    cache_file = tmp_path / "orbit_cache" / "modems.json"
    cache = OrbitCache(cache_file)
    catalog = _default_orbit_catalog()

    seeded = cache.get_or_seed(catalog)
    assert len(seeded) == 12
    assert cache.is_cached()

    # Second call reads from disk
    cached = cache.load()
    assert cached is not None
    assert len(cached) == 12


def test_api_orbit_modems_endpoint(client_with_db: TestClient) -> None:
    """GET /api/orbit/modems returns serialized modems, filter stats, and summary."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    assert login_resp.status_code in (200, 302, 303)

    resp = client_with_db.get("/api/orbit/modems", cookies=login_resp.cookies)
    assert resp.status_code == 200
    data = resp.json()

    assert "modems" in data
    assert "stats" in data
    assert "summary" in data

    modems = data["modems"]
    assert len(modems) == 12
    assert data["stats"]["all"] == 12
    assert data["stats"]["active"] == 5
    assert data["stats"]["idle"] == 6
    assert data["stats"]["ex_customer"] == 1
    assert data["stats"]["imei_pending"] == 2

    summary = data["summary"]
    assert summary["total_modems"] == 12
    assert summary["active_modems"] == 5
    assert summary["total_remaining_gb"] > 0
    assert summary["expiring_soon_count"] >= 1


def test_api_orbit_sync_endpoint(client_with_db: TestClient) -> None:
    """POST /api/orbit/sync triggers sync and returns 202 Accepted."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    assert login_resp.status_code in (200, 302, 303)

    resp = client_with_db.post("/api/orbit/sync", cookies=login_resp.cookies)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert "sync triggered" in data["message"].lower()


def test_orbit_dashboard_html_view(client_with_db: TestClient) -> None:
    """GET /orbit renders all filter pills, cards, modal elements, and buttons."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/orbit", cookies=login_resp.cookies)

    assert resp.status_code == 200
    text = resp.text

    assert "Telkomsel Orbit Modem Monitoring" in text
    assert "Semua Modem (12)" in text
    assert "Aktif di Ruangan (5)" in text
    assert "Cadangan / IDLE (6)" in text
    assert "Bekas (1)" in text
    assert "Sync Orbit Now" in text
    assert "Lihat Rincian Paket" in text
    assert "orbit-package-modal" in text or "orbit-modal" in text
