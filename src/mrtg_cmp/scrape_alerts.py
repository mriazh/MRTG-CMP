"""Failure-only WhatsApp alerts for the Netcare and Orbit scrape daemons.

Each daemon hands at most one aggregate message per outage to the existing
:mod:`mrtg_cmp.notifier` dispatcher; nothing is sent on a round that completed
without a single real failure, so a healthy fleet stays silent.

:class:`FailureStreakTracker` sits in front of the seam. A single failed round
is usually a portal blip, so a target has to fail
:data:`ALERT_FAILURE_THRESHOLD` rounds *in a row* before it is reported, and the
message latches until that target succeeds again. That is what turns a four
round outage into one message instead of four.

Two kinds of "not a fresh success" are modelled explicitly, because a stale
picture on a dashboard is exactly the kind of quiet fault that outlasts an
on-call shift:

* Netcare keeps the previous good image when a target fails, which
  :func:`netcare_failures` reports as ``stale`` rather than as a capture.
* Orbit modems awaiting an IMEI report the expected ``IMEI_PENDING`` skip,
  which :func:`orbit_failures` drops instead of paging someone about it.

Delivery is deliberately unable to take a daemon down: a gateway error or a
non-200 reply is logged and swallowed so the scrape loop keeps running.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import settings
from .notifier import _now_wib_str, send_whatsapp_message

logger = logging.getLogger(__name__)

#: Service label shown in an alert so the reader knows which daemon spoke.
SERVICE_NETCARE = "TelkomCare Netcare"
SERVICE_ORBIT = "Telkomsel Orbit"

#: Consecutive failed rounds a target must survive before anyone is paged.
#:
#: A single scrape failure is usually a portal blip, and paging on it trains
#: operators to ignore the channel. Crossing this many rounds in a row means the
#: fault has outlived one poll interval, so it is worth waking someone.
ALERT_FAILURE_THRESHOLD = 3

#: File holding the per-target counters, written beside each service cache.
ALERT_STATE_FILENAME = "alert_state.json"

#: Longest reason rendered before it is elided, keeping one message readable.
MAX_REASON_CHARS = 80

#: Most failures named individually before the tail is summarised as a count.
MAX_LISTED_FAILURES = 10

#: Shown when a modem has no usable IMEI yet but is otherwise identifiable.
UNKNOWN_TARGET = "unknown target"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ScrapeFailure:
    """One target or modem that did not produce a fresh result this round."""

    target: str
    reason: str
    stale: bool = False
    streak: int = 0


#: Alert seam shared by both daemons: ``(service, failures, total) -> bool``.
AlertSender = Callable[[str, list[ScrapeFailure], int], bool]


def sanitize_reason(reason: str | None, limit: int = MAX_REASON_CHARS) -> str:
    """Flatten a driver or portal error into one short single-line string.

    Scrapers surface multi-line Selenium traces and stack fragments; left
    alone those would break the one-message-per-round format and bury the
    target list, so whitespace collapses and the tail is elided.
    """

    flattened = _WHITESPACE.sub(" ", reason or "").strip()
    if not flattened:
        return "unknown error"
    if len(flattened) <= limit:
        return flattened
    return flattened[: max(0, limit - 3)].rstrip() + "..."


def netcare_failures(outcomes: Sequence[object], cache: object) -> list[ScrapeFailure]:
    """Collect the Netcare targets that failed, flagging those kept on cache.

    ``cache`` is consulted for a previously captured image so a target still
    being served from an old file is reported as stale instead of implying the
    capture simply succeeded quietly.
    """

    failures: list[ScrapeFailure] = []
    for outcome in outcomes:
        status = getattr(outcome, "status", None)
        if status == "ok":
            continue
        target = str(getattr(outcome, "target", UNKNOWN_TARGET))
        failures.append(
            ScrapeFailure(
                target=target,
                reason=sanitize_reason(getattr(outcome, "error", None)),
                stale=bool(_has_image(cache, target)),
            )
        )
    return failures


def _has_image(cache: object, target: str) -> bool:
    """Return True when the cache still holds a previous image for a target."""

    has_image = getattr(cache, "has_image", None)
    if not callable(has_image):
        return False
    try:
        return bool(has_image(target))
    except OSError:  # pragma: no cover - unreadable cache is reported elsewhere
        return False


def orbit_failures(statuses: Sequence[object]) -> list[ScrapeFailure]:
    """Collect the Orbit modems that genuinely failed, skipping pending IMEIs.

    ``IMEI_PENDING`` is the normal state of a modem still awaiting its
    hardware identifier, so it is an expected skip rather than a fault worth
    waking anyone for.
    """

    failures: list[ScrapeFailure] = []
    for status in statuses:
        error = getattr(status, "error", None)
        if not error or error == "IMEI_PENDING":
            continue
        failures.append(
            ScrapeFailure(
                target=_orbit_identifier(status),
                reason=sanitize_reason(error),
            )
        )
    return failures


def _orbit_identifier(status: object) -> str:
    """Name a modem by IMEI, falling back to its phone number then a stub."""

    target = getattr(status, "target", None)
    for attribute in ("imei", "phone"):
        value = str(getattr(target, attribute, "") or "").strip()
        if value:
            return value
    return UNKNOWN_TARGET


def orbit_identifier(status: object) -> str:
    """Public alias for naming a modem consistently with :func:`orbit_failures`."""

    return _orbit_identifier(status)


class FailureStreakTracker:
    """Consecutive-failure counters with a one-shot latch, per service and target.

    A round reports which targets failed and which came back clean. A failure
    increments that target's counter; a success clears it entirely. The first
    failure past :data:`ALERT_FAILURE_THRESHOLD` latches and is returned, and
    further failures return nothing until a success re-arms the target. That
    gives exactly one message per outage rather than one per round of it.

    The counters are persisted next to the owning service cache so a restart
    cannot re-alert a target that has already been reported. State is per
    ``(service, target)``: two daemons, or two targets in one daemon, never
    share a count. Reading a damaged or missing file resets to zero rather than
    raising, because losing a counter must never stop a scrape loop.
    """

    def __init__(
        self,
        state_path: Path | str | None = None,
        threshold: int = ALERT_FAILURE_THRESHOLD,
    ) -> None:
        self.state_path = Path(state_path) if state_path is not None else None
        self.threshold = max(0, int(threshold))
        self._lock = threading.Lock()
        # service -> target -> {"consecutive_failures": int, "alerted": bool}
        self._state: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    def record_round(
        self,
        service: str,
        failures: Sequence[ScrapeFailure],
        succeeded: Sequence[str] = (),
        skipped: Sequence[str] = (),
    ) -> list[ScrapeFailure]:
        """Fold one round into the streaks, returning failures crossing the threshold.

        ``succeeded`` names the targets that produced a fresh result this round;
        each one clears its own counter and latch. ``skipped`` names the targets
        the round passed over on purpose — an Orbit modem still reporting
        ``IMEI_PENDING``. A skipped target neither advances nor clears its
        streak, and is explicitly not retired either.

        Skipping has to be stated rather than inferred from absence: a modem
        that waits on its IMEI looks identical to one that left the catalog, and
        guessing wrong silently discards a real failure streak.
        """

        with self._lock:
            entries = self._state.setdefault(service, {})
            for target in succeeded:
                entries.pop(target, None)

            crossing: list[ScrapeFailure] = []
            for failure in failures:
                entry = entries.setdefault(
                    failure.target, {"consecutive_failures": 0, "alerted": False}
                )
                entry["consecutive_failures"] = int(entry["consecutive_failures"]) + 1
                streak = int(entry["consecutive_failures"])
                if streak > self.threshold and not entry["alerted"]:
                    entry["alerted"] = True
                    crossing.append(replace(failure, streak=streak))

            _drop_retired(entries, failures, succeeded, skipped)
            self._save()
            return crossing

    def streak(self, service: str, target: str) -> int:
        """Return the current consecutive-failure count for one target."""

        with self._lock:
            entry = self._state.get(service, {}).get(target, {})
            return int(entry.get("consecutive_failures", 0))

    def _load(self) -> None:
        """Read persisted streaks, tolerating a missing or damaged file."""

        path = self.state_path
        if path is None:
            return
        if not path.is_file():
            # Absent state means every counter starts at zero, so a daemon
            # started mid-outage alerts again. That is the safe direction to
            # fail in, but it is still worth saying out loud.
            logger.warning(
                "Scrape alert streak state at %s is absent; starting every streak from zero",
                path,
            )
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Scrape alert streak state at %s could not be read: %s", path, exc)
            return

        services = payload.get("services") if isinstance(payload, dict) else None
        if not isinstance(services, dict):
            logger.warning("Scrape alert streak state at %s is malformed", path)
            return
        for service, targets in services.items():
            if not isinstance(targets, dict):
                continue
            for target, entry in targets.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    count = max(0, int(entry.get("consecutive_failures", 0)))
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                self._state.setdefault(str(service), {})[str(target)] = {
                    "consecutive_failures": count,
                    "alerted": bool(entry.get("alerted", False)),
                }

    def _save(self) -> None:
        """Persist streaks atomically; a failed write never breaks the loop."""

        path = self.state_path
        if path is None:
            return
        payload = {"version": 1, "services": self._state}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_path = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=path.name,
                suffix=".tmp",
            )
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, indent=2, sort_keys=True))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            except OSError:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
        except OSError as exc:
            logger.warning("Scrape alert streak state at %s could not be written: %s", path, exc)


def _drop_retired(
    entries: dict[str, dict[str, Any]],
    failures: Sequence[ScrapeFailure],
    succeeded: Sequence[str],
    skipped: Sequence[str] = (),
) -> None:
    """Forget targets missing from the catalog, keeping skipped ones intact.

    Only an entry absent from a round that reported on the rest of the catalog
    is retired. ``skipped`` is part of what the round reported, so a modem
    waiting on its IMEI keeps its streak even when its neighbours are being
    counted; dropping on absence alone would reset a real outage every time one
    modem waited.
    """

    seen = {f.target for f in failures} | set(succeeded) | set(skipped)
    if not seen:
        return
    for target in [t for t in entries if t not in seen]:
        del entries[target]


def format_scrape_failure_alert(
    service: str,
    failures: Sequence[ScrapeFailure],
    total: int,
    timestamp: str | None = None,
) -> str:
    """Compose the single aggregate message describing one failed round.

    The whole round is summarised once rather than once per target: an
    eighteen-link outage should read as one incident with a count, not as a
    wall of separate notifications.
    """

    ts = timestamp or _now_wib_str()
    lines = [
        "🚨 *[SCRAPE FAILED]*",
        f"📡 *Service:* {service}",
        f"⏱ *Time:* {ts} WIB",
        f"📊 *Result:* {len(failures)} of {total} failed",
        "",
    ]
    lines.extend(_format_failure_line(f) for f in failures[:MAX_LISTED_FAILURES])
    hidden = len(failures) - MAX_LISTED_FAILURES
    if hidden > 0:
        lines.append(f"...and {hidden} more")
    return "\n".join(lines)


def _format_failure_line(failure: ScrapeFailure) -> str:
    """Render one failure, naming it stale when the dashboard is showing cache."""

    notes = ""
    if failure.stale:
        notes += " (stale cache, showing last good graph)"
    if failure.streak:
        notes += f" ({failure.streak} consecutive rounds)"
    return f"• `{failure.target}` — {failure.reason}{notes}"


def send_scrape_failure_alert(
    service: str,
    failures: Sequence[ScrapeFailure],
    total: int,
    timestamp: str | None = None,
) -> bool:
    """Send the aggregate alert through the shared WhatsApp notifier.

    Returns True only when a message was actually dispatched. Every delivery
    failure is logged and contained: alerting is best-effort reporting layered
    on top of the scrape loop, so a gateway outage must not end a round or stop
    a daemon.
    """

    if not failures:
        return False
    if not settings.wa_alert_enabled:
        logger.debug("WhatsApp alerts disabled; skipping %s failure alert", service)
        return False

    message = format_scrape_failure_alert(service, failures, total, timestamp)
    try:
        delivered = send_whatsapp_message(message)
    except Exception as exc:
        logger.warning("Scrape failure alert for %s could not be sent: %s", service, exc)
        return False

    if not delivered:
        logger.warning("Scrape failure alert for %s was rejected by the gateway", service)
        return False

    logger.info("Sent %s scrape failure alert for %s target(s)", service, len(failures))
    return True


__all__ = [
    "ALERT_FAILURE_THRESHOLD",
    "ALERT_STATE_FILENAME",
    "SERVICE_NETCARE",
    "SERVICE_ORBIT",
    "AlertSender",
    "FailureStreakTracker",
    "ScrapeFailure",
    "format_scrape_failure_alert",
    "netcare_failures",
    "orbit_failures",
    "orbit_identifier",
    "sanitize_reason",
    "send_scrape_failure_alert",
]