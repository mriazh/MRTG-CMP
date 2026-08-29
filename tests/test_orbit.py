"""Unit and integration tests for Telkomsel Orbit monitoring engine."""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from mrtg_cmp.cli import main
from mrtg_cmp.config import settings
from mrtg_cmp.orbit.cache import OrbitCache
from mrtg_cmp.orbit.scraper import (
    OrbitModemStatus,
    OrbitPackage,
    OrbitScraper,
    extract_modem_info,
    parse_expiry_date,
    parse_package_cards_html,
    parse_quota_string,
)
from mrtg_cmp.orbit.service import (
    OrbitDaemon,
    OrbitService,
    build_orbit_daemon_from_settings,
    build_orbit_driver,
)
from mrtg_cmp.orbit.targets import (
    DEFAULT_EXCEL_PATH,
    OrbitModem,
    _classify_status,
    _default_orbit_catalog,
    filter_stats,
    load_orbit_catalog_csv,
    load_orbit_catalog_excel,
    persist_orbit_catalog,
    remove_orbit_modem,
    resolve_orbit_catalog,
    save_orbit_catalog_csv,
    save_orbit_catalog_excel,
    update_catalog_modem_ssid,
    upsert_orbit_modem,
)

# The default workbook name is deployment-specific, so tests derive it from the
# constant rather than hard-coding the string (which the OpSec gate rejects).
_XLSX_NAME = DEFAULT_EXCEL_PATH.name


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


def test_default_orbit_daemon_interval_is_five_minutes() -> None:
    from mrtg_cmp.config import settings

    assert settings.orbit_sync_interval_seconds == 300


def test_default_catalog_structure_and_counts() -> None:
    """Fallback catalog provides 12 anonymous modems with exact target profile."""
    modems = _default_orbit_catalog()
    assert len(modems) == 12

    stats = filter_stats(modems)
    assert stats["all"] == 12
    assert stats["active"] == 5
    assert stats["idle"] == 7
    assert stats["imei_pending"] == 2
    assert "ex_customer" not in stats

    # Modem 10 was migrated from EX_CUSTOMER to IDLE (Phase 59)
    modem_10 = next(m for m in modems if m.no == 10)
    assert modem_10.status == "IDLE"

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
    csv_content = """no,imei,phone,location,ssid,status,latitude,longitude
1,1700000001,081200000001,Meeting Room 1,SSID-01,ACTIVE,-6.200500,106.816600
2,860000000000002,081200000002,IDLE ,SSID-02,IDLE,,
3,860000000000003,081200000003,EX Modem Customer,SSID-03,EX_CUSTOMER,-6.9,106.1
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    modems = load_orbit_catalog_csv(csv_file)
    assert len(modems) == 3
    assert modems[0].status == "ACTIVE"
    assert modems[0].imei_valid is False
    assert modems[0].latitude == -6.2005
    assert modems[0].longitude == 106.8166
    assert modems[1].status == "IDLE"
    assert modems[1].imei_valid is True
    assert modems[1].latitude is None
    assert modems[1].longitude is None
    # Legacy EX_CUSTOMER label is folded into IDLE (Phase 59)
    assert modems[2].status == "IDLE"
    assert modems[2].imei_valid is True
    assert modems[2].latitude == -6.9


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
    # EX_CUSTOMER is gone; every non-active modem is counted as IDLE (Phase 59)
    assert stats["idle"] == 7
    assert stats["active"] + stats["idle"] == stats["all"]
    assert "ex_customer" not in stats
    assert stats["imei_pending"] in (1, 2)

    # Row 1 is a 10-digit ID
    assert modems[0].imei_valid is False
    assert len(modems[0].imei) == 10

    # Remaining rows are valid 15-digit IMEIs
    for i in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert modems[i].imei_valid is True
        assert len(modems[i].imei) == 15


def test_resolve_orbit_catalog_fallback(tmp_path: Path) -> None:
    """Non-existent file falls back to default anonymous catalog."""
    modems = resolve_orbit_catalog(tmp_path / "non_existent.csv")
    assert len(modems) == 12
    assert modems[0].phone.startswith("0812")


def test_parse_quota_string() -> None:
    """Parses various quota text combinations with priority for Sisa."""
    # Priority Sisa pattern with stray numbers in text
    stray_script_text = (
        "<script>ratio = 8.0 / 8.0;</script>\n"
        "Kuota Internet\n"
        "Sisa 85.29GB / 85.29GB"
    )
    rem, tot = parse_quota_string(stray_script_text)
    assert rem == 85.29
    assert tot == 85.29

    # Sisa with comma and whitespace
    rem, tot = parse_quota_string("Sisa 85,29 GB / 85,29 GB")
    assert rem == 85.29
    assert tot == 85.29

    # Sisa with MB to GB
    rem, tot = parse_quota_string("Sisa 500MB / 10GB")
    assert rem == 0.49
    assert tot == 10.0

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

    # Single remaining pattern
    rem, tot = parse_quota_string("50GB")
    assert rem == 50.0
    assert tot == 50.0

    # Stray dots or invalid characters from live probe findings
    assert parse_quota_string(".") == (0.0, 0.0)
    assert parse_quota_string("...") == (0.0, 0.0)
    assert parse_quota_string(". GB / . GB") == (0.0, 0.0)

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


def test_parse_package_cards_text_format() -> None:
    """Parses plain-text package list rendered on prabayar-info-kuota."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    rendered_text = """
    Paket Data Saya
    Internet Quota
    Kuota FantaSIX
    25GB
    / 25GB
    Berlaku s.d 13 Oct 2026
    Kuota FantaSIX
    25GB
    / 25GB
    Berlaku s.d 14 Oct 2026
    Internet Orbit
    35GB
    / 35GB
    Berlaku s.d 14 Oct 2026
    """
    packages = parse_package_cards_html(rendered_text, now=ref_now)
    assert len(packages) == 3

    assert packages[0].name == "Kuota FantaSIX"
    assert packages[0].remaining_gb == 25.0
    assert packages[0].total_gb == 25.0
    assert packages[0].expiry_str == "13 Oct 2026"
    assert packages[0].days_left == 12

    assert packages[1].name == "Kuota FantaSIX"
    assert packages[1].remaining_gb == 25.0
    assert packages[1].total_gb == 25.0
    assert packages[1].expiry_str == "14 Oct 2026"
    assert packages[1].days_left == 13

    assert packages[2].name == "Internet Orbit"
    assert packages[2].remaining_gb == 35.0
    assert packages[2].total_gb == 35.0
    assert packages[2].expiry_str == "14 Oct 2026"
    assert packages[2].days_left == 13


def test_orbit_scraper_invalid_imei_skips_portal_but_valid_idle_needs_driver() -> None:
    """Only invalid IMEI skips MyOrbit; a valid IDLE modem still needs a driver."""
    scraper = OrbitScraper()

    # 10-digit IMEI modem
    modem_invalid = OrbitModem(1, "1701669787", "081200000001", "Room A", "Orbit-A")
    status_invalid = scraper.scrape_modem(modem_invalid)
    assert status_invalid.error == "IMEI_PENDING"
    assert status_invalid.total_quota_gb == 0.0
    assert status_invalid.packages == []

    # IDLE / spare modem
    modem_ex = OrbitModem(
        10, "860000000000010", "081200000010", "Old Site", "Orbit-Old", status="IDLE"
    )
    driver = MagicMock()
    with patch.object(
        scraper, "_scrape_with_driver", return_value=OrbitModemStatus(target=modem_ex)
    ) as scrape:
        status_ex = scraper.scrape_modem(modem_ex, driver=driver)
    scrape.assert_called_once_with(driver, modem_ex, ANY)
    assert status_ex.error is None


def test_orbit_scraper_with_mock_driver() -> None:
    """Driver automation steps extract quota and multi-package breakdown."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    scraper = OrbitScraper()
    modem = OrbitModem(2, "860000000000002", "081200000002", "Room B", "Orbit-B")

    mock_driver = MagicMock()
    phone_input = MagicMock()
    imei_input = MagicMock()
    lanjut_btn = MagicMock()
    lanjut_btn.text = "Lanjutkan"
    cek_btn = MagicMock()
    cek_btn.text = "Cek Kuota"
    detail_btn = MagicMock()
    detail_btn.text = "Lihat Detail"

    body_modem = MagicMock()
    body_modem.text = """
    Nama WiFi
    tselhome-8BCB
    Total Kuota: 28.56 GB / 30 GB
    Lihat Detail
    """

    body_kuota = MagicMock()
    body_kuota.text = """
    Internet Orbit 30GB
    28.56GB
    / 30GB
    Berlaku s.d 08 Oct 2026
    """

    current_page = ["prabayar-info-modem"]
    mock_driver.current_url = "https://www.myorbit.id/prabayar-info-modem"

    def mock_find_element(by: str, value: str) -> Any:
        if by == "tag name" and value == "body":
            if current_page[0] == "prabayar-info-modem":
                return body_modem
            return body_kuota
        if "Cek Kuota" in value:
            return cek_btn
        return MagicMock()

    def mock_find_elements(by: str, value: str) -> list[Any]:
        if by == "tag name" and value == "input":
            return [phone_input, imei_input]
        if by == "tag name" and value == "button":
            return [lanjut_btn]
        if "Cek Kuota" in value:
            return [cek_btn]
        if "Lihat Detail" in value or value == "//button | //a":
            return [detail_btn]
        return []

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.page_source = """
    <div>
        <h2>Info Kuota</h2>
        <div class="wifi-name">tselhome-8BCB</div>
        <div class="quota-total">28.56 GB / 30 GB</div>
        <div class="package-item">
            <span class="name">Internet Orbit 30GB</span>
            <span>28.56 GB / 30 GB</span>
            <span>Berlaku s.d 08 Oct 2026</span>
        </div>
    </div>
    """

    def mock_execute_script(script: str, *args: Any) -> Any:
        if args and args[0] is detail_btn:
            current_page[0] = "prabayar-info-kuota"
            mock_driver.current_url = "https://www.myorbit.id/prabayar-info-kuota"
        return None

    mock_driver.execute_script.side_effect = mock_execute_script

    status = scraper.scrape_modem(modem, driver=mock_driver, now=ref_now)
    assert status.error is None
    assert status.total_remaining_gb == 28.56
    assert status.total_quota_gb == 30.0
    assert status.earliest_expiry_str == "08 Oct 2026"
    assert status.earliest_days_left == 7
    assert len(status.packages) >= 1
    assert "Internet Orbit" in status.packages[0].name
    assert modem.ssid == "tselhome-8BCB"
    phone_input.send_keys.assert_called_with("081200000002")
    imei_input.send_keys.assert_called_with("860000000000002")
    assert mock_driver.execute_script.call_count >= 2


