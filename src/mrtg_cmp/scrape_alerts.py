"""Failure-only WhatsApp alerts for the Netcare and Orbit scrape daemons.

Each daemon hands one aggregate message per round to the existing
:mod:`mrtg_cmp.notifier` dispatcher; nothing is sent on a round that completed
without a single real failure, so a healthy fleet stays silent.

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

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import settings
from .notifier import _now_wib_str, send_whatsapp_message

logger = logging.getLogger(__name__)

#: Service label shown in an alert so the reader knows which daemon spoke.
SERVICE_NETCARE = "TelkomCare Netcare"
SERVICE_ORBIT = "Telkomsel Orbit"

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

    note = " (stale cache, showing last good graph)" if failure.stale else ""
    return f"• `{failure.target}` — {failure.reason}{note}"


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
    "SERVICE_NETCARE",
    "SERVICE_ORBIT",
    "AlertSender",
    "ScrapeFailure",
    "format_scrape_failure_alert",
    "netcare_failures",
    "orbit_failures",
    "sanitize_reason",
    "send_scrape_failure_alert",
]