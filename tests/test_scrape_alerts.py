"""Tests for failure-only WhatsApp alerts on the Netcare and Orbit daemons.

A round that succeeds end to end must be silent, a round with any real failure
must produce exactly one aggregate message, and a broken gateway must never
take a daemon down. Every test drives a public daemon/service seam and patches
the sender, so nothing here can reach a real WhatsApp gateway.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mrtg_cmp.netcare.scraper import NetcareCache, ScrapeOutcome
from mrtg_cmp.netcare.service import NetcareDaemon, NetcareService
from mrtg_cmp.netcare.targets import NetcareTarget, NetcareTargetType
from mrtg_cmp.orbit import service as orbit_service
from mrtg_cmp.orbit.cache import OrbitCache
from mrtg_cmp.orbit.scraper import OrbitModemStatus
from mrtg_cmp.orbit.service import OrbitDaemon, OrbitService
from mrtg_cmp.orbit.targets import OrbitModem
from mrtg_cmp.scrape_alerts import (
    ALERT_FAILURE_THRESHOLD,
    ALERT_STATE_FILENAME,
    SERVICE_NETCARE,
    SERVICE_ORBIT,
    FailureStreakTracker,
    ScrapeFailure,
    format_scrape_failure_alert,
    netcare_failures,
    orbit_failures,
    sanitize_reason,
    send_scrape_failure_alert,
)

# --- doubles ---------------------------------------------------------------


class RecordingAlerter:
    """Alert seam double that records every message a daemon emits."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, list[ScrapeFailure], int]] = []
        self._error = error

    def __call__(self, service: str, failures: list[ScrapeFailure], total: int) -> bool:
        self.calls.append((service, list(failures), total))
        if self._error is not None:
            raise self._error
        return True

    @property
    def messages(self) -> list[str]:
        return [format_scrape_failure_alert(s, f, t) for s, f, t in self.calls]


class ListHandler(logging.Handler):
    """Collect WARNING-and-above messages for assertions."""

    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record.getMessage())


class warnings:
    """Context manager capturing the ``mrtg_cmp`` logger at WARNING and above."""

    def __enter__(self) -> list[str]:
        self._messages: list[str] = []
        self._handler = ListHandler(self._messages)
        logger = logging.getLogger("mrtg_cmp")
        self._logger, self._level = logger, logger.level
        logger.addHandler(self._handler)
        logger.setLevel(logging.WARNING)
        return self._messages

    def __exit__(self, *_exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)


def _target(index: int) -> NetcareTarget:
    return NetcareTarget(
        target=f"target-{index:03d}",
        name=f"Branch {index:03d}",
        type=NetcareTargetType.SID,
        address="Address withheld",
        region="CGK",
    )


def _modem(index: int, *, valid: bool = True) -> OrbitModem:
    imei = f"{869338000000000 + index}" if valid else "PENDING"
    return OrbitModem(
        no=index,
        imei=imei,
        phone="628000000000",
        location="Jakarta",
        ssid="tselhome-test",
        status="ACTIVE",
        imei_valid=valid,
    )


def _status(modem: OrbitModem, error: str | None = None) -> OrbitModemStatus:
    return OrbitModemStatus(
        target=modem, error=error, last_scraped_at="2026-10-06 10:00:00"
    )


def _cache_image(cache: NetcareCache, target: str) -> None:
    """Write a previous good capture so the cache reports a stale target."""

    path = cache.image_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    cache.mark_ok(target)


class StubScraper:
    """Orbit scraper double returning canned statuses per modem number."""

    def __init__(self, errors: dict[int, str]) -> None:
        self._errors = errors

    def scrape_modem(self, target: OrbitModem, driver: Any = None) -> OrbitModemStatus:
        error = self._errors.get(target.no)
        if target.imei_valid is False and error is None:
            error = "IMEI_PENDING"
        return _status(target, error)


def _netcare_daemon(
    cache: NetcareCache,
    outcomes: list[ScrapeOutcome],
    alerter: RecordingAlerter,
) -> NetcareDaemon:
    service = NetcareService(cache, [_target(i) for i in range(1, len(outcomes) + 1)])
    return NetcareDaemon(
        service=service,
        run_round=lambda: list(outcomes),
        interval_seconds=1,
        sleeper=lambda _seconds: None,
        alerts=alerter,
    )