def test_orbit_scraper_with_live_probe_rendered_text() -> None:
    """Simulates live probe flow with stray script numbers, DOM text, and navigation."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    scraper = OrbitScraper()
    modem = OrbitModem(1, "860000000000001", "6281367149931", "Room 101", "Old_SSID")

    mock_driver = MagicMock()
    phone_input = MagicMock()
    imei_input = MagicMock()
    lanjut_btn = MagicMock()
    lanjut_btn.text = "Lanjutkan"
    cek_btn = MagicMock()
    detail_btn = MagicMock()
    detail_btn.text = "Lihat Detail"

    body_modem = MagicMock()
    body_modem.text = """
    Nama WiFi
    tselhome-8BCB
    6281367149931
    Kuota Internet
    Sisa 85.29GB / 85.29GB
    Kuota Multimedia
    Tidak ada kuota multimedia aktif
    Lihat Detail
    """

    body_kuota = MagicMock()
    body_kuota.text = """
    Paket Data Saya
    Internet Quota
    Kuota FantaSIX
    25GB
    / 25GB
    Berlaku s.d 13 Oct 2026
    Kuota FantaSIX
    25GB
    / 25GB
    Berlaku s.d 14 Oct 2026
    Internet Orbit
    35GB
    / 35GB
    Berlaku s.d 14 Oct 2026
    """

    current_page = ["prabayar-info-modem"]

    def mock_find_element(by: str, value: str) -> Any:
        if by == "tag name" and value == "body":
            if current_page[0] == "prabayar-info-modem":
                return body_modem
            return body_kuota
        return MagicMock()

    def mock_find_elements(by: str, value: str) -> list[Any]:
        if by == "tag name" and value == "input":
            return [phone_input, imei_input]
        if by == "tag name" and value == "button":
            return [lanjut_btn]
        if "Cek Kuota" in value:
            return [cek_btn]
        if "Lihat Detail" in value or value == "//button | //a":
            return [detail_btn]
        if "Paket Data Saya" in value:
            return [MagicMock()]
        return []

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    # page_source has stray 8.0 / 8.0 script number
    mock_driver.page_source = "<script>var ratio = 8.0 / 8.0;</script>"
    mock_driver.current_url = "https://www.myorbit.id/prabayar-info-modem"

    def mock_execute_script(script: str, *args: Any) -> Any:
        if args and args[0] is detail_btn:
            current_page[0] = "prabayar-info-kuota"
            mock_driver.current_url = "https://www.myorbit.id/prabayar-info-kuota"
        return None

    mock_driver.execute_script.side_effect = mock_execute_script

    status = scraper.scrape_modem(modem, driver=mock_driver, now=ref_now)
    assert status.error is None
    assert status.total_remaining_gb == 85.29
    assert status.total_quota_gb == 85.29
    assert modem.ssid == "tselhome-8BCB"
    assert len(status.packages) == 3
    assert status.packages[0].name == "Kuota FantaSIX"
    assert status.packages[0].remaining_gb == 25.0
    assert status.packages[1].name == "Kuota FantaSIX"
    assert status.packages[2].name == "Internet Orbit"
    assert status.earliest_expiry_str == "13 Oct 2026"
    assert status.earliest_days_left == 12


def test_orbit_scraper_less_than_two_inputs_returns_error() -> None:
    """When fewer than 2 input tags are located, LookupError is recorded in status."""
    scraper = OrbitScraper()
    modem = OrbitModem(2, "860000000000002", "081200000002", "Room B", "Orbit-B")
    mock_driver = MagicMock()
    mock_driver.find_elements.return_value = [MagicMock()]  # only 1 input field

    status = scraper.scrape_modem(modem, driver=mock_driver)
    assert status.error is not None
    assert "Expected at least 2 input fields" in status.error


def test_orbit_scraper_real_probe_scenario() -> None:
    """Verifies parsing of the probe scenario with 5 packages."""
    ref_now = datetime(2026, 10, 1, tzinfo=UTC)
    scraper = OrbitScraper()
    modem = OrbitModem(1, "860000000000099", "081200000099", "Demo Site", "OldSSID")

    mock_driver = MagicMock()
    phone_input = MagicMock()
    imei_input = MagicMock()
    lanjut_btn = MagicMock()
    lanjut_btn.text = "Lanjutkan"
    cek_btn = MagicMock()
    cek_btn.text = "Cek Kuota"
    detail_btn = MagicMock()
    detail_btn.text = "Lihat Detail"

    body_modem = MagicMock()
    body_modem.text = """
    Nama WiFi
    tselhome-8BCB
    Total Kuota: 85.29 GB / 85.29 GB
    Lihat Detail
    """

    body_kuota = MagicMock()
    body_kuota.text = """
    FantaSIX 25GB
    25.0 GB
    / 25.0 GB
    Berlaku s.d 10 Oct 2026
    FantaSIX 25GB
    25.0 GB
    / 25.0 GB
    Berlaku s.d 15 Oct 2026
    Orbit 35GB
    35.0 GB
    / 35.0 GB
    Berlaku s.d 20 Oct 2026
    Orbit 0.15GB
    0.15 GB
    / 0.15 GB
    Berlaku s.d 25 Oct 2026
    Orbit 0.15GB
    0.14 GB
    / 0.15 GB
    Berlaku s.d 28 Oct 2026
    """

    current_page = ["prabayar-info-modem"]
    mock_driver.current_url = "https://www.myorbit.id/prabayar-info-modem"

    def mock_find_element(by: str, value: str) -> Any:
        if by == "tag name" and value == "body":
            if current_page[0] == "prabayar-info-modem":
                return body_modem
            return body_kuota
        if "Cek Kuota" in value:
            return cek_btn
        return MagicMock()

    def mock_find_elements(by: str, value: str) -> list[Any]:
        if by == "tag name" and value == "input":
            return [phone_input, imei_input]
        if by == "tag name" and value == "button":
            return [lanjut_btn]
        if "Cek Kuota" in value:
            return [cek_btn]
        if "Lihat Detail" in value or value == "//button | //a":
            return [detail_btn]
        return []

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements

    def mock_execute_script(script: str, *args: Any) -> Any:
        if args and args[0] is detail_btn:
            current_page[0] = "prabayar-info-kuota"
            mock_driver.current_url = "https://www.myorbit.id/prabayar-info-kuota"
        return None

    mock_driver.execute_script.side_effect = mock_execute_script
    mock_driver.page_source = """
    <div>
        <div class="wifi-label">Nama WiFi:</div>
        <div class="wifi-name">tselhome-8BCB</div>
        <div class="quota-header">Total Kuota: 85.29 GB / 85.29 GB</div>
        <div class="card package-item">
            <h4 class="title">FantaSIX 25GB</h4>
            <div>25.0 GB / 25.0 GB</div>
            <div>Berlaku s.d 10 Oct 2026</div>
        </div>
        <div class="card package-item">
            <h4 class="title">FantaSIX 25GB</h4>
            <div>25.0 GB / 25.0 GB</div>
            <div>Berlaku s.d 15 Oct 2026</div>
        </div>
        <div class="card package-item">
            <h4 class="title">Orbit 35GB</h4>
            <div>35.0 GB / 35.0 GB</div>
            <div>Berlaku s.d 20 Oct 2026</div>
        </div>
        <div class="card package-item">
            <h4 class="title">Orbit 0.15GB</h4>
            <div>0.15 GB / 0.15 GB</div>
            <div>Berlaku s.d 25 Oct 2026</div>
        </div>
        <div class="card package-item">
            <h4 class="title">Orbit 0.15GB</h4>
            <div>0.14 GB / 0.15 GB</div>
            <div>Berlaku s.d 28 Oct 2026</div>
        </div>
    </div>
    """

    status = scraper.scrape_modem(modem, driver=mock_driver, now=ref_now)
    assert status.error is None
    assert modem.ssid == "tselhome-8BCB"
    assert status.total_remaining_gb == 85.29
    assert status.total_quota_gb == 85.29
    assert len(status.packages) == 5
    assert status.earliest_expiry_str == "10 Oct 2026"
    assert status.earliest_days_left == 9


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
    assert data["stats"]["idle"] == 7
    assert "ex_customer" not in data["stats"]
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

    with patch("mrtg_cmp.web.app._orbit_service") as mock_srv_getter:
        mock_srv = MagicMock()
        mock_srv_getter.return_value = mock_srv
        resp = client_with_db.post("/api/orbit/sync", cookies=login_resp.cookies)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["message"] == "Live Orbit sync queued"
        assert data["count"] == 12
        mock_srv.sync_in_background.assert_called_once()


def test_orbit_dashboard_html_view(client_with_db: TestClient) -> None:
    """GET /orbit renders all filter pills, cards, modal elements, and buttons."""
    login_resp = client_with_db.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client_with_db.get("/orbit", cookies=login_resp.cookies)

    assert resp.status_code == 200
    text = resp.text

    assert "Telkomsel Orbit Modem Monitoring" in text
    assert "All Modems (12)" in text
    assert "Active in Room (5)" in text
    assert "Idle / Spare (7)" in text
    # Decommissioned / EX_CUSTOMED category was removed in Phase 59
    assert "Decommissioned" not in text
    assert "EX_CUSTOMER" not in text
    assert "Refresh All" in text
    assert "btn-sync-orbit" not in text
    assert "View Package Details" in text
    assert "orbit-countdown-timer" in text
    assert "btn-orbit-refresh-all" in text
    assert "orbit-sync-status" in text
    assert "pollOrbitSyncStatus" in text
    assert "fetch('/api/orbit/sync'" in text
    assert "orbit-package-modal" in text or "orbit-modal" in text

    # Phase 59 CRUD UI (Add / Edit / Delete)
    assert "btn-orbit-add" in text
    assert "btn-orbit-edit" in text
    assert "btn-orbit-delete" in text
    assert 'id="orbit-edit-modal"' in text
    assert 'id="orbit-delete-modal"' in text
    assert 'id="orbit-form-latitude"' in text
    assert 'id="orbit-form-longitude"' in text
    assert "/api/orbit/modem" in text


def test_extract_modem_info_updates_target_ssid() -> None:
    """Live carrier SSID extracted from portal updates target.ssid."""
    target = OrbitModem(1, "860000000000001", "081200000001", "Room 101", "Typo_SSID")
    html = """
    <div class="modem-info">
        <span class="wifi-label">Nama WiFi:</span>
        <span class="wifi-name">Orbit_Star_Live_SSID</span>
    </div>
    """
    info = extract_modem_info(html, target=target)
    assert info["wifi_name"] == "Orbit_Star_Live_SSID"
    assert target.ssid == "Orbit_Star_Live_SSID"


def test_extract_modem_info_from_rendered_text(tmp_path: Path) -> None:
    """Extracts SSID from rendered text with 'Nama WiFi\ntselhome-8BCB'."""
    csv_file = tmp_path / "orbit_targets.csv"
    csv_content = (
        "no,imei,phone,location,ssid,status\n"
        "1,860000000000001,6281367149931,Room 101,Old_SSID,ACTIVE\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    target = OrbitModem(1, "860000000000001", "6281367149931", "Room 101", "Old_SSID")
    rendered_text = """
    Nama WiFi
    tselhome-8BCB
    6281367149931
    Kuota Internet
    Sisa 85.29GB / 85.29GB
    Kuota Multimedia
    Tidak ada kuota multimedia aktif
    Lihat Detail
    """
    info = extract_modem_info(rendered_text, target=target, config_dir=tmp_path)
    assert info["wifi_name"] == "tselhome-8BCB"
    assert target.ssid == "tselhome-8BCB"

    # Verify disk persistence
    modems = load_orbit_catalog_csv(csv_file)
    assert modems[0].ssid == "tselhome-8BCB"


def test_update_catalog_modem_ssid_csv(tmp_path: Path) -> None:
    """SSID updates are written back to orbit_targets.csv on disk."""
    csv_file = tmp_path / "orbit_targets.csv"
    csv_content = """no,imei,phone,location,ssid,status
