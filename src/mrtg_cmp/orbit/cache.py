"""Telkomsel Orbit cache storage engine.

Persists scraped modem status atomically to data/orbit_cache/modems.json.
Provides thread-safe atomic reading and writing with graceful fallback.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from mrtg_cmp.orbit.scraper import OrbitModemStatus, OrbitPackage
from mrtg_cmp.orbit.targets import OrbitModem

logger = logging.getLogger("mrtg_cmp.orbit.cache")

DEFAULT_CACHE_DIR = Path("data") / "orbit_cache"
DEFAULT_CACHE_FILE = DEFAULT_CACHE_DIR / "modems.json"


class OrbitCache:
    """Thread-safe JSON cache manager for Orbit modem statuses."""

    def __init__(self, cache_file: Path | str | None = None) -> None:
        self.cache_file = Path(cache_file) if cache_file else DEFAULT_CACHE_FILE
        self._lock = threading.RLock()

    def is_cached(self) -> bool:
        """Check if cache file exists and is non-empty."""
        with self._lock:
            return self.cache_file.is_file() and self.cache_file.stat().st_size > 0

    def load(self) -> list[OrbitModemStatus] | None:
        """Load modem statuses from JSON cache."""
        with self._lock:
            if not self.is_cached():
                return None
            try:
                content = self.cache_file.read_text(encoding="utf-8")
                raw = json.loads(content)
                if not isinstance(raw, list):
                    logger.warning("Invalid cache structure in %s: not a list", self.cache_file)
                    return None
                return [OrbitModemStatus.from_dict(item) for item in raw if isinstance(item, dict)]
            except Exception as exc:
                logger.warning("Failed loading Orbit cache %s: %s", self.cache_file, exc)
                return None

    def save(self, modems_status: list[OrbitModemStatus]) -> None:
        """Atomically persist modem statuses to JSON cache."""
        with self._lock:
            parent = self.cache_file.parent
            parent.mkdir(parents=True, exist_ok=True)

            data = [status.as_dict() for status in modems_status]
            json_text = json.dumps(data, indent=2, ensure_ascii=False)

            temp_fd, temp_path = tempfile.mkstemp(
                dir=str(parent),
                prefix="orbit_cache_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                    f.write(json_text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.cache_file)
                logger.info("Persisted %d Orbit modems to %s", len(modems_status), self.cache_file)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

    def get_or_seed(self, catalog: list[OrbitModem]) -> list[OrbitModemStatus]:
        """Return cached status if present, otherwise seed demo/mock status and cache it."""
        cached = self.load()
        if cached:
            return cached

        seeded = self.generate_demo_status(catalog)
        try:
            self.save(seeded)
        except Exception as exc:
            logger.warning("Failed seeding Orbit cache: %s", exc)
        return seeded

    @staticmethod
    def generate_demo_status(catalog: list[OrbitModem]) -> list[OrbitModemStatus]:
        """Generate realistic mock status data for the catalog."""
        now_dt = datetime.now(UTC)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        demo_list: list[OrbitModemStatus] = []
        for m in catalog:
            if not m.imei_valid:
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=0.0,
                        total_quota_gb=0.0,
                        multimedia_active=False,
                        packages=[],
                        earliest_expiry_str=None,
                        earliest_days_left=None,
                        last_scraped_at=now_str,
                        error="IMEI_PENDING",
                    )
                )
            elif m.status == "EX_CUSTOMER":
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=0.0,
                        total_quota_gb=0.0,
                        multimedia_active=False,
                        packages=[],
                        earliest_expiry_str=None,
                        earliest_days_left=None,
                        last_scraped_at=now_str,
                        error="EX_CUSTOMER",
                    )
                )
            elif m.no == 2:  # Sikidang Room
                p1 = OrbitPackage("Internet Orbit 30GB", 25.0, 25.0, "08 Oct 2026", 7)
                p2 = OrbitPackage("Kuota FantaSIX 5GB", 3.56, 5.0, "22 Oct 2026", 21)
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=28.56,
                        total_quota_gb=30.0,
                        multimedia_active=True,
                        packages=[p1, p2],
                        earliest_expiry_str="08 Oct 2026",
                        earliest_days_left=7,
                        last_scraped_at=now_str,
                        error=None,
                    )
                )
            elif m.no == 4:  # Nias Room H4
                p1 = OrbitPackage("Internet Orbit 50GB", 40.0, 45.0, "15 Oct 2026", 14)
                p2 = OrbitPackage("Bonus Orbit 5GB", 5.2, 5.0, "28 Oct 2026", 27)
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=45.2,
                        total_quota_gb=50.0,
                        multimedia_active=True,
                        packages=[p1, p2],
                        earliest_expiry_str="15 Oct 2026",
                        earliest_days_left=14,
                        last_scraped_at=now_str,
                        error=None,
                    )
                )
            elif m.no == 5:  # Lounge BOD H4 - Low quota & expiring soon
                p1 = OrbitPackage("Internet Orbit 100GB", 12.4, 100.0, "04 Oct 2026", 3)
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=12.4,
                        total_quota_gb=100.0,
                        multimedia_active=False,
                        packages=[p1],
                        earliest_expiry_str="04 Oct 2026",
                        earliest_days_left=3,
                        last_scraped_at=now_str,
                        error=None,
                    )
                )
            elif m.no == 7:  # IDLE modem - Low quota (< 10%)
                p1 = OrbitPackage("Internet Orbit 30GB", 2.1, 30.0, "03 Oct 2026", 2)
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=2.1,
                        total_quota_gb=30.0,
                        multimedia_active=False,
                        packages=[p1],
                        earliest_expiry_str="03 Oct 2026",
                        earliest_days_left=2,
                        last_scraped_at=now_str,
                        error=None,
                    )
                )
            else:  # Other valid modems
                quota = 30.0 if m.status == "ACTIVE" else 15.0
                rem = round(quota * 0.85, 2)
                p1 = OrbitPackage(f"Internet Orbit {int(quota)}GB", rem, quota, "25 Oct 2026", 24)
                demo_list.append(
                    OrbitModemStatus(
                        target=m,
                        total_remaining_gb=rem,
                        total_quota_gb=quota,
                        multimedia_active=False,
                        packages=[p1],
                        earliest_expiry_str="25 Oct 2026",
                        earliest_days_left=24,
                        last_scraped_at=now_str,
                        error=None,
                    )
                )

        return demo_list