def _orbit_daemon(
    catalog: list[OrbitModem],
    errors: dict[int, str],
    alerter: RecordingAlerter,
    tmp_path: Path,
    monkeypatch: Any,
) -> OrbitDaemon:
    # A real driver would launch Chrome; these tests only exercise the
    # round-completion seam, so the builder is stubbed out entirely.
    monkeypatch.setattr(orbit_service, "build_orbit_driver", lambda **_kw: None)
    service = OrbitService(scraper=StubScraper(errors))  # type: ignore[arg-type]
    return OrbitDaemon(
        service=service,
        catalog_loader=lambda: list(catalog),
        cache=OrbitCache(tmp_path / "modems.json"),
        interval_seconds=1,
        sleeper=lambda _seconds: None,
        alerts=alerter,
    )


class ScriptedScraper:
    """Orbit scraper double whose canned errors advance one round at a time.

    The daemon's sleeper hook runs between rounds, which is the only public
    seam at which a per-round script can be advanced.
    """

    def __init__(self, error_rounds: list[dict[int, str]]) -> None:
        self._rounds = error_rounds
        self._index = 0
        self.errors: dict[int, str] = dict(error_rounds[0])

    def advance(self) -> None:
        """Move to the next scripted round, holding the last one forever."""

        self._index = min(self._index + 1, len(self._rounds) - 1)
        self.errors = dict(self._rounds[self._index])

    def scrape_modem(self, target: OrbitModem, driver: Any = None) -> OrbitModemStatus:
        error = self.errors.get(target.no)
        if target.imei_valid is False and error is None:
            error = "IMEI_PENDING"
        return _status(target, error)


def _scripted_netcare_daemon(
    cache: NetcareCache,
    rounds: list[list[ScrapeOutcome]],
    alerter: RecordingAlerter,
    streaks: FailureStreakTracker | None = None,
) -> NetcareDaemon:
    """Daemon replaying a scripted outcome list per round."""

    service = NetcareService(cache, [_target(i) for i in range(1, 9)])
    script = list(rounds)

    def run_round() -> list[ScrapeOutcome]:
        return list(script.pop(0)) if len(script) > 1 else list(script[0])

    return NetcareDaemon(
        service=service,
        run_round=run_round,
        interval_seconds=1,
        sleeper=lambda _seconds: None,
        alerts=alerter,
        streaks=streaks,
    )


def _scripted_orbit_daemon(
    catalog: list[OrbitModem],
    error_rounds: list[dict[int, str]],
    alerter: RecordingAlerter,
    tmp_path: Path,
    monkeypatch: Any,
    streaks: FailureStreakTracker | None = None,
) -> OrbitDaemon:
    """Daemon replaying one canned error map per round."""

    monkeypatch.setattr(orbit_service, "build_orbit_driver", lambda **_kw: None)
    scraper = ScriptedScraper(error_rounds)
    service = OrbitService(scraper=scraper)  # type: ignore[arg-type]
    return OrbitDaemon(
        service=service,
        catalog_loader=lambda: list(catalog),
        cache=OrbitCache(tmp_path / "modems.json"),
        interval_seconds=1,
        sleeper=lambda _seconds: scraper.advance(),
        alerts=alerter,
        streaks=streaks,
    )


# --- formatting ------------------------------------------------------------


def test_sanitize_reason_flattens_and_truncates() -> None:
    """A multi-line driver trace must not become a multi-line alert."""

    reason = sanitize_reason("Graph did not render\n\n  details: " + "x" * 500)

    assert "\n" not in reason
    assert len(reason) <= 90
    assert reason.endswith("...")


def test_sanitize_reason_falls_back_when_empty() -> None:
    """A blank or missing reason still produces something an operator can read."""

    assert sanitize_reason(None) == "unknown error"
    assert sanitize_reason("   \n  ") == "unknown error"


def test_format_alert_names_service_targets_and_counts() -> None:
    """The aggregate message carries the service, each failure, and the count."""

    message = format_scrape_failure_alert(
        SERVICE_NETCARE,
        [
            ScrapeFailure(target="target-001", reason="portal timeout"),
            ScrapeFailure(target="target-002", reason="no graph rendered"),
        ],
        total=18,
        timestamp="2026-10-06 10:00:00",
    )

    assert SERVICE_NETCARE in message
    assert "target-001" in message
    assert "target-002" in message
    assert "portal timeout" in message
    assert "2 of 18" in message