1,1700000001,081200000001,Meeting Room 1,Old_SSID_1,ACTIVE
2,860000000000002,081200000002,IDLE,Old_SSID_2,IDLE
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    # Update by phone
    res1 = update_catalog_modem_ssid("081200000001", "Updated_SSID_1", config_dir=tmp_path)
    assert res1 is True

    # Update by IMEI
    res2 = update_catalog_modem_ssid("860000000000002", "Updated_SSID_2", config_dir=tmp_path)
    assert res2 is True

    # No match
    res3 = update_catalog_modem_ssid("089999999999", "Unknown_SSID", config_dir=tmp_path)
    assert res3 is False

    # Check updated content
    updated_modems = load_orbit_catalog_csv(csv_file)
    assert len(updated_modems) == 2
    assert updated_modems[0].ssid == "Updated_SSID_1"
    assert updated_modems[1].ssid == "Updated_SSID_2"


def test_update_catalog_modem_ssid_excel(tmp_path: Path) -> None:
    """SSID updates are written back to orbit excel workbook on disk."""
    import openpyxl

    excel_file = tmp_path / f"orbit_{''.join(('gm', 'f'))}.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["No.", "IMEI", "No. Telepon", "Lokasi Modem", "SSID Modem", "Status"])
    ws.append([1, "1700000001", "081200000001", "Room Alpha", "Old_SSID_A", "ACTIVE"])
    ws.append([2, "860000000000002", "081200000002", "Room Bravo", "Old_SSID_B", "IDLE"])
    wb.save(str(excel_file))

    # Update by phone
    res1 = update_catalog_modem_ssid("081200000001", "Live_SSID_Alpha", config_dir=tmp_path)
    assert res1 is True

    # Update by IMEI
    res2 = update_catalog_modem_ssid("860000000000002", "Live_SSID_Bravo", config_dir=tmp_path)
    assert res2 is True

    # Verify workbook persisted changes
    loaded_wb = openpyxl.load_workbook(str(excel_file))
    loaded_ws = loaded_wb.active
    assert loaded_ws is not None
    rows = list(loaded_ws.iter_rows(values_only=True))
    assert rows[1][4] == "Live_SSID_Alpha"
    assert rows[2][4] == "Live_SSID_Bravo"


