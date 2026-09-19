"""Live Telkomsel Orbit background synchronization engine and WebDriver builder."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mrtg_cmp.orbit.burn_rate import QuotaBurnRateTracker, send_burn_alert
from mrtg_cmp.orbit.cache import OrbitCache
from mrtg_cmp.orbit.scraper import (
    NO_PACKAGE_EXPIRY_STR,
    OrbitModemStatus,
    OrbitScraper,
)
from mrtg_cmp.orbit.targets import OrbitModem
from mrtg_cmp.scrape_alerts import (
    ALERT_STATE_FILENAME,
    SERVICE_ORBIT,
    AlertSender,
    FailureStreakTracker,
    orbit_failures,
    orbit_identifier,
    send_scrape_failure_alert,
)

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

    cache_path = os.environ.setdefault(
        "SE_CACHE_PATH", str(Path(tempfile.gettempdir()) / "selenium")
    )
    try:
        Path(cache_path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Selenium cache path is not writable: %s", exc)
        return None

    options = ChromeOptions()
    profile_dir = tempfile.mkdtemp(prefix="mrtg-cmp-orbit-chrome-")
    options.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    try:
        driver: Any = webdriver.Chrome(options=options)
        driver._orbit_profile_dir = profile_dir
        return driver
    except Exception as exc:
        shutil.rmtree(profile_dir, ignore_errors=True)
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
        self._sync_state = "idle"
        self._sync_error: str | None = None

    @property
    def sync_status(self) -> dict[str, str | None]:
        with self._lock:
            return {"state": self._sync_state, "error": self._sync_error}

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
        """Scrape every valid-IMEI modem, skip only pending IMEIs, and update cache."""
        with self._lock:
            already_syncing = self._is_syncing
            self._is_syncing = True

        own_driver = None
        statuses: list[OrbitModemStatus] = []
        try:
            valid_targets = [m for m in catalog if m.imei_valid]
            if driver is None and valid_targets:
                own_driver = build_orbit_driver(headless=self.headless)

            active_driver = driver or own_driver

            for target in catalog:
                if not target.imei_valid:
                    st = self.scraper.scrape_modem(target)
                else:
                    st = self.scraper.scrape_modem(target, driver=active_driver)
                statuses.append(st)

            if cache is not None:
                self._save_cache_merging_errors(cache, statuses)

            return statuses
        finally:
            if own_driver is not None:
                profile_dir = getattr(own_driver, "_orbit_profile_dir", None)
                try:
                    own_driver.quit()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug("Failed quitting Orbit driver: %s", exc)
                finally:
                    if profile_dir:
                        shutil.rmtree(profile_dir, ignore_errors=True)
            with self._lock:
                if not already_syncing:
                    self._is_syncing = False

    def _save_cache_merging_errors(
        self, cache: OrbitCache, statuses: list[OrbitModemStatus]
    ) -> None:
        """Persist scraped statuses, retaining cached quota for failed rounds.

        Retention only fires when the round itself looks broken: any modem hit
        DRIVER_UNAVAILABLE, or every modem that reported quota came back 0.0 GB
        while the cache already holds valid numbers. A modem that legitimately
        has no active data package (expiry marker "No active package") really
        does hold 0.0 GB, so its fresh status is saved instead of reverting to
        stale non-zero quota.
        """
        previous = cache.load()
        prev_by_no: dict[int, OrbitModemStatus] = {}
        if previous:
            for st in previous:
                prev_by_no[st.target.no] = st

        has_driver_unavailable = any(
            s.error == "DRIVER_UNAVAILABLE" for s in statuses
        )
        # Zero-quota readings from modems that openly report no package are
        # trustworthy; only the rest of the round can be judged all-zero.
        no_package = {
            s.target.no for s in statuses if s.earliest_expiry_str == NO_PACKAGE_EXPIRY_STR
        }
        reported = [s for s in statuses if s.target.no not in no_package]
        all_zero = bool(reported) and all(
            s.total_remaining_gb == 0.0 and s.total_quota_gb == 0.0 for s in reported
        )
        cache_has_valid_quota = any(
            s.total_quota_gb > 0.0 or s.total_remaining_gb > 0.0
            for s in (previous or [])
        )
        round_anomalous = has_driver_unavailable or (all_zero and cache_has_valid_quota)

        to_save: list[OrbitModemStatus] = []
        retained = 0
        for st in statuses:
            prior = prev_by_no.get(st.target.no)
            failed_driver = st.error == "DRIVER_UNAVAILABLE"
            empty_over_valid = (
                round_anomalous
                and not st.error
                and st.target.no not in no_package
                and st.total_remaining_gb == 0.0
                and st.total_quota_gb == 0.0
                and prior is not None
                and (prior.total_quota_gb > 0.0 or prior.total_remaining_gb > 0.0)
            )
            keep_existing = (
                st.error == "IMEI_PENDING"
                or failed_driver
                or empty_over_valid
            )
            if keep_existing and prior is not None:
                # Refresh the target reference but keep cached quota data.
                prior.target = st.target
                to_save.append(prior)
                retained += 1
            else:
                to_save.append(st)

        if retained:
            reasons = []
            if has_driver_unavailable:
                reasons.append("DRIVER_UNAVAILABLE")
            if all_zero and cache_has_valid_quota:
                reasons.append("all-zero overwrite guard")
            logger.warning(
                "Retained existing cache for %d modems due to %s",
                retained,
                " or ".join(reasons) if reasons else "failure",
            )

        cache.save(to_save)

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
            self._sync_state = "queued"
            self._sync_error = None

        def _worker() -> None:
            with self._lock:
                self._sync_state = "running"
            try:
                self.sync_all(catalog, cache)
                with self._lock:
                    self._sync_state = "done"
            except Exception as exc:
                logger.exception("Unexpected error during background Orbit sync: %s", exc)
                with self._lock:
                    self._sync_state = "error"
                    self._sync_error = str(exc)
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
        interval_seconds: int = 300,
        sleeper: Callable[[int], None] | None = None,
        alerts: AlertSender | None = None,
        streaks: FailureStreakTracker | None = None,
    ) -> None:
        self.service = service
        self.catalog_loader = catalog_loader
        self.cache = cache
        self.interval_seconds = max(1, interval_seconds)
        self._sleeper = sleeper
        self._alerts = alerts or send_scrape_failure_alert
        self._streaks = streaks or FailureStreakTracker(
            cache.cache_file.parent / ALERT_STATE_FILENAME
        )
        self._burn_tracker = QuotaBurnRateTracker(
            cache.cache_file.parent / "burn_rate_history.json"
        )

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
                pending = sum(1 for s in statuses if s.error == "IMEI_PENDING")
                failed = sum(1 for s in statuses if s.error and s.error != "IMEI_PENDING")
                scraped = len(statuses) - pending - failed
                logger.info(
                    "Orbit daemon round complete: %s scraped, %s IMEI_PENDING skipped, "
                    "%s errors (%s total)",
                    scraped,
                    pending,
                    failed,
                    len(statuses),
                )
                self._alert_on_failures(statuses)
                self._alert_on_burn_rate(statuses)
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

    def _alert_on_failures(self, statuses: list[OrbitModemStatus]) -> None:
        """Send one aggregate alert when a modem has failed too many rounds running.

        Only a modem that crosses the consecutive-failure threshold is reported,
        and only once until it succeeds again, mirroring the Netcare daemon. A
        round of pending IMEIs is a normal skip: it stays silent and, because
        such a modem is neither a success nor a failure, it neither advances nor
        clears the streak it had already earned. Containment mirrors the Netcare
        daemon too: a gateway fault is an alert problem, not a reason for the
        scrape round to be recorded as failed.
        """

        failures = orbit_failures(statuses)
        succeeded = [
            orbit_identifier(s)
            for s in statuses
            if not getattr(s, "error", None)
        ]
        # A modem still waiting on its IMEI is neither healthy nor broken, so it
        # is reported as skipped: the round spoke about it, and that keeps a
        # live failure streak from being mistaken for a modem that left the
        # catalog.
        skipped = [
            orbit_identifier(s)
            for s in statuses
            if getattr(s, "error", None) == "IMEI_PENDING"
        ]
        crossing = self._streaks.record_round(SERVICE_ORBIT, failures, succeeded, skipped)
        if not crossing:
            return
        try:
            self._alerts(SERVICE_ORBIT, crossing, len(statuses))
        except Exception as exc:
            logger.warning("Orbit scrape failure alert could not be delivered: %s", exc)

    def _alert_on_burn_rate(self, statuses: list[OrbitModemStatus]) -> None:
        """Send WhatsApp alert for active modems that hit CRITICAL burn-rate."""
        try:
            self._burn_tracker.record_statuses(statuses)
            pending = self._burn_tracker.critical_alerts_pending()
            for result in pending:
                try:
                    send_burn_alert(result)
                except Exception as exc:
                    logger.warning(
                        "Orbit burn rate alert could not be delivered for modem %s: %s",
                        result.modem_no,
                        exc,
                    )
            self._burn_tracker.mark_alerted(pending)
        except Exception as exc:
            logger.warning("Orbit burn rate evaluation failed: %s", exc)


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
