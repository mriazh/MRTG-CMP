"""Telkomsel Orbit modem scraper and quota extraction engine.

Extracts total quota, multimedia quota, and individual package breakdowns from
the no-auth MyOrbit portal: https://www.myorbit.id/informasi-modem-input.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mrtg_cmp.orbit.targets import OrbitModem

logger = logging.getLogger("mrtg_cmp.orbit.scraper")

DEFAULT_ORBIT_PORTAL_URL = "https://www.myorbit.id/informasi-modem-input"

# Month abbreviations mapping (both Indonesian and English)
MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mei": 5,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "agu": 8,
    "ags": 8,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "oct": 10,
    "nov": 11,
    "des": 12,
    "dec": 12,
}


@dataclass
class OrbitPackage:
    """Represents an individual Orbit internet or multimedia package."""

    name: str
    remaining_gb: float
    total_gb: float
    expiry_str: str
    days_left: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "remaining_gb": self.remaining_gb,
            "total_gb": self.total_gb,
            "expiry_str": self.expiry_str,
            "days_left": self.days_left,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrbitPackage:
        """Construct from dictionary."""
        return cls(
            name=data.get("name", ""),
            remaining_gb=float(data.get("remaining_gb", 0.0)),
            total_gb=float(data.get("total_gb", 0.0)),
            expiry_str=data.get("expiry_str", ""),
            days_left=data.get("days_left"),
        )


@dataclass
class OrbitModemStatus:
    """Represents the scraped status and quota info for an Orbit modem."""

    target: OrbitModem
    total_remaining_gb: float = 0.0
    total_quota_gb: float = 0.0
    multimedia_active: bool = False
    packages: list[OrbitPackage] = field(default_factory=list)
    earliest_expiry_str: str | None = None
    earliest_days_left: int | None = None
    last_scraped_at: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "target": self.target.as_dict(),
            "total_remaining_gb": round(self.total_remaining_gb, 2),
            "total_quota_gb": round(self.total_quota_gb, 2),
            "multimedia_active": self.multimedia_active,
            "packages": [p.as_dict() for p in self.packages],
            "earliest_expiry_str": self.earliest_expiry_str,
            "earliest_days_left": self.earliest_days_left,
            "last_scraped_at": self.last_scraped_at,
            "error": self.error,
        }

    @property
    def packages_json(self) -> str:
        """JSON-serialized string of packages for data attributes in HTML."""
        import json
        return json.dumps([p.as_dict() for p in self.packages])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrbitModemStatus:
        """Construct from dictionary."""
        target_dict = data.get("target", {})
        target = OrbitModem(
            no=int(target_dict.get("no", 0)),
            imei=str(target_dict.get("imei", "")),
            phone=str(target_dict.get("phone", "")),
            location=str(target_dict.get("location", "")),
            ssid=str(target_dict.get("ssid", "")),
            status=str(target_dict.get("status", "ACTIVE")),
            imei_valid=bool(target_dict.get("imei_valid", False)),
        )
        packages = [
            OrbitPackage.from_dict(p)
            for p in data.get("packages", [])
            if isinstance(p, dict)
        ]
        return cls(
            target=target,
            total_remaining_gb=float(data.get("total_remaining_gb", 0.0)),
            total_quota_gb=float(data.get("total_quota_gb", 0.0)),
            multimedia_active=bool(data.get("multimedia_active", False)),
            packages=packages,
            earliest_expiry_str=data.get("earliest_expiry_str"),
            earliest_days_left=data.get("earliest_days_left"),
            last_scraped_at=str(data.get("last_scraped_at", "")),
            error=data.get("error"),
        )


def parse_quota_string(text: str) -> tuple[float, float]:
    """Parse quota string like '28.56GB / 30GB' or '500MB / 10GB'.

    Returns (remaining_gb, total_gb).
    """
    if not text:
        return 0.0, 0.0

    # Pattern: remaining [unit] / total [unit]
    pattern = r"([\d.,]+)\s*(GB|MB|KB)?\s*/\s*([\d.,]+)\s*(GB|MB|KB)?"
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        # Try single remaining pattern
        single = re.search(r"([\d.,]+)\s*(GB|MB|KB)?", text, re.IGNORECASE)
        if single:
            val = float(single.group(1).replace(",", "."))
            unit = (single.group(2) or "GB").upper()
            if unit == "MB":
                val /= 1024.0
            elif unit == "KB":
                val /= (1024.0 * 1024.0)
            return round(val, 2), round(val, 2)
        return 0.0, 0.0

    rem_val = float(m.group(1).replace(",", "."))
    rem_unit = (m.group(2) or m.group(4) or "GB").upper()
    tot_val = float(m.group(3).replace(",", "."))
    tot_unit = (m.group(4) or "GB").upper()

    if rem_unit == "MB":
        rem_val /= 1024.0
    elif rem_unit == "KB":
        rem_val /= (1024.0 * 1024.0)

    if tot_unit == "MB":
        tot_val /= 1024.0
    elif tot_unit == "KB":
        tot_val /= (1024.0 * 1024.0)

    return round(rem_val, 2), round(tot_val, 2)


def parse_expiry_date(
    expiry_text: str, now: datetime | None = None
) -> tuple[str, int | None]:
    """Parse expiry string (e.g. 'Berlaku s.d 08 Oct 2026') and compute days left.

    Returns (clean_expiry_str, days_left).
    """
    if not expiry_text:
        return "", None

    # Clean prefix like 'Berlaku s.d.', 'Berlaku s.d', 'Berlaku s/d'
    cleaned = re.sub(
        r"^berlaku\s+s[\./]?d[\.:]?\s*", "", expiry_text.strip(), flags=re.IGNORECASE
    ).strip()

    ref_now = now or datetime.now(UTC)

    # Match DD Mon YYYY (e.g. 08 Oct 2026 or 8 Okt 2026)
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", cleaned)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)[:3].lower()
        year = int(m.group(3))
        month = MONTH_MAP.get(mon_str)
        if month:
            try:
                target_dt = datetime(year, month, day, tzinfo=UTC)
                days_left = (target_dt.date() - ref_now.date()).days
                # Format standard e.g. "08 Oct 2026"
                standard_str = target_dt.strftime("%d %b %Y")
                return standard_str, days_left
            except ValueError:
                pass

    # Match DD/MM/YYYY or YYYY-MM-DD
    slash_match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", cleaned)
    if slash_match:
        day = int(slash_match.group(1))
        month = int(slash_match.group(2))
        year = int(slash_match.group(3))
        try:
            target_dt = datetime(year, month, day, tzinfo=UTC)
            days_left = (target_dt.date() - ref_now.date()).days
            return target_dt.strftime("%d %b %Y"), days_left
        except ValueError:
            pass

    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if iso_match:
        year = int(iso_match.group(1))
        month = int(iso_match.group(2))
        day = int(iso_match.group(3))
        try:
            target_dt = datetime(year, month, day, tzinfo=UTC)
            days_left = (target_dt.date() - ref_now.date()).days
            return target_dt.strftime("%d %b %Y"), days_left
        except ValueError:
            pass

    return cleaned, None


def parse_package_cards_html(html: str, now: datetime | None = None) -> list[OrbitPackage]:
    """Parse package breakdown cards from HTML markup.

    Matches cards containing package name, quota info, and expiry date.
    """
    packages: list[OrbitPackage] = []
    if not html:
        return packages

    # Regex pattern to match package blocks or rows on MyOrbit
    card_pattern = re.compile(
        r"(?:<div[^>]*class=[\"'][^\"']*(?:card|package|item)[^\"']*[\"'][^>]*>)(.*?)"
        r"(?=</div>\s*<div[^>]*class=[\"'][^\"']*(?:card|package|item)[^\"']*[\"']|</div>\s*</div>|$)",
        re.DOTALL | re.IGNORECASE,
    )

    matches = card_pattern.findall(html)
    if not matches:
        # Fallback: look for lines or blocks containing 'Berlaku' and 'GB'
        matches = [html]

    known_packages = (
        "Internet Orbit",
        "Kuota FantaSIX",
        "Kuota Sahur",
        "Multimedia Orbit",
        "Orbit Booster",
        "Paket Ekstra",
    )

    for block in matches:
        # Find package name
        name = "Internet Orbit"
        name_match = re.search(
            r"<(?:h[3-6]|p|strong|span)[^>]*class=[\"'][^\"']*(?:title|name|header)[^\"']*[\"'][^>]*>(.*?)</(?:h[3-6]|p|strong|span)>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if name_match:
            name = re.sub(r"<[^>]+>", "", name_match.group(1)).strip()
        else:
            # Check for known Orbit package names
            for known in known_packages:
                if known.lower() in block.lower():
                    name = known
                    break

        # Find quota
        rem_gb, tot_gb = parse_quota_string(block)

        # Find expiry
        exp_match = re.search(r"Berlaku\s+s[\./]?d[\.:]?\s*([^\n<]+)", block, re.IGNORECASE)
        expiry_raw = exp_match.group(0).strip() if exp_match else ""
        if not expiry_raw:
            # Try date pattern directly
            d_match = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", block)
            if d_match:
                expiry_raw = d_match.group(1)

        exp_str, days_left = parse_expiry_date(expiry_raw, now=now)

        if tot_gb > 0 or exp_str:
            packages.append(
                OrbitPackage(
                    name=name,
                    remaining_gb=rem_gb,
                    total_gb=tot_gb,
                    expiry_str=exp_str,
                    days_left=days_left,
                )
            )

    return packages


class OrbitScraper:
    """Headless web automation scraper for MyOrbit portal."""

    def __init__(
        self,
        portal_url: str = DEFAULT_ORBIT_PORTAL_URL,
        headless: bool = True,
        timeout_seconds: int = 30,
    ) -> None:
        self.portal_url = portal_url
        self.headless = headless
        self.timeout_seconds = timeout_seconds

    def scrape_modem(
        self,
        target: OrbitModem,
        driver: Any | None = None,
        now: datetime | None = None,
    ) -> OrbitModemStatus:
        """Scrape quota for a single Orbit modem.

        If target.imei_valid is False, immediately returns IMEI_PENDING status
        without touching the portal.
        """
        now_dt = now or datetime.now(UTC)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        if not target.imei_valid:
            logger.info(
                "Modem %s has invalid/pending IMEI (%s) -> IMEI_PENDING",
                target.no,
                target.imei,
            )
            return OrbitModemStatus(
                target=target,
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str=None,
                earliest_days_left=None,
                last_scraped_at=now_str,
                error="IMEI_PENDING",
            )

        if target.status == "EX_CUSTOMER":
            logger.info("Modem %s is EX_CUSTOMER -> returning archived status", target.no)
            return OrbitModemStatus(
                target=target,
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str=None,
                earliest_days_left=None,
                last_scraped_at=now_str,
                error="EX_CUSTOMER",
            )

        if driver is None:
            # Running without a live driver, return clean pending/mock or handle via driver
            logger.debug("No live WebDriver supplied for modem %s", target.no)
            return OrbitModemStatus(
                target=target,
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str=None,
                earliest_days_left=None,
                last_scraped_at=now_str,
                error="DRIVER_UNAVAILABLE",
            )

        return self._scrape_with_driver(driver, target, now_dt)

    def _scrape_with_driver(
        self,
        driver: Any,
        target: OrbitModem,
        now_dt: datetime,
    ) -> OrbitModemStatus:
        """Perform the multi-step browser automation on MyOrbit."""
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        try:
            # Step 1: Navigate to input page
            driver.get(self.portal_url)

            # Lazy import selenium helpers
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as ec
            from selenium.webdriver.support.ui import WebDriverWait

            wait = WebDriverWait(driver, self.timeout_seconds)

            # Step 2: Fill Phone and IMEI
            phone_input = wait.until(
                ec.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "input[name='phone'], input[name='msisdn'], #nomor_modem",
                ))
            )
            phone_input.clear()
            phone_input.send_keys(target.phone)

            imei_input = driver.find_element(By.CSS_SELECTOR, "input[name='imei'], #nomor_imei")
            imei_input.clear()
            imei_input.send_keys(target.imei)

            submit_btn = driver.find_element(
                By.XPATH, "//button[contains(normalize-space(), 'Lanjutkan') or @type='submit']"
            )
            submit_btn.click()

            # Step 3: Wait for registration/verification landing
            cek_kuota_xpath = (
                "//button[contains(normalize-space(), 'Cek Kuota')] | "
                "//a[contains(normalize-space(), 'Cek Kuota')]"
            )
            wait.until(ec.presence_of_element_located((By.XPATH, cek_kuota_xpath)))
            cek_kuota_btn = driver.find_element(By.XPATH, cek_kuota_xpath)
            cek_kuota_btn.click()

            # Step 4: Extract Total Quota from prabayar-info-modem
            wait.until(
                ec.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(), 'GB') or contains(text(), 'MB')]")
                )
            )
            page_text = driver.page_source
            total_rem, total_quota = parse_quota_string(page_text)
            multimedia = "multimedia" in page_text.lower()

            # Step 5: Click "Lihat Detail" to get multi-package breakdown
            detail_xpath = (
                "//button[contains(normalize-space(), 'Lihat Detail')] | "
                "//a[contains(normalize-space(), 'Lihat Detail')]"
            )
            detail_buttons = driver.find_elements(By.XPATH, detail_xpath)
            packages: list[OrbitPackage] = []
            if detail_buttons:
                detail_buttons[0].click()
                wait.until(
                    ec.presence_of_element_located(
                        (By.XPATH, "//*[contains(text(), 'Berlaku') or contains(text(), 's.d')]")
                    )
                )
                packages = parse_package_cards_html(driver.page_source, now=now_dt)

            if not packages and total_quota > 0:
                # Default single package if detailed breakdown was unavailable
                packages = [
                    OrbitPackage(
                        name="Internet Orbit",
                        remaining_gb=total_rem,
                        total_gb=total_quota,
                        expiry_str="",
                        days_left=None,
                    )
                ]

            earliest_str, earliest_days = self._calculate_earliest_expiry(packages)

            return OrbitModemStatus(
                target=target,
                total_remaining_gb=total_rem,
                total_quota_gb=total_quota,
                multimedia_active=multimedia,
                packages=packages,
                earliest_expiry_str=earliest_str,
                earliest_days_left=earliest_days,
                last_scraped_at=now_str,
                error=None,
            )
        except Exception as exc:
            logger.warning("Scrape error on modem %s (%s): %s", target.no, target.phone, exc)
            return OrbitModemStatus(
                target=target,
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                multimedia_active=False,
                packages=[],
                earliest_expiry_str=None,
                earliest_days_left=None,
                last_scraped_at=now_str,
                error=str(exc),
            )

    @staticmethod
    def _calculate_earliest_expiry(packages: list[OrbitPackage]) -> tuple[str | None, int | None]:
        """Find the earliest package expiration date and remaining days."""
        valid_packages = [p for p in packages if p.days_left is not None]
        if not valid_packages:
            return None, None

        # Min by days_left
        earliest = min(
            valid_packages,
            key=lambda p: p.days_left if p.days_left is not None else 999999,
        )
        return earliest.expiry_str, earliest.days_left