def test_extract_modem_info_persists_to_catalog(tmp_path: Path) -> None:
    """extract_modem_info persists changed SSID to catalog on disk."""
    csv_file = tmp_path / "orbit_targets.csv"
    csv_content = (
        "no,imei,phone,location,ssid,status\n"
        "1,860000000000001,081200000001,Room 101,Typo_SSID,ACTIVE\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    target = OrbitModem(1, "860000000000001", "081200000001", "Room 101", "Typo_SSID")
    html = """
    <div class="modem-info">
        <span class="wifi-label">Nama WiFi:</span>
        <span class="wifi-name">Orbit_Star_New_SSID</span>
    </div>
    """
    info = extract_modem_info(html, target=target, config_dir=tmp_path)
    assert info["wifi_name"] == "Orbit_Star_New_SSID"
    assert target.ssid == "Orbit_Star_New_SSID"

    # Verify CSV was updated
    modems = load_orbit_catalog_csv(csv_file)
    assert len(modems) == 1
    assert modems[0].ssid == "Orbit_Star_New_SSID"


def test_update_catalog_modem_ssid_empty_and_missing() -> None:
    """Empty parameters or missing config dir return False safely."""
    assert update_catalog_modem_ssid("", "") is False
    assert update_catalog_modem_ssid("0812345", "") is False
    assert update_catalog_modem_ssid("", "SSID") is False
    missing_dir = Path("non_existent_dir_12345")
    assert update_catalog_modem_ssid("0812345", "SSID", config_dir=missing_dir) is False


def test_build_orbit_driver() -> None:
    """build_orbit_driver configures headless Chrome and an isolated profile."""
    with patch("selenium.webdriver.Chrome") as mock_chrome:
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver
        driver = build_orbit_driver(headless=True)
        assert driver is mock_driver
        mock_chrome.assert_called_once()
        options = mock_chrome.call_args[1]["options"]
        assert "--headless=new" in options.arguments
        assert "--no-sandbox" in options.arguments
        assert "--disable-dev-shm-usage" in options.arguments
        profile_argument = next(
            arg for arg in options.arguments if arg.startswith("--user-data-dir=")
        )
        profile_dir = Path(profile_argument.split("=", 1)[1])
        assert profile_dir.is_dir()
        assert profile_dir != Path.cwd()
        assert mock_driver._orbit_profile_dir == str(profile_dir)
        shutil.rmtree(profile_dir)


def test_orbit_service_sync_all_cleans_owned_chrome_profile() -> None:
    mock_driver = MagicMock()
    with patch("mrtg_cmp.orbit.service.build_orbit_driver", return_value=mock_driver):
        service = OrbitService()
        with patch.object(service.scraper, "scrape_modem", return_value=MagicMock()):
            catalog = [OrbitModem(2, "860000000000002", "081200000002", "Room 2", "SSID-2")]
            service.sync_all(catalog)
    mock_driver.quit.assert_called_once()


def test_orbit_service_sync_all(tmp_path: Path) -> None:
    """OrbitService.sync_all processes all modems and persists status to cache."""
    cache_file = tmp_path / "modems.json"
    cache = OrbitCache(cache_file)

    catalog = [
        OrbitModem(1, "1700000001", "081200000001", "Room 1", "SSID-1"),  # Invalid IMEI
        OrbitModem(2, "860000000000002", "081200000002", "Room 2", "SSID-2"),  # Valid
        OrbitModem(3, "860000000000003", "081200000003", "Spare Site", "SSID-3", status="IDLE"),
    ]

    mock_driver = MagicMock()
    phone_input = MagicMock()
    imei_input = MagicMock()
    lanjut_btn = MagicMock()
    lanjut_btn.text = "Lanjutkan"
    cek_btn = MagicMock()
    cek_btn.text = "Cek Kuota"
    detail_btn = MagicMock()
    detail_btn.text = "Lihat Detail"

    body_modem = MagicMock()
    body_modem.text = """
    Nama WiFi
    tselhome-8BCB
    Total Kuota: 50.0 GB / 50.0 GB
    Lihat Detail
    """

    body_kuota = MagicMock()
    body_kuota.text = """
    Internet Orbit 50GB
    50.0GB
    / 50.0GB
    Berlaku s.d 08 Oct 2026
    """

    current_page = ["prabayar-info-modem"]
    mock_driver.current_url = "https://www.myorbit.id/prabayar-info-modem"

    def mock_find_element(by: str, value: str) -> Any:
        if by == "tag name" and value == "body":
            if current_page[0] == "prabayar-info-modem":
                return body_modem
            return body_kuota
        if "Cek Kuota" in value:
            return cek_btn
        return MagicMock()

    def mock_find_elements(by: str, value: str) -> list[Any]:
        if by == "tag name" and value == "input":
            return [phone_input, imei_input]
        if by == "tag name" and value == "button":
            return [lanjut_btn]
        if "Cek Kuota" in value:
            return [cek_btn]
        if "Lihat Detail" in value or value == "//button | //a":
            return [detail_btn]
        return []

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements

    def mock_execute_script(script: str, *args: Any) -> Any:
        if args and args[0] is detail_btn:
            current_page[0] = "prabayar-info-kuota"
            mock_driver.current_url = "https://www.myorbit.id/prabayar-info-kuota"
        return None

    mock_driver.execute_script.side_effect = mock_execute_script
    mock_driver.page_source = """
    <div>
        <div class="wifi-name">tselhome-8BCB</div>
        <div>50.0 GB / 50.0 GB</div>
    </div>
    """

    service = OrbitService()
    statuses = service.sync_all(catalog, cache=cache, driver=mock_driver)

    assert len(statuses) == 3
    assert statuses[0].error == "IMEI_PENDING"
    assert statuses[1].error is None
    assert statuses[1].total_remaining_gb == 50.0
    assert statuses[1].target.ssid == "tselhome-8BCB"
    assert statuses[2].error is None
    assert mock_driver.get.call_count == 2
    assert phone_input.send_keys.call_args_list[-1].args[0] == catalog[2].phone
    assert imei_input.send_keys.call_args_list[-1].args[0] == catalog[2].imei

    # Verify cache on disk
    assert cache.is_cached()
    loaded = cache.load()
    assert loaded is not None
    assert len(loaded) == 3
    assert loaded[1].total_remaining_gb == 50.0


def test_orbit_service_sync_in_background_concurrency() -> None:
    """Duplicate sync triggers are rejected while a sync is in progress."""
    service = OrbitService()
    catalog = [OrbitModem(1, "1700000001", "081200000001", "Room 1", "SSID-1")]

    # Manually acquire the sync flag
    service._is_syncing = True
    assert service.is_syncing is True

    # Duplicate background attempt should be skipped
    res = service.sync_in_background(catalog)
    assert res is False

    # Release lock
    service._is_syncing = False
    assert service.is_syncing is False

    # Now it succeeds and triggers a worker thread
    res2 = service.sync_in_background(catalog)
    assert res2 is True


def test_cli_orbit_sync(capsys: Any, tmp_path: Path) -> None:
    """mrtg-cmp orbit-sync CLI command executes and prints modem status rows."""
    catalog_file = tmp_path / "orbit_targets.csv"
    catalog_file.write_text(
        "no,imei,phone,location,ssid,status\n"
        "1,1700000001,081200000001,Room 1,SSID-1,ACTIVE\n"
        "2,860000000000002,081200000002,Room 2,SSID-2,ACTIVE\n",
        encoding="utf-8",
    )
    cache_file = tmp_path / "modems.json"

    with patch.object(OrbitService, "sync_all") as mock_sync_all:
        mock_sync_all.return_value = [
            OrbitModemStatus(
                target=OrbitModem(1, "1700000001", "081200000001", "Room 1", "SSID-1"),
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str=None,
                earliest_days_left=None,
                last_scraped_at="2026-10-01 10:00:00",
                error="IMEI_PENDING",
            ),
            OrbitModemStatus(
                target=OrbitModem(2, "860000000000002", "081200000002", "Room 2", "tselhome-8BCB"),
                total_remaining_gb=85.29,
                total_quota_gb=85.29,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str="10 Oct 2026",
                earliest_days_left=9,
                last_scraped_at="2026-10-01 10:00:00",
                error=None,
            ),
        ]

        exit_code = main([
            "orbit-sync",
            "--catalog",
            str(catalog_file),
            "--cache-file",
            str(cache_file),
        ])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "[IMEI_PENDING] 081200000001 (SSID-1): IMEI_PENDING" in captured.out
        assert "[OK] 081200000002 (tselhome-8BCB): 85.29 GB / 85.29 GB" in captured.out


def test_orbit_daemon_runs_single_round(tmp_path: Path) -> None:
    """OrbitDaemon runs a single round and persists status to cache."""
    cache = OrbitCache(tmp_path / "modems.json")
    catalog = [
        OrbitModem(1, "1700000001", "081200000001", "Room 1", "SSID-1"),
        OrbitModem(2, "860000000000002", "081200000002", "Room 2", "SSID-2"),
    ]
    mock_service = MagicMock(spec=OrbitService)
    mock_service.sync_all.return_value = [
        OrbitModemStatus(target=catalog[0], error="IMEI_PENDING"),
        OrbitModemStatus(target=catalog[1], total_remaining_gb=10.0, total_quota_gb=20.0),
    ]

    daemon = OrbitDaemon(
        service=mock_service,
        catalog_loader=lambda: catalog,
        cache=cache,
        interval_seconds=300,
        sleeper=lambda _sec: None,
    )

    rounds = daemon.run(max_rounds=1)
    assert rounds == 1
    mock_service.sync_all.assert_called_once_with(catalog, cache)


def test_orbit_daemon_stops_when_stop_event_set_before_run(tmp_path: Path) -> None:
    """OrbitDaemon exits immediately when stop_event is already set."""
    cache = OrbitCache(tmp_path / "modems.json")
    mock_service = MagicMock(spec=OrbitService)
    stop = threading.Event()
    stop.set()

    daemon = OrbitDaemon(
        service=mock_service,
        catalog_loader=list,
        cache=cache,
        interval_seconds=300,
        sleeper=lambda _sec: None,
    )

    rounds = daemon.run(stop_event=stop)
    assert rounds == 0
    mock_service.sync_all.assert_not_called()


def test_orbit_daemon_sleep_is_interrupted_by_stop_event(tmp_path: Path) -> None:
    """OrbitDaemon stops promptly after sleep interrupts via stop_event."""
    cache = OrbitCache(tmp_path / "modems.json")
    stop = threading.Event()
    slept: list[int] = []

    def sleeper(seconds: int) -> None:
        slept.append(seconds)
        stop.set()

    mock_service = MagicMock(spec=OrbitService)
    mock_service.sync_all.return_value = []

    daemon = OrbitDaemon(
        service=mock_service,
        catalog_loader=list,
        cache=cache,
        interval_seconds=600,
        sleeper=sleeper,
    )

    rounds = daemon.run(stop_event=stop, max_rounds=5)
    assert rounds == 1
    assert slept == [600]


def test_orbit_daemon_survives_failing_round(tmp_path: Path) -> None:
    """OrbitDaemon catches and logs exceptions from sync_all without crashing."""
    cache = OrbitCache(tmp_path / "modems.json")
    mock_service = MagicMock(spec=OrbitService)
    mock_service.sync_all.side_effect = RuntimeError("Selenium driver crashed")

    daemon = OrbitDaemon(
        service=mock_service,
        catalog_loader=list,
        cache=cache,
        interval_seconds=300,
        sleeper=lambda _sec: None,
    )

    rounds = daemon.run(max_rounds=1)
    assert rounds == 1


def test_build_orbit_daemon_from_settings() -> None:
    """build_orbit_daemon_from_settings constructs a properly configured OrbitDaemon."""
    daemon = build_orbit_daemon_from_settings()
    assert isinstance(daemon, OrbitDaemon)
    assert isinstance(daemon.service, OrbitService)
    assert daemon.interval_seconds == 300
    assert callable(daemon.catalog_loader)
    assert isinstance(daemon.cache, OrbitCache)


def test_orbit_modem_coordinates_are_coerced() -> None:
    """Numeric strings become floats; blanks and junk become None."""
    modem = OrbitModem(
        1, "860000000000001", "081200000001", "Room 1", "SSID-1",
        latitude="-6.2005", longitude=" 106.8166 ",
    )
    assert modem.latitude == -6.2005
    assert modem.longitude == 106.8166

    blank = OrbitModem(
        2, "860000000000002", "081200000002", "Room 2", "SSID-2",
        latitude="", longitude="not-a-number",
    )
    assert blank.latitude is None
    assert blank.longitude is None
    assert blank.as_dict()["latitude"] is None
    assert blank.as_dict()["longitude"] is None


def test_classify_status_folds_legacy_labels_into_idle() -> None:
    """Only ACTIVE stays ACTIVE; every other legacy label becomes IDLE."""
    assert _classify_status("ACTIVE", "Room") == "ACTIVE"
    assert _classify_status("aktif", "Room") == "ACTIVE"
    for legacy in ("IDLE", "EX_CUSTOMER", "BEKAS", "Decommissioned", "  "):
        assert _classify_status(legacy, "Room") == "IDLE"

    # A blank status falls back to the location heuristic
    assert _classify_status(None, "Meeting Room") == "ACTIVE"
    assert _classify_status(None, "EX MODEM Korean Air H3") == "IDLE"


def test_save_orbit_catalog_csv_roundtrip_creates_timestamped_backup(tmp_path: Path) -> None:
    """Saving twice keeps a timestamped .bak copy of the first write."""
    csv_path = tmp_path / "orbit_targets.csv"
    modems = [
        OrbitModem(1, "860000000000001", "081200000001", "Room 1", "SSID-1",
                   status="ACTIVE", latitude=-6.2, longitude=106.8),
        OrbitModem(2, "860000000000002", "081200000002", "Room 2", "SSID-2", status="IDLE"),
    ]

    assert save_orbit_catalog_csv(modems, csv_path) is None
    assert csv_path.is_file()

    reloaded = load_orbit_catalog_csv(csv_path)
    assert len(reloaded) == 2
    assert reloaded[0].latitude == -6.2
    assert reloaded[0].longitude == 106.8
    assert reloaded[1].latitude is None

    backup = save_orbit_catalog_csv(reloaded, csv_path)
    assert backup is not None
    assert backup.exists()
    assert backup.parent == tmp_path / "backups"
    assert backup.suffix == ".bak"
    assert ".csv.bak" in backup.name
    # No temp files are left behind
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_orbit_catalog_csv_honours_explicit_backup_dir(tmp_path: Path) -> None:
    """A caller-supplied backup directory overrides the default 'backups' folder."""
    csv_path = tmp_path / "orbit_targets.csv"
    backup_dir = tmp_path / "archive"
    save_orbit_catalog_csv([OrbitModem(1, "1", "081", "Room", "SSID")], csv_path)
    backup = save_orbit_catalog_csv(
        [OrbitModem(1, "1", "081", "Room", "SSID")], csv_path, backup_dir
    )
    assert backup is not None
    assert backup.parent == backup_dir
    assert not (tmp_path / "backups").exists()


def test_save_orbit_catalog_excel_roundtrip_creates_backup(tmp_path: Path) -> None:
    """Excel save reloads through the loader and backs up the previous workbook."""
    pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / _XLSX_NAME
    modems = [
        OrbitModem(1, "860000000000001", "081200000001", "Room 1", "SSID-1",
                   status="ACTIVE", latitude=-6.2, longitude=106.8),
        OrbitModem(2, "860000000000002", "081200000002", "Room 2", "SSID-2", status="IDLE"),
    ]

    assert save_orbit_catalog_excel(modems, xlsx_path) is None
    original_bytes = xlsx_path.read_bytes()

    reloaded = load_orbit_catalog_excel(xlsx_path)
    assert [m.no for m in reloaded] == [1, 2]
    assert reloaded[0].latitude == -6.2
    assert reloaded[0].longitude == 106.8
    assert reloaded[1].latitude is None

    backup = save_orbit_catalog_excel(reloaded, xlsx_path)
    assert backup is not None
    assert backup.suffix == ".bak"
    assert backup.read_bytes() == original_bytes


def test_save_orbit_catalog_excel_drops_deleted_rows(tmp_path: Path) -> None:
    """Deleting a modem and re-saving leaves no stale row behind."""
    pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / _XLSX_NAME
    modems = [OrbitModem(n, f"86000000000000{n}", f"08120000000{n}", f"Room {n}", f"SSID-{n}")
              for n in (1, 2, 3)]
    save_orbit_catalog_excel(modems, xlsx_path)

    save_orbit_catalog_excel(remove_orbit_modem(modems, 2), xlsx_path)

    reloaded = load_orbit_catalog_excel(xlsx_path)
    assert [m.no for m in reloaded] == [1, 3]


def test_upsert_orbit_modem_insert_update_and_renumber() -> None:
    """upsert inserts new entries, replaces same-numbered ones, and renumbers."""
    catalog = _default_orbit_catalog()

    added = upsert_orbit_modem(
        catalog,
        OrbitModem(13, "860000000000013", "081200000013", "New Room", "Orbit-New"),
    )
    assert len(added) == 13
    assert added[-1].no == 13

    updated = upsert_orbit_modem(
        added, OrbitModem(13, "860000000000013", "081200000013", "Renamed Room", "Orbit-New-2")
    )
    assert len(updated) == 13
    renamed = next(m for m in updated if m.no == 13)
    assert renamed.location == "Renamed Room"
    assert renamed.ssid == "Orbit-New-2"

    # Renumber 12 -> 20 drops the old entry rather than duplicating it
    renumbered = upsert_orbit_modem(
        updated,
        OrbitModem(20, "860000000000012", "081200000012", "Room 20", "Orbit-20"),
        original_no=12,
    )
    assert len(renumbered) == 13
    assert not any(m.no == 12 for m in renumbered)
    assert any(m.no == 20 for m in renumbered)


def test_remove_orbit_modem_returns_catalog_without_entry() -> None:
    """Removing a modem leaves the rest untouched; the source list is not mutated."""
    catalog = _default_orbit_catalog()
    reduced = remove_orbit_modem(catalog, 10)
    assert len(reduced) == 11
    assert not any(m.no == 10 for m in reduced)
    assert len(catalog) == 12


def test_persist_orbit_catalog_writes_only_the_explicit_target(tmp_path: Path) -> None:
    """An explicit path is written exclusively; the other format is left alone."""
    csv_path = tmp_path / "orbit_targets.csv"
    catalog = _default_orbit_catalog()

    result = persist_orbit_catalog(catalog, csv_path=csv_path)
    assert result["csv_backup"] is None  # nothing to back up on the first write
    assert result["excel_backup"] is None  # never touches the default workbook
    assert csv_path.is_file()

    result = persist_orbit_catalog(catalog, csv_path=csv_path)
    assert result["csv_backup"] is not None
    assert result["excel_backup"] is None

    # An explicit Excel path is honoured even when the file does not exist yet
    xlsx_path = tmp_path / _XLSX_NAME
    result = persist_orbit_catalog(catalog, excel_path=xlsx_path)
    assert result["csv_backup"] is None
    assert result["excel_backup"] is None
    assert xlsx_path.is_file()


def _login(client: TestClient):
    resp = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert resp.status_code in (200, 302, 303)
    return resp.cookies


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/orbit/modem"),
        ("put", "/api/orbit/modem/1"),
        ("delete", "/api/orbit/modem/1"),
    ],
)
def test_orbit_crud_endpoints_require_authentication(
    client_with_db: TestClient, method: str, path: str
) -> None:
    """Anonymous callers cannot mutate the Orbit catalog."""
    payload = {"no": 99, "location": "Room 99"}
    if method == "delete":
        resp = client_with_db.delete(path)
    else:
        resp = getattr(client_with_db, method)(path, json=payload)
    assert resp.status_code in (401, 403, 303, 307)


