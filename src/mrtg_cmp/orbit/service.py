"""Live Telkomsel Orbit background synchronization engine and WebDriver builder."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mrtg_cmp.orbit.cache import OrbitCache
from mrtg_cmp.orbit.scraper import OrbitModemStatus, OrbitScraper
from mrtg_cmp.orbit.targets import OrbitModem

if TYPE_CHECKING:
    from mrtg_cmp.config import Settings

logger = logging.getLogger("mrtg_cmp.orbit.service")


def build_orbit_driver(headless: bool = True) -> Any:
    """Set up and return a headless Chrome Selenium WebDriver instance for Orbit scraping."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
    except ImportError:  # pragma: no cover - optional runtime dependency
        logger.error("selenium is not installed; unable to build Orbit Chrome driver")
        return None

    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    try:
        return webdriver.Chrome(options=options)
    except Exception as exc:
        logger.error("Failed to start Chrome driver for Orbit scraper: %s", exc)
        return None


class OrbitService:
    """Service to coordinate live Orbit scraping, persistence, and background sync."""

    def __init__(
        self,
        scraper: OrbitScraper | None = None,
        headless: bool = True,
    ) -> None:
        self.scraper = scraper or OrbitScraper(headless=headless)
        self.headless = headless
        self._lock = threading.Lock()
        self._is_syncing = False

    @property
    def is_syncing(self) -> bool:
        """Return True if a sync operation is currently running."""
        with self._lock:
            return self._is_syncing

    def sync_all(
        self,
        catalog: list[OrbitModem],
        cache: OrbitCache | None = None,
        driver: Any | None = None,
    ) -> list[OrbitModemStatus]:
        """Synchronously scrape every modem in the catalog and update cache.

        Handles modems with invalid IMEIs or EX_CUSTOMER status immediately
        without launching a browser. Launches a shared headless Chrome driver
        for active valid modems, and persists all statuses atomically to cache.
        """
        with self._lock:
            already_syncing = self._is_syncing
            self._is_syncing = True

        own_driver = None
        statuses: list[OrbitModemStatus] = []
        try:
            valid_targets = [m for m in catalog if m.imei_valid and m.status != "EX_CUSTOMER"]
            if driver is None and valid_targets:
                own_driver = build_orbit_driver(headless=self.headless)

            active_driver = driver or own_driver

            for target in catalog:
                if not target.imei_valid or target.status == "EX_CUSTOMER":
                    st = self.scraper.scrape_modem(target)
                else:
                    st = self.scraper.scrape_modem(target, driver=active_driver)
                statuses.append(st)

            if cache is not None:
                cache.save(statuses)

            return statuses
        finally:
            if own_driver is not None:
                try:
                    own_driver.quit()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug("Failed quitting Orbit driver: %s", exc)
            with self._lock:
                if not already_syncing:
                    self._is_syncing = False

    def sync_in_background(
        self,
        catalog: list[OrbitModem],
        cache: OrbitCache | None = None,
    ) -> bool:
        """Run sync_all in a background thread if not already running.

        Returns True if a new background sync was started, False if already syncing.
        """
        with self._lock:
            if self._is_syncing:
                logger.info("Orbit sync is already in progress; skipping duplicate trigger")
                return False
            self._is_syncing = True

        def _worker() -> None:
            try:
                self.sync_all(catalog, cache)
            except Exception as exc:
                logger.exception("Unexpected error during background Orbit sync: %s", exc)
            finally:
                with self._lock:
                    self._is_syncing = False

        thread = threading.Thread(target=_worker, name="OrbitSyncWorker", daemon=True)
        thread.start()
        return True


def _wait(seconds: int) -> None:
    """Interruptible sleep so SIGTERM/SIGINT is handled promptly."""
    threading.Event().wait(seconds)


def install_shutdown_handlers(stop_event: threading.Event) -> None:
    """Ask the Orbit scrape loop to stop on SIGINT/SIGTERM."""

    def _handler(_signum: int, _frame: Any) -> None:
        logger.info("Orbit shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):  # pragma: no cover - non-main thread
            logger.debug("Could not install handler for %s", sig)


class OrbitDaemon:
    """Continuous scrape loop with graceful shutdown for Telkomsel Orbit modems."""

    def __init__(
        self,
        service: OrbitService,
        catalog_loader: Callable[[], list[OrbitModem]],
        cache: OrbitCache,
        interval_seconds: int = 1800,
        sleeper: Callable[[int], None] | None = None,
    ) -> None:
        self.service = service
        self.catalog_loader = catalog_loader
        self.cache = cache
        self.interval_seconds = max(1, interval_seconds)
        self._sleeper = sleeper

    def _sleep(self, seconds: int, stop_event: threading.Event | None = None) -> None:
        if self._sleeper is not None:
            self._sleeper(seconds)
        elif stop_event is not None:
            stop_event.wait(seconds)
        else:
            _wait(seconds)

    def install_signal_handlers(self, stop_event: threading.Event) -> None:
        """Ask the loop to stop on SIGINT/SIGTERM (best effort on non-POSIX)."""
        install_shutdown_handlers(stop_event)

    def run(
        self,
        stop_event: threading.Event | None = None,
        max_rounds: int | None = None,
    ) -> int:
        """Run scrape rounds until stopped. Returns the number of completed rounds."""
        stop = stop_event or threading.Event()
        completed = 0
        while not stop.is_set():
            if max_rounds is not None and completed >= max_rounds:
                break
            try:
                catalog = self.catalog_loader()
                statuses = self.service.sync_all(catalog, self.cache)
                completed += 1
                ok = sum(1 for s in statuses if not s.error)
                logger.info(
                    "Orbit daemon round complete: %s/%s modems updated",
                    ok,
                    len(statuses),
                )
            except Exception as exc:
                logger.error("Orbit daemon round failed: %s", exc)
                completed += 1

            if max_rounds is not None and completed >= max_rounds:
                break
            if stop.is_set():
                break
            self._sleep(self.interval_seconds, stop)

        logger.info("Orbit daemon stopped after %s round(s)", completed)
        return completed


def build_orbit_daemon_from_settings(config: Settings | None = None) -> OrbitDaemon:
    """Assemble a fully wired Orbit daemon from settings."""
    from mrtg_cmp.config import settings as global_settings
    from mrtg_cmp.orbit.targets import resolve_orbit_catalog

    cfg = config or global_settings
    service = OrbitService(headless=True)

    def catalog_loader() -> list[OrbitModem]:
        return resolve_orbit_catalog(cfg.orbit_catalog_file)

    cache = OrbitCache(cfg.orbit_cache_dir / "modems.json")
    return OrbitDaemon(
        service=service,
        catalog_loader=catalog_loader,
        cache=cache,
        interval_seconds=cfg.orbit_sync_interval_seconds,
    )


__all__ = [
    "OrbitDaemon",
    "OrbitService",
    "build_orbit_daemon_from_settings",
    "build_orbit_driver",
    "install_shutdown_handlers",
]