def test_format_alert_marks_stale_cache_as_not_fresh() -> None:
    """A stale cache must never read as a fresh success."""

    message = format_scrape_failure_alert(
        SERVICE_NETCARE,
        [ScrapeFailure(target="target-001", reason="no graph", stale=True)],
        total=1,
        timestamp="2026-10-06 10:00:00",
    )

    assert "last good" in message.lower()


def test_format_alert_truncates_a_very_long_failure_list() -> None:
    """A whole-catalog outage still fits in one readable message."""

    failures = [
        ScrapeFailure(target=f"target-{i:03d}", reason="timeout") for i in range(1, 21)
    ]

    message = format_scrape_failure_alert(
        SERVICE_NETCARE, failures, total=20, timestamp="2026-10-06 10:00:00"
    )

    assert "target-001" in message
    assert "more" in message.lower()
    assert len(message) < 2000


# --- failure collection ----------------------------------------------------


def test_netcare_failures_counts_no_graph_and_error(tmp_path: Path) -> None:
    """no_graph and error are failures; ok is not."""

    cache = NetcareCache(tmp_path / "cache")
    outcomes = [
        ScrapeOutcome.from_ok("target-001"),
        ScrapeOutcome.from_no_graph("target-002", "no graph"),
        ScrapeOutcome.from_error("target-003", "portal timeout"),
    ]

    failures = netcare_failures(outcomes, cache)

    assert [f.target for f in failures] == ["target-002", "target-003"]


def test_netcare_failures_flags_a_stale_cached_image(tmp_path: Path) -> None:
    """A target that kept its previous good image is stale, not fresh."""

    cache = NetcareCache(tmp_path / "cache")
    _cache_image(cache, "target-001")

    failures = netcare_failures([ScrapeOutcome.from_error("target-001", "timeout")], cache)

    assert failures[0].stale is True


def test_orbit_failures_excludes_pending_imei() -> None:
    """IMEI_PENDING is an expected skip, not a failure."""

    statuses = [
        _status(_modem(1)),
        _status(_modem(2), "IMEI_PENDING"),
        _status(_modem(3), "quota lookup failed"),
    ]

    failures = orbit_failures(statuses)

    assert [f.target for f in failures] == ["869338000000003"]
    assert failures[0].reason == "quota lookup failed"


def test_orbit_failures_uses_phone_when_imei_is_absent() -> None:
    """A modem with no usable IMEI still needs an identifier in the alert."""

    modem = OrbitModem(no=4, imei="", phone="628000000009", location="Jakarta", ssid="x")

    failures = orbit_failures([_status(modem, "portal error")])

    assert failures[0].target == "628000000009"


# --- dispatch --------------------------------------------------------------


def test_send_alert_is_skipped_when_disabled(monkeypatch: Any) -> None:
    """Nothing is dispatched while WA alerts are off."""

    from mrtg_cmp import config

    monkeypatch.setattr(config.settings, "wa_alert_enabled", False)
    sent: list[str] = []
    monkeypatch.setattr(
        "mrtg_cmp.scrape_alerts.send_whatsapp_message", lambda msg, **kw: sent.append(msg)
    )

    assert (
        send_scrape_failure_alert(SERVICE_NETCARE, [ScrapeFailure("target-001", "timeout")], 1)
        is False
    )
    assert sent == []


def test_send_alert_dispatches_when_enabled(monkeypatch: Any) -> None:
    """The shared notifier is the single dispatch path used."""

    from mrtg_cmp import config

    monkeypatch.setattr(config.settings, "wa_alert_enabled", True)
    sent: list[str] = []

    def record(msg: str, **_kwargs: Any) -> bool:
        sent.append(msg)
        return True

    monkeypatch.setattr("mrtg_cmp.scrape_alerts.send_whatsapp_message", record)

    assert (
        send_scrape_failure_alert(SERVICE_ORBIT, [ScrapeFailure("869338000000001", "timeout")], 1)
        is True
    )
    assert len(sent) == 1
    assert SERVICE_ORBIT in sent[0]


def test_send_alert_contains_a_raising_gateway(monkeypatch: Any) -> None:
    """A gateway blow-up is logged and reported, never propagated."""

    from mrtg_cmp import config

    monkeypatch.setattr(config.settings, "wa_alert_enabled", True)

    def boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr("mrtg_cmp.scrape_alerts.send_whatsapp_message", boom)

    with warnings() as records:
        assert (
            send_scrape_failure_alert(
                SERVICE_NETCARE, [ScrapeFailure("target-001", "timeout")], 1
            )
            is False
        )

    assert any("alert" in record.lower() for record in records)