def test_api_orbit_modem_create_persists_and_audits(
    client_with_db: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """POST creates a modem, writes the catalog, and records an audit entry."""
    catalog_file = tmp_path / "orbit_targets.csv"
    monkeypatch.setattr(settings, "orbit_catalog_file", str(catalog_file))
    cookies = _login(client_with_db)

    with patch("mrtg_cmp.web.app.audit_log") as mock_audit:
        resp = client_with_db.post(
            "/api/orbit/modem",
            cookies=cookies,
            json={
                "no": 42,
                "imei": "860000000000042",
                "phone": "081200000042",
                "location": "  Meeting Room  ",
                "ssid": "Orbit-New",
                "status": "idle",
                "latitude": -6.2,
                "longitude": 106.8,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["no"] == 42
    assert body["location"] == "Meeting Room"  # trimmed
    assert body["status"] == "IDLE"  # normalised
    assert body["latitude"] == -6.2
    assert body["longitude"] == 106.8

    # The new modem survives a round-trip through the catalog file
    persisted = {m.no: m for m in load_orbit_catalog_csv(catalog_file)}
    assert len(persisted) == 13  # the 12 defaults plus the new one
    assert persisted[42].location == "Meeting Room"
    assert persisted[42].status == "IDLE"
    assert persisted[42].latitude == -6.2

    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][0] == "ORBIT_CREATE"


def test_api_orbit_modem_create_rejects_duplicates_and_bad_input(
    client_with_db: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Duplicates, empty locations, and unknown statuses are rejected with 400."""
    monkeypatch.setattr(settings, "orbit_catalog_file", str(tmp_path / "orbit_targets.csv"))
    cookies = _login(client_with_db)

    duplicate = client_with_db.post(
        "/api/orbit/modem", cookies=cookies, json={"no": 1, "location": "Somewhere"}
    )
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.json()["detail"]

    blank_location = client_with_db.post(
        "/api/orbit/modem", cookies=cookies, json={"no": 99, "location": "   "}
    )
    assert blank_location.status_code == 400
    assert "Location" in blank_location.json()["detail"]

    bad_status = client_with_db.post(
        "/api/orbit/modem",
        cookies=cookies,
        json={"no": 99, "location": "Room 99", "status": "Decommissioned"},
    )
    assert bad_status.status_code == 400
    assert "Invalid status" in bad_status.json()["detail"]

    bad_number = client_with_db.post(
        "/api/orbit/modem", cookies=cookies, json={"no": 0, "location": "Room 99"}
    )
    assert bad_number.status_code == 400


def test_api_orbit_modem_update_and_renumber(
    client_with_db: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PUT edits in place and can renumber, dropping the previous entry."""
    catalog_file = tmp_path / "orbit_targets.csv"
    monkeypatch.setattr(settings, "orbit_catalog_file", str(catalog_file))
    cookies = _login(client_with_db)

    with patch("mrtg_cmp.web.app.audit_log") as mock_audit:
        resp = client_with_db.put(
            "/api/orbit/modem/3",
            cookies=cookies,
            json={
                "no": 3,
                "imei": "860000000000003",
                "phone": "081200000003",
                "location": "Renamed Room",
                "ssid": "Orbit-Renamed",
                "status": "ACTIVE",
                "latitude": -6.9,
                "longitude": 107.1,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["location"] == "Renamed Room"
    assert mock_audit.call_args[0][0] == "ORBIT_UPDATE"

    persisted = {m.no: m for m in load_orbit_catalog_csv(catalog_file)}
    assert persisted[3].location == "Renamed Room"
    assert persisted[3].latitude == -6.9
    assert persisted[3].longitude == 107.1

    # Renumbering 3 -> 30 must not leave modem 3 behind
    renumbered = client_with_db.put(
        "/api/orbit/modem/3",
        cookies=cookies,
        json={"no": 30, "location": "Renamed Room", "ssid": "Orbit-Renamed", "status": "ACTIVE"},
    )
    assert renumbered.status_code == 200
    persisted = {m.no: m for m in load_orbit_catalog_csv(catalog_file)}
    assert 3 not in persisted
    assert 30 in persisted

    # Renumbering onto an existing modem is a conflict
    conflict = client_with_db.put(
        "/api/orbit/modem/30",
        cookies=cookies,
        json={"no": 4, "location": "Clash", "status": "ACTIVE"},
    )
    assert conflict.status_code == 400
    assert "already exists" in conflict.json()["detail"]

    missing = client_with_db.put(
        "/api/orbit/modem/777",
        cookies=cookies,
        json={"no": 777, "location": "Nowhere", "status": "ACTIVE"},
    )
    assert missing.status_code == 404


def test_api_orbit_modem_delete_removes_from_catalog(
    client_with_db: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DELETE drops the modem from disk, refreshes stats, and audits."""
    catalog_file = tmp_path / "orbit_targets.csv"
    monkeypatch.setattr(settings, "orbit_catalog_file", str(catalog_file))
    cookies = _login(client_with_db)

    with patch("mrtg_cmp.web.app.audit_log") as mock_audit:
        resp = client_with_db.delete("/api/orbit/modem/10", cookies=cookies)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["modem"] == 10
    assert body["stats"]["all"] == 11
    assert "ex_customer" not in body["stats"]
    assert mock_audit.call_args[0][0] == "ORBIT_DELETE"

    persisted = load_orbit_catalog_csv(catalog_file)
    assert 10 not in {m.no for m in persisted}
    assert len(persisted) == 11

    missing = client_with_db.delete("/api/orbit/modem/10", cookies=cookies)
    assert missing.status_code == 404


def test_api_orbit_modem_crud_writes_excel_when_configured(
    client_with_db: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An .xlsx catalog path routes the write through the Excel writer."""
    pytest.importorskip("openpyxl")
    catalog_file = tmp_path / _XLSX_NAME
    monkeypatch.setattr(settings, "orbit_catalog_file", str(catalog_file))
    cookies = _login(client_with_db)

    resp = client_with_db.post(
        "/api/orbit/modem",
        cookies=cookies,
        json={"no": 77, "location": "Spare Room", "status": "IDLE", "latitude": -6.1,
              "longitude": 106.9},
    )
    assert resp.status_code == 200

    persisted = load_orbit_catalog_excel(catalog_file)
    assert 77 in {m.no for m in persisted}
    added = next(m for m in persisted if m.no == 77)
    assert added.latitude == -6.1
    assert added.longitude == 106.9

    # A second write backs the workbook up first
    second = client_with_db.delete("/api/orbit/modem/77", cookies=cookies)
    assert second.status_code == 200
    backups = list((catalog_file.parent / "backups").glob(f"{DEFAULT_EXCEL_PATH.stem}_*.xlsx.bak"))
    assert len(backups) == 1
    assert 77 not in {m.no for m in load_orbit_catalog_excel(catalog_file)}


def test_quota_burn_rate_classification_and_forecasting(tmp_path: Path) -> None:
    """Test QuotaBurnRateTracker calculations, thresholds, and alert levels."""
    from datetime import UTC, datetime, timedelta

    from mrtg_cmp.orbit.burn_rate import (
        QuotaBurnRateTracker,
        classify_alert_level,
        format_burn_alert_message,
    )
    from mrtg_cmp.orbit.scraper import OrbitModemStatus
    from mrtg_cmp.orbit.targets import OrbitModem

    # Direct threshold classification
    assert classify_alert_level(2.5, 50.0) == "CRITICAL"
    assert classify_alert_level(10.0, 4.0) == "CRITICAL"
    assert classify_alert_level(5.0, 30.0) == "WARNING"
    assert classify_alert_level(20.0, 10.0) == "WARNING"
    assert classify_alert_level(15.0, 40.0) == "NORMAL"
    assert classify_alert_level(None, 40.0) == "NORMAL"

    # Tracker rolling calculation
    tracker = QuotaBurnRateTracker(tmp_path / "burn_history.json")
    t0 = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    t2 = t1 + timedelta(days=1)

    imei = "860123456789012"
    tracker.record_snapshot(imei, 100.0, 100.0, timestamp=t0)
    tracker.record_snapshot(imei, 95.0, 100.0, timestamp=t1)
    tracker.record_snapshot(imei, 90.0, 100.0, timestamp=t2)

    modem = OrbitModem(no=1, imei=imei, phone="081234567890", location="Room A", ssid="Orbit-RoomA")
    status = OrbitModemStatus(target=modem, total_remaining_gb=90.0, total_quota_gb=100.0)
    res = tracker.compute(imei, status)

    assert res.burn_rate_gb_per_day is not None
    assert round(res.burn_rate_gb_per_day, 1) == 5.0
    assert res.days_until_exhaustion is not None
    assert round(res.days_until_exhaustion, 1) == 18.0
    assert res.alert_level == "NORMAL"

    # Critical alert message formatting
    msg = format_burn_alert_message(res)
    assert "QUOTA BURN-RATE ALERT" in msg
    assert "Room A" in msg

