"""Quota burn-rate forecasting and predictive alerting for Telkomsel Orbit modems.

Calculates GB/day burn rate from scrape history, estimates days until quota
exhaustion, and assigns alert levels for proactive WhatsApp notifications.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("mrtg_cmp.orbit.burn_rate")

#: Number of consecutive scrape snapshots folded into the rolling burn-rate average.
DEFAULT_HISTORY_WINDOW = 3

#: Days-until-exhaustion thresholds that trigger alert levels.
CRITICAL_DAYS_THRESHOLD = 3.0
WARNING_DAYS_THRESHOLD = 7.0

#: Remaining-quota thresholds (GB) that trigger alert levels.
CRITICAL_QUOTA_GB_THRESHOLD = 5.0
WARNING_QUOTA_GB_THRESHOLD = 15.0


@dataclass(slots=True)
class BurnRateSnapshot:
    """A single historical data point for burn-rate calculation."""

    timestamp: datetime
    remaining_gb: float
    total_quota_gb: float


@dataclass(slots=True)
class BurnRateResult:
    """Result of burn-rate calculation for a modem."""

    modem_no: int
    imei: str
    phone: str
    location: str
    current_remaining_gb: float
    total_quota_gb: float
    burn_rate_gb_per_day: float | None
    days_until_exhaustion: float | None
    alert_level: str  # "CRITICAL", "WARNING", "NORMAL"
    data_points_used: int
    earliest_expiry_str: str | None = None
    earliest_days_left: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        rate = self.burn_rate_gb_per_day
        days = self.days_until_exhaustion
        return {
            "modem_no": self.modem_no,
            "imei": self.imei,
            "phone": self.phone,
            "location": self.location,
            "current_remaining_gb": round(self.current_remaining_gb, 2),
            "total_quota_gb": round(self.total_quota_gb, 2),
            "burn_rate_gb_per_day": round(rate, 3) if rate is not None else None,
            "days_until_exhaustion": round(days, 1) if days is not None else None,
            "alert_level": self.alert_level,
            "data_points_used": self.data_points_used,
            "earliest_expiry_str": self.earliest_expiry_str,
            "earliest_days_left": self.earliest_days_left,
        }


def classify_alert_level(
    days_until_exhaustion: float | None,
    current_remaining_gb: float,
) -> str:
    """Assign CRITICAL / WARNING / NORMAL from days-to-empty and remaining quota.

    CRITICAL fires when the modem is within 3 days of exhaustion or has less
    than 5 GB remaining; WARNING fires at 7 days / 15 GB; everything else is
    NORMAL.  A ``None`` days value (no usable history yet) never escalates
    through the day-based rule — only the remaining-quota rule can fire.
    """
    if days_until_exhaustion is not None and days_until_exhaustion < CRITICAL_DAYS_THRESHOLD:
        return "CRITICAL"
    if current_remaining_gb < CRITICAL_QUOTA_GB_THRESHOLD:
        return "CRITICAL"
    if days_until_exhaustion is not None and days_until_exhaustion < WARNING_DAYS_THRESHOLD:
        return "WARNING"
    if current_remaining_gb < WARNING_QUOTA_GB_THRESHOLD:
        return "WARNING"
    return "NORMAL"


def format_burn_alert_message(result: BurnRateResult, timestamp: str | None = None) -> str:
    """Render a WhatsApp-friendly CRITICAL burn-rate alert for one modem."""
    from mrtg_cmp.notifier import _now_wib_str

    ts = timestamp or _now_wib_str()
    # Calculate days and rate strings to avoid overly long lines
    days_value = result.days_until_exhaustion
    days_str = f"{days_value:.1f}" if days_value is not None else "unknown"
    rate_value = result.burn_rate_gb_per_day
    rate_str = f"{rate_value:.2f}" if rate_value is not None else "?"
    lines = [
        "🔥 *[QUOTA BURN-RATE ALERT]*",
        f"📡 *Modem:* #{result.modem_no} {result.location} ({result.phone})",
        f"⏱ *Time:* {ts} WIB",
        f"📊 *Burn rate:* {rate_str} GB/day",
        f"⚠ *Estimated days to empty:* {days_str}",
        f"🔋 *Remaining quota:* {result.current_remaining_gb:.2f} GB /",
        f"  {result.total_quota_gb:.2f} GB",
        f"🚨 *Alert level:* {result.alert_level}",
    ]
    return "\n".join(lines)


def send_burn_alert(result: BurnRateResult, timestamp: str | None = None) -> bool:
    """Send one CRITICAL burn-rate alert through the shared WhatsApp notifier."""
    from mrtg_cmp.config import settings
    from mrtg_cmp.notifier import send_whatsapp_message

    if result.alert_level != "CRITICAL":
        return False
    if not settings.wa_alert_enabled:
        logger.debug(
            "WhatsApp alerts disabled; skipping Orbit burn-rate alert for modem %s",
            result.modem_no,
        )
        return False
    message = format_burn_alert_message(result, timestamp)
    try:
        delivered = send_whatsapp_message(message)
    except Exception as exc:
        logger.warning(
            "Orbit burn-rate alert for modem %s could not be sent: %s",
            result.modem_no,
            exc,
        )
        return False
    if not delivered:
        logger.warning(
            "Orbit burn-rate alert for modem %s was rejected by the gateway",
            result.modem_no,
        )
        return False
    logger.info(
        "Sent Orbit CRITICAL burn-rate alert for modem %s",
        result.modem_no,
    )
    return True


class QuotaBurnRateTracker:
    """Track per-modem quota snapshots, roll them into a GB/day burn rate, and
    persist that history as JSON so a restart does not lose the forecast.

    The tracker records one snapshot per successful scrape round, keyed by the
    modem's IMEI.  ``compute`` returns a :class:`BurnRateResult` built from the
    most recent :data:`DEFAULT_HISTORY_WINDOW` snapshots: the burn rate is the
    average of the consecutive GB/day deltas over that window, and
    days-until-exhaustion divides the current remaining quota by that rate.
    """

    def __init__(
        self,
        state_file: Path | str | None = None,
        history_window: int = DEFAULT_HISTORY_WINDOW,
    ) -> None:
        self.state_file = Path(state_file) if state_file is not None else None
        self.history_window = max(1, history_window)
        self._lock = threading.Lock()
        # imei -> list[BurnRateSnapshot], oldest first
        self._snapshots: dict[str, list[BurnRateSnapshot]] = {}
        # imei -> most recent computed BurnRateResult
        self._results: dict[str, BurnRateResult] = {}
        # imei -> whether the CRITICAL alert already fired for this modem;
        # dropped (re-armed) as soon as the modem recovers below CRITICAL.
        self._critical_alerted: dict[str, bool] = {}
        self._load()

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def record_snapshot(
        self,
        imei: str,
        remaining_gb: float,
        total_quota_gb: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Append one scrape round's quota reading for a modem."""
        with self._lock:
            now = timestamp or datetime.now(UTC)
            snapshots = self._snapshots.setdefault(imei, [])
            snapshots.append(
                BurnRateSnapshot(
                    timestamp=now,
                    remaining_gb=remaining_gb,
                    total_quota_gb=total_quota_gb,
                )
            )
            self._save_locked()

    def record_statuses(self, statuses: list[Any], timestamp: datetime | None = None) -> None:
        """Record one snapshot per successfully scraped Orbit modem status.

        A status carrying an error (or a pending IMEI) is skipped: it did not
        produce a fresh quota reading, so it must not distort the burn-rate
        history.
        """
        for status in statuses:
            if getattr(status, "error", None):
                continue
            target = getattr(status, "target", None)
            imei = str(getattr(target, "imei", "") or "").strip()
            if len(imei) != 15:
                continue
            remaining = float(getattr(status, "total_remaining_gb", 0.0) or 0.0)
            total = float(getattr(status, "total_quota_gb", 0.0) or 0.0)
            self.record_snapshot(imei, remaining, total, timestamp=timestamp)

    # ------------------------------------------------------------------
    # computation
    # ------------------------------------------------------------------

    def _burn_rate_from_snapshots(
        self, snapshots: list[BurnRateSnapshot]
    ) -> tuple[float | None, int]:
        """Average the consecutive GB/day deltas across a snapshot list.

        Returns ``(rate, points_used)``.  Fewer than two snapshots, or
        snapshots with no positive elapsed time between them, yield
        ``None`` (not enough signal to forecast from).
        """
        if len(snapshots) < 2:
            return None, len(snapshots)
        deltas: list[float] = []
        for older, newer in zip(snapshots[:-1], snapshots[1:], strict=True):
            elapsed_seconds = (newer.timestamp - older.timestamp).total_seconds()
            if elapsed_seconds <= 0:
                continue
            delta_gb = older.remaining_gb - newer.remaining_gb
            if delta_gb < 0:
                # Quota topped up between readings; that interval is not a
                # consumption signal, so skip it rather than drag the average
                # negative.
                continue
            deltas.append(delta_gb / (elapsed_seconds / 86400.0))
        if not deltas:
            return None, len(snapshots)
        rate = sum(deltas) / len(deltas)
        return rate, len(snapshots)

    def _evaluate(
        self,
        imei: str,
        status: Any,
        windowed_snapshots: list[BurnRateSnapshot],
    ) -> BurnRateResult:
        target = getattr(status, "target", None) if status is not None else None
        modem_no = int(getattr(target, "no", 0) or 0)
        phone = str(getattr(target, "phone", "") or "")
        location = str(getattr(target, "location", "") or "")
        current_remaining = windowed_snapshots[-1].remaining_gb if windowed_snapshots else 0.0
        total_quota = windowed_snapshots[-1].total_quota_gb if windowed_snapshots else 0.0

        rate, points_used = self._burn_rate_from_snapshots(windowed_snapshots)
        days_until_exhaustion = current_remaining / rate if rate is not None and rate > 0 else None

        alert_level = classify_alert_level(days_until_exhaustion, current_remaining)

        result = BurnRateResult(
            modem_no=modem_no,
            imei=imei,
            phone=phone,
            location=location,
            current_remaining_gb=current_remaining,
            total_quota_gb=total_quota,
            burn_rate_gb_per_day=rate,
            days_until_exhaustion=days_until_exhaustion,
            alert_level=alert_level,
            data_points_used=points_used,
            earliest_expiry_str=getattr(target, "earliest_expiry_str", None),
            earliest_days_left=getattr(target, "earliest_days_left", None),
        )
        self._results[imei] = result
        if alert_level != "CRITICAL":
            self._critical_alerted.pop(imei, None)
        return result

    def compute(self, imei: str, status: Any | None = None) -> BurnRateResult:
        """Recompute and cache the burn-rate forecast for one modem's IMEI."""
        with self._lock:
            all_snapshots = self._snapshots.get(imei, [])
            windowed = all_snapshots[-self.history_window :] if all_snapshots else []
            return self._evaluate(imei, status, windowed)

    def compute_all(self, statuses: list[Any] | None = None) -> list[BurnRateResult]:
        """Evaluate every modem that has recorded at least one snapshot.

        ``statuses`` may supply fresh
        :class:`~mrtg_cmp.orbit.scraper.OrbitModemStatus` objects (for their
        target context and any newly observed IMEI); when omitted, the tracker
        only re-evaluates the IMEIs it already has history for.
        """
        with self._lock:
            if statuses:
                targets: dict[str, Any] = {}
                for status in statuses:
                    target = getattr(status, "target", None)
                    imei = str(getattr(target, "imei", "") or "").strip()
                    if len(imei) == 15:
                        targets[imei] = status
            else:
                targets = {imei: None for imei in self._snapshots}

            results = [
                self._evaluate(
                    imei,
                    targets.get(imei),
                    self._snapshots.get(imei, [])[-self.history_window :],
                )
                for imei in targets
            ]
            return results

    def critical_alerts_pending(self) -> list[BurnRateResult]:
        """Return CRITICAL results that have not already alerted this episode.

        A modem alerts once, until its level drops back below CRITICAL, which
        re-arms it for the next excursion.
        """
        with self._lock:
            pending: list[BurnRateResult] = []
            for imei, result in self._results.items():
                if result.alert_level != "CRITICAL":
                    continue
                if self._critical_alerted.get(imei, False):
                    continue
                pending.append(result)
            return pending

    def mark_alerted(self, results: list[BurnRateResult]) -> None:
        """Latch the CRITICAL alert for the given results so they do not re-fire."""
        with self._lock:
            for result in results:
                if result.alert_level == "CRITICAL":
                    self._critical_alerted[result.imei] = True

    def result_for_imei(self, imei: str) -> BurnRateResult | None:
        """Return the most recently computed forecast for one IMEI, if any."""
        with self._lock:
            return self._results.get(imei)

    def clear(self, imei: str | None = None) -> None:
        """Drop all recorded history (and cached results) for one IMEI, or all."""
        with self._lock:
            if imei is None:
                self._snapshots.clear()
                self._results.clear()
                self._critical_alerted.clear()
            else:
                self._snapshots.pop(imei, None)
                self._results.pop(imei, None)
                self._critical_alerted.pop(imei, None)
            self._save_locked()

    def snapshot_count(self, imei: str) -> int:
        """Return how many snapshots are recorded for one IMEI (for tests/debug)."""
        with self._lock:
            return len(self._snapshots.get(imei, []))

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _save_locked(self) -> None:
        """Persist snapshots atomically; the caller must hold ``self._lock``."""
        path = self.state_file
        if path is None:
            return
        payload = {
            "version": 1,
            "snapshots": [
                {
                    "imei": imei,
                    "points": [
                        {
                            "timestamp": snapshot.timestamp.isoformat(),
                            "remaining_gb": snapshot.remaining_gb,
                            "total_quota_gb": snapshot.total_quota_gb,
                        }
                        for snapshot in snapshots
                    ],
                }
                for imei, snapshots in self._snapshots.items()
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, indent=2, sort_keys=True))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            except OSError:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except OSError as exc:
            logger.warning("Orbit burn-rate state at %s could not be written: %s", path, exc)

    def _load(self) -> None:
        """Load persisted snapshots, tolerating a missing or damaged file."""
        path = self.state_file
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Orbit burn-rate state at %s could not be read: %s", path, exc)
            return
        entries = payload.get("snapshots") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            logger.warning("Orbit burn-rate state at %s is malformed", path)
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            imei = str(entry.get("imei", ""))
            points = entry.get("points")
            if not imei or not isinstance(points, list):
                continue
            snapshots: list[BurnRateSnapshot] = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                try:
                    ts = datetime.fromisoformat(str(point.get("timestamp", "")))
                except ValueError:
                    continue
                try:
                    remaining = float(point.get("remaining_gb", 0.0))
                    total = float(point.get("total_quota_gb", 0.0))
                except (TypeError, ValueError):
                    continue
                snapshots.append(
                    BurnRateSnapshot(
                        timestamp=ts,
                        remaining_gb=remaining,
                        total_quota_gb=total,
                    )
                )
            if snapshots:
                snapshots.sort(key=lambda snapshot: snapshot.timestamp)
                self._snapshots[imei] = snapshots


__all__ = [
    "CRITICAL_DAYS_THRESHOLD",
    "CRITICAL_QUOTA_GB_THRESHOLD",
    "DEFAULT_HISTORY_WINDOW",
    "QuotaBurnRateTracker",
    "BurnRateSnapshot",
    "BurnRateResult",
    "WARNING_DAYS_THRESHOLD",
    "WARNING_QUOTA_GB_THRESHOLD",
    "classify_alert_level",
    "format_burn_alert_message",
    "send_burn_alert",
]