def test_send_alert_reports_a_rejected_message(monkeypatch: Any) -> None:
    """A gateway that answers non-200 is a delivery failure worth logging."""

    from mrtg_cmp import config

    monkeypatch.setattr(config.settings, "wa_alert_enabled", True)
    monkeypatch.setattr("mrtg_cmp.scrape_alerts.send_whatsapp_message", lambda *a, **k: False)

    with warnings() as records:
        assert (
            send_scrape_failure_alert(
                SERVICE_ORBIT, [ScrapeFailure("869338000000001", "timeout")], 1
            )
            is False
        )

    assert any("alert" in record.lower() for record in records)


# --- Netcare daemon seam ---------------------------------------------------


def test_netcare_daemon_stays_silent_on_a_clean_round(tmp_path: Path) -> None:
    """An all-ok round sends nothing at all."""

    alerter = RecordingAlerter()
    daemon = _netcare_daemon(
        NetcareCache(tmp_path / "cache"),
        [ScrapeOutcome.from_ok(f"target-{i:03d}") for i in range(1, 4)],
        alerter,
    )

    assert daemon.run(max_rounds=1) == 1
    assert alerter.calls == []


def test_netcare_daemon_sends_one_aggregate_alert_per_round(tmp_path: Path) -> None:
    """Three failed rounds stay quiet; the fourth produces one message naming all three."""

    alerter = RecordingAlerter()
    daemon = _netcare_daemon(
        NetcareCache(tmp_path / "cache"),
        [
            ScrapeOutcome.from_ok("target-001"),
            ScrapeOutcome.from_no_graph("target-002", "no graph rendered"),
            ScrapeOutcome.from_error("target-003", "portal timeout"),
            ScrapeOutcome.from_error("target-004", "graph did not render"),
        ],
        alerter,
    )

    assert daemon.run(max_rounds=4) == 4
    assert len(alerter.calls) == 1

    service, failures, total = alerter.calls[0]
    assert service == SERVICE_NETCARE
    assert total == 4
    assert [f.target for f in failures] == ["target-002", "target-003", "target-004"]

    message = alerter.messages[0]
    for target in ("target-002", "target-003", "target-004"):
        assert target in message
    assert "target-001" not in message


def test_netcare_daemon_alert_identifies_a_stale_cache(tmp_path: Path) -> None:
    """A target that fell back to its previous image is labelled stale."""

    cache = NetcareCache(tmp_path / "cache")
    _cache_image(cache, "target-001")

    alerter = RecordingAlerter()
    daemon = _netcare_daemon(
        cache,
        [
            ScrapeOutcome.from_no_graph("target-001", "no graph"),
            ScrapeOutcome.from_ok("target-002"),
        ],
        alerter,
    )

    daemon.run(max_rounds=4)

    assert len(alerter.calls) == 1
    assert alerter.calls[0][1][0].stale is True
    assert "last good" in alerter.messages[0].lower()


def test_netcare_daemon_survives_a_raising_alerter(tmp_path: Path) -> None:
    """A broken alert path must not stop the daemon or lose the round."""

    alerter = RecordingAlerter(error=RuntimeError("gateway down"))
    daemon = _netcare_daemon(
        NetcareCache(tmp_path / "cache"),
        [ScrapeOutcome.from_ok("target-001"), ScrapeOutcome.from_error("target-002", "boom")],
        alerter,
    )

    with warnings() as records:
        assert daemon.run(max_rounds=4) == 4

    assert any("alert" in record.lower() for record in records)


def test_netcare_daemon_sends_no_alert_when_the_round_itself_raises(tmp_path: Path) -> None:
    """An aborted round is not a set of per-target failures, so it stays quiet."""

    alerter = RecordingAlerter()
    service = NetcareService(NetcareCache(tmp_path / "cache"), [_target(1)])

    def run_round() -> list[ScrapeOutcome]:
        raise RuntimeError("session expired")

    daemon = NetcareDaemon(
        service=service,
        run_round=run_round,
        interval_seconds=1,
        sleeper=lambda _seconds: None,
        alerts=alerter,
    )

    assert daemon.run(max_rounds=1) == 1
    assert alerter.calls == []


# --- Orbit daemon seam -----------------------------------------------------


def test_orbit_daemon_stays_silent_on_a_clean_round(tmp_path: Path, monkeypatch: Any) -> None:
    """Every modem scraped successfully means no message."""

    alerter = RecordingAlerter()
    daemon = _orbit_daemon([_modem(i) for i in range(1, 4)], {}, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=1) == 1
    assert alerter.calls == []


def test_orbit_daemon_ignores_a_round_of_only_pending_imeis(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Pending IMEIs are an expected skip and must not page anyone."""

    alerter = RecordingAlerter()
    catalog = [_modem(i, valid=False) for i in range(1, 4)]
    daemon = _orbit_daemon(catalog, {}, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=1) == 1
    assert alerter.calls == []


def test_orbit_daemon_sends_one_aggregate_alert_excluding_pending(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Four failed rounds alert once; the pending modem stays out of the message."""

    alerter = RecordingAlerter()
    catalog = [_modem(1), _modem(2, valid=False), _modem(3)]
    daemon = _orbit_daemon(catalog, {3: "quota lookup failed"}, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=4) == 4
    assert len(alerter.calls) == 1

    service, failures, total = alerter.calls[0]
    assert service == SERVICE_ORBIT
    assert total == 3
    assert [f.target for f in failures] == ["869338000000003"]
    assert "IMEI_PENDING" not in alerter.messages[0]


def test_orbit_daemon_survives_a_raising_alerter(tmp_path: Path, monkeypatch: Any) -> None:
    """An alert failure leaves the Orbit daemon running."""

    alerter = RecordingAlerter(error=RuntimeError("gateway down"))
    daemon = _orbit_daemon([_modem(1)], {1: "portal error"}, alerter, tmp_path, monkeypatch)

    with warnings() as records:
        assert daemon.run(max_rounds=4) == 4

    assert any("alert" in record.lower() for record in records)


def test_orbit_daemon_sends_no_alert_when_the_round_itself_raises(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An aborted sync is logged, not turned into a per-modem alert."""

    alerter = RecordingAlerter()
    service = OrbitService(scraper=StubScraper({}))  # type: ignore[arg-type]

    def catalog_loader() -> list[OrbitModem]:
        raise RuntimeError("catalog unreadable")

    daemon = OrbitDaemon(
        service=service,
        catalog_loader=catalog_loader,
        cache=OrbitCache(tmp_path / "modems.json"),
        interval_seconds=1,
        sleeper=lambda _seconds: None,
        alerts=alerter,
    )

    assert daemon.run(max_rounds=1) == 1
    assert alerter.calls == []


# --- consecutive-failure threshold -----------------------------------------


def test_netcare_stays_silent_below_the_threshold(tmp_path: Path) -> None:
    """Three consecutive failures are transient; nobody is paged yet."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), [failing] * 3, alerter)

    assert daemon.run(max_rounds=3) == 3
    assert alerter.calls == []


def test_netcare_alerts_on_the_fourth_consecutive_failure(tmp_path: Path) -> None:
    """The fourth consecutive failed round is the one that crosses the line."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), [failing] * 4, alerter)

    assert daemon.run(max_rounds=4) == 4
    assert len(alerter.calls) == 1

    service, failures, total = alerter.calls[0]
    assert service == SERVICE_NETCARE
    assert total == 1
    assert [f.target for f in failures] == ["target-001"]
    assert failures[0].streak == ALERT_FAILURE_THRESHOLD + 1


def test_netcare_does_not_repeat_the_alert_while_the_failure_continues(tmp_path: Path) -> None:
    """A latched target stays quiet no matter how long the outage runs."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), [failing] * 9, alerter)

    assert daemon.run(max_rounds=9) == 9
    assert len(alerter.calls) == 1


def test_netcare_rearms_the_alert_after_a_success_resets_the_streak(tmp_path: Path) -> None:
    """A fresh success clears both the counter and the latch."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    healthy = [ScrapeOutcome.from_ok("target-001")]
    rounds = [failing] * 4 + [healthy] + [failing] * 3
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), rounds, alerter)

    # Four failures cross the threshold once, then a success and three more
    # failures stay below it again.
    assert daemon.run(max_rounds=8) == 8
    assert len(alerter.calls) == 1


def test_orbit_alerts_on_the_fourth_consecutive_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Orbit uses the same threshold as Netcare."""

    alerter = RecordingAlerter()
    rounds = [{1: "portal error"}] * 4
    daemon = _scripted_orbit_daemon([_modem(1)], rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=4) == 4
    assert len(alerter.calls) == 1

    service, failures, total = alerter.calls[0]
    assert service == SERVICE_ORBIT
    assert total == 1
    assert [f.target for f in failures] == ["869338000000001"]


def test_orbit_stays_silent_below_the_threshold(tmp_path: Path, monkeypatch: Any) -> None:
    """Three consecutive modem failures do not page anyone."""

    alerter = RecordingAlerter()
    rounds = [{1: "portal error"}] * 3
    daemon = _scripted_orbit_daemon([_modem(1)], rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=3) == 3
    assert alerter.calls == []


def test_orbit_does_not_repeat_the_alert_while_the_failure_continues(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A latched modem stays quiet for the rest of the outage."""

    alerter = RecordingAlerter()
    rounds = [{1: "portal error"}] * 8
    daemon = _scripted_orbit_daemon([_modem(1)], rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=8) == 8
    assert len(alerter.calls) == 1


def test_orbit_pending_imei_never_advances_a_streak(tmp_path: Path, monkeypatch: Any) -> None:
    """An expected skip is neither a failure nor a reset of the counter."""

    alerter = RecordingAlerter()
    catalog = [_modem(1), _modem(2, valid=False)]
    # Modem 1 fails every round; modem 2 stays IMEI_PENDING throughout.
    rounds = [{1: "portal error"}] * 6
    daemon = _scripted_orbit_daemon(catalog, rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=6) == 6
    assert len(alerter.calls) == 1

    _, failures, _ = alerter.calls[0]
    assert [f.target for f in failures] == ["869338000000001"]
    assert "IMEI_PENDING" not in alerter.messages[0]


def test_netcare_and_orbit_streaks_are_tracked_separately(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two services failing at once must not share a counter or a latch."""

    netcare_alerter = RecordingAlerter()
    orbit_alerter = RecordingAlerter()
    streaks = FailureStreakTracker(tmp_path / "streaks.json")

    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    netcare = _scripted_netcare_daemon(
        NetcareCache(tmp_path / "netcare"), [failing] * 3, netcare_alerter, streaks
    )
    assert netcare.run(max_rounds=3) == 3

    orbit = _scripted_orbit_daemon(
        [_modem(1)],
        [{1: "portal error"}] * 3,
        orbit_alerter,
        tmp_path,
        monkeypatch,
        streaks,
    )
    assert orbit.run(max_rounds=3) == 3

    # Three rounds each: neither service has crossed yet.
    assert netcare_alerter.calls == []
    assert orbit_alerter.calls == []

    # The fourth failure crosses for Netcare only.
    more = _scripted_netcare_daemon(
        NetcareCache(tmp_path / "netcare"), [failing], netcare_alerter, streaks
    )
    assert more.run(max_rounds=1) == 1
    assert len(netcare_alerter.calls) == 1
    assert orbit_alerter.calls == []


def test_streak_state_file_stays_next_to_the_cache_and_holds_no_credentials(
    tmp_path: Path,
) -> None:
    """The persisted record carries target ids and counters, nothing sensitive."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    cache_dir = tmp_path / "cache"
    daemon = _scripted_netcare_daemon(NetcareCache(cache_dir), [failing] * 4, alerter)

    daemon.run(max_rounds=4)

    state_path = cache_dir / ALERT_STATE_FILENAME
    assert state_path.is_file()

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    entry = payload["services"][SERVICE_NETCARE]["target-001"]
    assert entry["consecutive_failures"] == ALERT_FAILURE_THRESHOLD + 1
    assert entry["alerted"] is True

    raw = state_path.read_text(encoding="utf-8").lower()
    for secret in ("password", "secret", "cookie", "token", "telkom_user", "otp"):
        assert secret not in raw


def test_corrupt_streak_state_starts_over_without_raising(tmp_path: Path) -> None:
    """A damaged state file must never take the scrape loop down."""

    state_path = tmp_path / "streaks.json"
    state_path.write_text("{not json", encoding="utf-8")
    failures = [ScrapeFailure(target="target-001", reason="portal timeout")]

    # The damaged file is read while the tracker is constructed, so the warning
    # is expected there rather than during the rounds.
    with warnings() as records:
        streaks = FailureStreakTracker(state_path)
        rounds = [
            streaks.record_round(SERVICE_NETCARE, failures, [])
            for _ in range(ALERT_FAILURE_THRESHOLD + 1)
        ]

    # The unreadable state is discarded rather than raised, and the counter
    # restarts from zero: the target alerts on exactly its own fourth failure.
    assert rounds[:ALERT_FAILURE_THRESHOLD] == [[], [], []]
    assert len(rounds[ALERT_FAILURE_THRESHOLD]) == 1
    assert rounds[ALERT_FAILURE_THRESHOLD][0].streak == ALERT_FAILURE_THRESHOLD + 1
    assert any("streak" in record.lower() for record in records)


def test_missing_streak_state_warns_and_starts_from_zero(tmp_path: Path) -> None:
    """No state file is the cold-start case, and it is announced rather than silent.

    A daemon started onto a live outage loses its counters when the file is
    absent, so it will alert again. That is the safe direction to fail in, but
    an operator should still be able to see it happen.
    """

    state_path = tmp_path / "streaks.json"
    assert not state_path.exists()
    failures = [ScrapeFailure(target="target-001", reason="portal timeout")]

    with warnings() as records:
        streaks = FailureStreakTracker(state_path)
        rounds = [
            streaks.record_round(SERVICE_NETCARE, failures, [])
            for _ in range(ALERT_FAILURE_THRESHOLD + 1)
        ]

    assert any("streak" in record.lower() for record in records)
    assert rounds[:ALERT_FAILURE_THRESHOLD] == [[], [], []]
    assert len(rounds[ALERT_FAILURE_THRESHOLD]) == 1
    assert rounds[ALERT_FAILURE_THRESHOLD][0].streak == ALERT_FAILURE_THRESHOLD + 1


def test_streak_tracker_resets_on_success_and_rearms(tmp_path: Path) -> None:
    """The tracker itself re-arms once the target succeeds again."""

    streaks = FailureStreakTracker(tmp_path / "streaks.json")
    failure = ScrapeFailure(target="target-001", reason="portal timeout")

    for _ in range(ALERT_FAILURE_THRESHOLD):
        assert streaks.record_round(SERVICE_NETCARE, [failure], []) == []

    crossing = streaks.record_round(SERVICE_NETCARE, [failure], [])
    assert [f.target for f in crossing] == ["target-001"]
    assert crossing[0].streak == ALERT_FAILURE_THRESHOLD + 1

    # Still failing but already latched: no second alert.
    assert streaks.record_round(SERVICE_NETCARE, [failure], []) == []

    # A fresh success clears both the counter and the latch.
    streaks.record_round(SERVICE_NETCARE, [], ["target-001"])
    for _ in range(ALERT_FAILURE_THRESHOLD):
        assert streaks.record_round(SERVICE_NETCARE, [failure], []) == []
    assert len(streaks.record_round(SERVICE_NETCARE, [failure], [])) == 1


def test_streak_tracker_counts_each_service_separately(tmp_path: Path) -> None:
    """One state file, two services, independent counters."""

    streaks = FailureStreakTracker(tmp_path / "streaks.json")
    failure = ScrapeFailure(target="target-001", reason="portal timeout")

    netcare: list[ScrapeFailure] = []
    orbit: list[ScrapeFailure] = []
    for _ in range(ALERT_FAILURE_THRESHOLD + 1):
        netcare += streaks.record_round(SERVICE_NETCARE, [failure], [])
        orbit += streaks.record_round(SERVICE_ORBIT, [failure], [])

    # Both latch on their own fourth failure: neither service inherits the
    # other's count or latch, even though the file and target id are shared.
    assert [f.streak for f in netcare] == [ALERT_FAILURE_THRESHOLD + 1]
    assert [f.streak for f in orbit] == [ALERT_FAILURE_THRESHOLD + 1]

    # Re-arming one service leaves the other latched.
    streaks.record_round(SERVICE_NETCARE, [], ["target-001"])
    assert streaks.record_round(SERVICE_ORBIT, [failure], []) == []


def test_streak_tracker_drops_targets_that_left_the_catalog(tmp_path: Path) -> None:
    """A retired target must not keep its record alive forever."""

    streaks = FailureStreakTracker(tmp_path / "streaks.json")
    failure = ScrapeFailure(target="target-001", reason="portal timeout")

    streaks.record_round(SERVICE_NETCARE, [failure], [])
    # A clean round that no longer mentions the target retires it.
    streaks.record_round(SERVICE_NETCARE, [], ["target-002"])

    assert streaks.record_round(SERVICE_NETCARE, [failure], []) == []


def test_alert_message_names_the_streak_length(tmp_path: Path) -> None:
    """The aggregate message tells the reader how long the target has been down."""

    message = format_scrape_failure_alert(
        SERVICE_NETCARE,
        [ScrapeFailure(target="target-001", reason="portal timeout", streak=4)],
        total=18,
        timestamp="2026-10-06 10:00:00",
    )

    assert "target-001" in message
    assert "4 consecutive rounds" in message


def test_orbit_pending_imei_does_not_reset_a_pending_streak(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Rounds made only of pending IMEIs must not clear a real failure streak."""

    alerter = RecordingAlerter()
    catalog = [_modem(1, valid=False)]
    # Three real failures, then a round that reports only the pending skip.
    rounds = [{1: "portal error"}] * 3 + [{}]
    daemon = _scripted_orbit_daemon(catalog, rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=4) == 4
    assert alerter.calls == []


def test_orbit_pending_imei_stays_neutral_while_another_modem_reports(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A modem awaiting its IMEI keeps its streak even when others are reporting.

    This is the case a lone-pending round never reaches: the catalog is re-read
    every round, so a modem can start reporting ``IMEI_PENDING`` in the middle
    of a real outage. The skip must not be mistaken for the modem leaving the
    catalog, or the failure streak is silently thrown away and the alert that
    was about to fire never fires.
    """

    alerter = RecordingAlerter()
    # Modem 1 has no IMEI yet, but is scripted to fail until round four; modem 2
    # is fine and takes over as the round's only reporter.
    catalog = [_modem(1, valid=False), _modem(2)]
    rounds = [{1: "portal error"}] * 3 + [{2: "portal error"}] + [{1: "portal error"}]
    daemon = _scripted_orbit_daemon(catalog, rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=5) == 5
    # Three failures, a neutral pending round, then a fourth failure: the streak
    # survived, so the crossing round still alerts.
    assert len(alerter.calls) == 1


def test_orbit_rearms_the_alert_after_a_success_resets_the_streak(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A recovered modem starts a brand new streak after recovery."""

    alerter = RecordingAlerter()
    rounds = [{1: "portal error"}] * 4 + [{}] + [{1: "portal error"}] * 4
    daemon = _scripted_orbit_daemon([_modem(1)], rounds, alerter, tmp_path, monkeypatch)

    assert daemon.run(max_rounds=9) == 9
    assert len(alerter.calls) == 2


def test_netcare_alerts_again_after_recovery_and_a_fresh_streak(tmp_path: Path) -> None:
    """Recovery plus another four failures produces a second alert."""

    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]
    healthy = [ScrapeOutcome.from_ok("target-001")]
    rounds = [failing] * 4 + [healthy] + [failing] * 4
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), rounds, alerter)

    assert daemon.run(max_rounds=9) == 9
    assert len(alerter.calls) == 2


def test_netcare_tracks_each_target_independently(tmp_path: Path) -> None:
    """One target's streak never borrows another target's count or latch."""

    alerter = RecordingAlerter()
    # target-002 fails for four straight rounds; target-003 fails only once.
    ok = ScrapeOutcome.from_ok("target-003")
    rounds = [
        [ScrapeOutcome.from_error("target-002", "portal timeout"), ok],
        [ScrapeOutcome.from_error("target-002", "portal timeout"), ok],
        [ScrapeOutcome.from_error("target-002", "portal timeout"), ok],
        [
            ScrapeOutcome.from_error("target-002", "portal timeout"),
            ScrapeOutcome.from_error("target-003", "blip"),
        ],
    ]
    daemon = _scripted_netcare_daemon(NetcareCache(tmp_path / "cache"), rounds, alerter)

    assert daemon.run(max_rounds=4) == 4
    assert len(alerter.calls) == 1
    assert [f.target for f in alerter.calls[0][1]] == ["target-002"]


def test_netcare_streaks_survive_a_daemon_restart(tmp_path: Path) -> None:
    """The counter lives next to the cache, so a restart does not re-alert."""

    cache_dir = tmp_path / "cache"
    alerter = RecordingAlerter()
    failing = [ScrapeOutcome.from_error("target-001", "portal timeout")]

    first = _scripted_netcare_daemon(NetcareCache(cache_dir), [failing] * 3, alerter)
    assert first.run(max_rounds=3) == 3
    assert alerter.calls == []

    restarted = _scripted_netcare_daemon(NetcareCache(cache_dir), [failing] * 2, alerter)
    assert restarted.run(max_rounds=2) == 2
    assert len(alerter.calls) == 1
