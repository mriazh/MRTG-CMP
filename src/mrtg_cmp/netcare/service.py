"""Netcare scraper daemon.

Runs the scrape loop 24/7 independently of any web client: one round over all
branch targets, then a sleep of ``netcare_poll_interval_seconds`` (default 5
minutes). Shutdown on SIGINT/SIGTERM is graceful so systemd stops the unit
without leaving a half-written capture behind.

A round is fanned out over a small worker pool (``netcare_workers``, default
3). Targets are split into balanced contiguous buckets so each worker owns its
own capturer — and therefore its own browser session — which keeps concurrent
Selenium drivers out of each other's way and cuts an 18-branch round from
roughly 150 seconds to roughly 45 (FR-15.1).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..config import settings as global_settings
from .captcha import GeminiCaptchaSolver
from .query import STAGE_OPENING_BROWSER, StageCallback, branch_stage
from .scraper import (
    LIVE_DAY,
    STATUS_OK,
    STATUS_STALE,
    NetcareCache,
    ScrapeOutcome,
    build_graph_url,
    format_date_filter,
    format_date_range,
    normalize_day_key,
)
from .session import NetcareSession
from .targets import NetcareTarget, NetcareTargetType, resolve_targets

logger = logging.getLogger("mrtg_cmp.netcare.service")

DEFAULT_INTERVAL_SECONDS = 300

#: Default parallel workers when the caller does not pin a count.
DEFAULT_WORKERS = 3

#: XPath of the portal's rendered graph image.
GRAPH_IMAGE_XPATH = "//img[contains(@src, 'graph.php')]"
#: Minimum natural width, in pixels, before a render counts as a real graph.
MIN_GRAPH_WIDTH_PX = 400

#: Lookup mode -> the ``name`` attribute of that page's target search input.
TARGET_INPUT_NAMES: dict[NetcareTargetType, str] = {
    NetcareTargetType.SID: "sid",
    NetcareTargetType.GRAPH_TITLE: "graphtitle",
}

#: The portal's "Show Graph" anchor, pressed after the target ID is entered.
SHOW_GRAPH_SELECTOR = "a.btn-graph"

#: SID mode's date filter is an unlabelled button with no id, so it is matched
#: on its text instead.
SID_FILTER_BUTTON_XPATH = "//button[contains(normalize-space(), 'Filter')]"
#: The visible inputs preceding that button are the SID page's date range. The
#: browser returns the match set in document order, so the final two entries are
#: start and end.
SID_DATE_INPUT_XPATH = (
    "//button[contains(normalize-space(), 'Filter')]/preceding::input[not(@type='hidden')]"
)

#: Path the portal redirects to once the session is no longer valid.
LOGIN_PATH = "/public/login"

#: Seconds the page needs to settle after a modal alert is dismissed. The portal
#: paints its next frame asynchronously, so interacting immediately after
#: ``accept()`` lands on the pre-dismissal layout.
ALERT_SETTLE_SECONDS = 1.0

#: The portal's AJAX busy indicators. Whichever of them is still *displayed*
#: after a form or filter submission means the graph request behind it is still
#: in flight, so reading the graph image before they detach reads a stale one.
LOADING_OVERLAY_SELECTOR = ".blockUI, .loading, #loader, .spinner"
#: Seconds to wait for those overlays to detach before looking for the graph.
OVERLAY_TIMEOUT_SECONDS = 10
#: Poll interval while waiting for the overlays to go away.
OVERLAY_POLL_SECONDS = 0.5

#: Hides every non-ancestor element and pins the graph image full-bleed so a
#: capture cannot pick up pop-ups, banners, or stray overlays.
GRAPH_ISOLATION_SCRIPT = """
var target = null;
var images = document.querySelectorAll("img[src*='graph.php']");
for (var i = 0; i < images.length; i++) {
    if ((images[i].naturalWidth || 0) > 400) { target = images[i]; break; }
}
if (!target) { return false; }
window._hidden = [];
window._targetCssText = target.style.cssText;
window._bodyCssText = document.body.style.cssText;
window._htmlCssText = document.documentElement.style.cssText;
var ancestors = [];
for (var node = target.parentNode; node && node.nodeName !== 'HTML'; node = node.parentNode) {
    ancestors.push(node);
}
var all = document.body.getElementsByTagName('*');
var skippable = ['SCRIPT', 'STYLE'];
for (var j = 0; j < all.length; j++) {
    var el = all[j];
    var keep = el === target || ancestors.indexOf(el) !== -1;
    if (!keep && skippable.indexOf(el.nodeName) === -1) {
        if (window.getComputedStyle(el).display !== 'none') {
            window._hidden.push({el: el, display: el.style.display});
            el.style.setProperty('display', 'none', 'important');
        }
    }
}
target.style.setProperty('position', 'fixed', 'important');
target.style.setProperty('top', '0', 'important');
target.style.setProperty('left', '0', 'important');
target.style.setProperty('z-index', '99999999', 'important');
target.style.setProperty('margin', '0', 'important');
target.style.setProperty('padding', '0', 'important');
target.style.setProperty('transform', 'none', 'important');
target.style.setProperty('max-width', 'none', 'important');
target.style.setProperty('max-height', 'none', 'important');
var naturalWidth = target.naturalWidth || target.width || target.clientWidth;
var naturalHeight = target.naturalHeight || target.height || target.clientHeight;
target.style.setProperty('width', naturalWidth + 'px', 'important');
target.style.setProperty('height', naturalHeight + 'px', 'important');
document.body.style.setProperty('background', '#ffffff', 'important');
document.body.style.setProperty('margin', '0', 'important');
document.body.style.setProperty('padding', '0', 'important');
document.body.style.setProperty('overflow', 'hidden', 'important');
document.documentElement.style.setProperty('overflow', 'hidden', 'important');
return true;
"""

#: Restores everything {@link GRAPH_ISOLATION_SCRIPT} changed.
GRAPH_RESTORE_SCRIPT = """
if (window._hidden) {
    for (var i = 0; i < window._hidden.length; i++) {
        window._hidden[i].el.style.display = window._hidden[i].display;
    }
    window._hidden = null;
}
document.body.style.cssText = window._bodyCssText || '';
document.documentElement.style.cssText = window._htmlCssText || '';
"""

#: Callable capturing one target's graph and returning the raw PNG bytes.
TargetCapturer = Callable[[NetcareTarget], bytes]
#: Callable performing one full scrape round.
RoundRunner = Callable[[], "list[ScrapeOutcome]"]
#: Called after each target settles with ``(outcome, completed, total)``.
ProgressCallback = Callable[["ScrapeOutcome", int, int], None]
#: Builds a fresh capturer, e.g. bound to a worker's own browser session.
CapturerFactory = Callable[[int], TargetCapturer]
#: Builds the browser session dedicated to worker ``index``.
SessionFactory = Callable[[int], "NetcareSession"]


def _sleep(seconds: float) -> None:
    """Sleep for ``seconds``, routed through a seam so tests can intercept it.

    The alert settle and the overlay poll are the only deliberate pauses in the
    capture path. Routing both through one indirect call lets a test record or
    collapse them, so the assertions can be about *whether* the pause happened
    rather than about how long the suite took.
    """

    _sleep_hook(seconds)


_sleep_hook: Callable[[float], Any] = time.sleep


def normalize_workers(workers: int) -> int:
    """Return a usable worker count, degrading a bad value to one.

    A non-positive or non-numeric worker count must not take the daemon down,
    and must not silently hand a worker index to a pool that cannot serve it.
    """

    try:
        count = int(workers)
    except (TypeError, ValueError):
        count = 1
    return max(1, count)


def partition_targets(
    targets: Sequence[NetcareTarget],
    workers: int = DEFAULT_WORKERS,
) -> list[list[NetcareTarget]]:
    """Split targets into balanced contiguous buckets, one per worker.

    Buckets differ in size by at most one, so 18 targets over 3 workers become
    6/6/6 rather than letting one worker take the whole tail. A non-positive or
    non-numeric worker count degrades to a single bucket instead of raising, so a
    bad environment variable cannot take the daemon down.
    """

    count = normalize_workers(workers)
    if not targets:
        return []

    count = min(count, len(targets))
    total = len(targets)
    base, remainder = divmod(total, count)
    buckets: list[list[NetcareTarget]] = []
    cursor = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        buckets.append(list(targets[cursor : cursor + size]))
        cursor += size
    return buckets


def _relogin_hook(capture: TargetCapturer) -> Callable[[], bool] | None:
    """Return a capturer's re-login hook, or None when it has none.

    A plain function carries no session to re-authenticate, so the hook is read
    off the capturer rather than assumed. That keeps a caller-supplied lambda
    working exactly as before; it just never recovers.
    """

    hook = getattr(capture, "relogin", None)
    return hook if callable(hook) else None


def _relogin_and_retry(
    capture: TargetCapturer,
    target: NetcareTarget,
    relogin: Callable[[], bool] | None,
) -> bytes | None:
    """Re-authenticate and retry one target, returning None if that does not work.

    Exactly one retry, never a loop: an expired session is a re-login problem,
    and re-running the same capture against a portal that refuses every session
    only delays the round for each remaining target.
    """

    if relogin is None:
        logger.warning("No re-login hook for %s; failing the target", target.target)
        return None
    try:
        if not relogin():
            logger.warning("TelkomCare re-login failed; giving up on %s", target.target)
            return None
    except Exception as exc:
        logger.warning("TelkomCare re-login raised for %s: %s", target.target, exc)
        return None

    logger.info("Re-authenticated TelkomCare; retrying %s", target.target)
    try:
        return capture(target)
    except Exception as exc:
        logger.warning("Retry after re-login failed for %s: %s", target.target, exc)
        return None


def _capture_one(
    cache: NetcareCache,
    target: NetcareTarget,
    capture: TargetCapturer,
    day: str | None,
    relogin: Callable[[], bool] | None = None,
) -> ScrapeOutcome:
    """Capture a single target, recording the outcome in the manifest.

    An expired session is retried once after a re-login. Every other failure is
    recorded as-is: the round's contract is that one bad link never aborts the
    rest, and the previous image stays visible as ``stale``.
    """

    try:
        payload = capture(target)
    except NetcareSessionExpiredError as exc:
        logger.warning("TelkomCare session expired on %s: %s", target.target, exc)
        retry = _relogin_and_retry(capture, target, relogin)
        if retry is None:
            cache.mark_error(target.target, str(exc), day)
            return ScrapeOutcome.from_error(target.target, str(exc))
        payload = retry
    except Exception as exc:
        logger.warning("Netcare capture failed for %s: %s", target.target, exc)
        cache.mark_error(target.target, str(exc), day)
        return ScrapeOutcome.from_error(target.target, str(exc))

    if not cache.store(target.target, payload, day):
        cache.mark_no_graph(target.target, "No graph placeholder", day)
        return ScrapeOutcome.from_no_graph(target.target, "No graph placeholder")

    cache.mark_ok(target.target, day)
    return ScrapeOutcome.from_ok(target.target)


def scrape_round(
    cache: NetcareCache,
    targets: Sequence[NetcareTarget],
    capture: TargetCapturer | None = None,
    max_workers: int = DEFAULT_WORKERS,
    progress: ProgressCallback | None = None,
    day: datetime | str | None = None,
    capturer_factory: CapturerFactory | None = None,
    stage: StageCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[ScrapeOutcome]:
    """Capture every target once, never letting one failure abort the round.

    A target that fails keeps whatever image is already cached: the manifest is
    updated to ``stale`` so the dashboard shows the last good graph rather than a
    broken image. Outcomes come back in the original target order regardless of
    how the pool interleaved them, so callers can zip results positionally.

    Supply ``capturer_factory`` to give each worker its own capturer — and so its
    own browser session — instead of sharing the thread-safe ``capture``. ``day``
    selects a date partition; ``None`` writes the live flat cache. ``stage``
    publishes the branch currently being captured, and ``cancelled`` lets an
    operator abandon the round between targets instead of waiting it out.

    A capturer built by ``_per_target_capture`` carries a ``relogin`` hook. When
    one is present, a target that hits an expired session is retried once after
    re-authenticating, so one stale cookie costs a re-login rather than an error
    on every remaining branch in the round.

    Raises:
        ValueError: when neither ``capture`` nor ``capturer_factory`` is given,
            which would otherwise fail deep inside a worker thread.
    """

    if not targets:
        return []

    if capture is None and capturer_factory is None:
        raise ValueError("scrape_round needs either a capture callable or a capturer_factory")

    day_partition = normalize_day_key(day) if day is not None else None
    buckets = partition_targets(targets, max_workers)

    results: list[ScrapeOutcome | None] = [None] * len(targets)
    offsets = _bucket_offsets(buckets)
    total = len(targets)
    state = {"completed": 0}
    lock = threading.Lock()

    def publish(stage_text: str) -> None:
        """Share the current stage without letting a reporter break the round."""

        if stage is None:
            return
        with lock:
            done = state["completed"]
        stage(stage_text, min(done + 1, total), total)

    def is_cancelled() -> bool:
        return cancelled is not None and cancelled()

    def run_bucket(index: int) -> None:
        if capturer_factory is not None:
            bucket_capture = capturer_factory(index)
        else:
            assert capture is not None  # guarded above
            bucket_capture = capture
        # Resolved once per worker: the hook belongs to the session this bucket's
        # capturer drives, so it cannot differ between the bucket's targets.
        relogin = _relogin_hook(bucket_capture)
        start = offsets[index]
        for offset, target in enumerate(buckets[index]):
            if is_cancelled():
                logger.info("Netcare round cancelled before %s", target.target)
                return
            publish(branch_stage(target.name, start + offset + 1, total))
            outcome = _capture_one(cache, target, bucket_capture, day_partition, relogin)
            with lock:
                results[start + offset] = outcome
                state["completed"] += 1
                done = state["completed"]
            if progress is not None:
                progress(outcome, done, total)

    if len(buckets) <= 1:
        run_bucket(0)
    else:
        with ThreadPoolExecutor(max_workers=len(buckets), thread_name_prefix="netcare") as pool:
            for future in [pool.submit(run_bucket, i) for i in range(len(buckets))]:
                future.result()

    return [o for o in results if o is not None]


def _narrator(stage: StageCallback | None, total: int) -> Callable[[str], None] | None:
    """Adapt a 3-argument stage callback to the 1-argument login hook.

    Login happens before any target completes, so the counters stay at zero and
    only the text carries information.
    """

    if stage is None:
        return None

    def narrate(text: str) -> None:
        stage(text, 0, max(1, total))

    return narrate


def _bucket_offsets(buckets: Sequence[Sequence[NetcareTarget]]) -> list[int]:
    """Return the start index of each bucket in the flat result list."""

    offsets: list[int] = []
    running = 0
    for bucket in buckets:
        offsets.append(running)
        running += len(bucket)
    return offsets


class NetcareService:
    """Owns the cache, the target list, and the priority refresh queue."""

    def __init__(
        self,
        cache: NetcareCache,
        targets: Sequence[NetcareTarget],
        priority_queue: list[str] | None = None,
    ) -> None:
        self.cache = cache
        self.targets = list(targets)
        self._known = {t.target for t in self.targets}
        self._priority: list[str] = list(priority_queue or [])

    def stats(self, day: datetime | str | None = None) -> dict[str, Any]:
        """Return manifest counters used by the API and daemon logs."""

        manifest = self.cache.read_manifest(day)
        counts: dict[str, int] = {}
        for entry in manifest.values():
            status = str(entry.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "total": len(self.targets),
            "day": normalize_day_key(day) if day is not None else LIVE_DAY,
            "cached_images": sum(1 for t in self.targets if self.cache.has_image(t.target, day)),
            "by_status": counts,
            "ok": counts.get("ok", 0),
            "stale": counts.get("stale", 0),
            "error": counts.get("error", 0),
            "no_graph": counts.get("no_graph", 0),
            "pending": counts.get("pending", 0),
        }

    def refresh_targets(self, target_ids: Sequence[str]) -> bool:
        """Queue specific targets for the next round. False when none are known."""

        queued = [t for t in target_ids if t in self._known]
        for target_id in queued:
            if target_id not in self._priority:
                self._priority.append(target_id)
        return bool(queued)

    def pending_priority(self) -> set[str]:
        """Return the currently queued priority targets."""

        return set(self._priority)

    def drain_priority(self) -> list[str]:
        """Return and clear the queued priority targets."""

        queued, self._priority = self._priority, []
        return queued

    def query(
        self,
        window: tuple[datetime, datetime] | None = None,
        target_ids: Sequence[str] | None = None,
        pool: SessionPool | None = None,
        base_url: str | None = None,
        workers: int = DEFAULT_WORKERS,
        progress: ProgressCallback | None = None,
        day: datetime | str | None = None,
        stage: StageCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[ScrapeOutcome]:
        """Capture an arbitrary time window on demand into a cache partition.

        A window inside today refreshes the live flat cache so the dashboard keeps
        its single-latest-file behaviour; anything older is written to its own
        ``{YYYY-MM-DD}`` partition (FR-15.4). A partition that already holds every
        requested capture is reported from cache without waking a browser.

        ``stage`` narrates the round to the dashboard and ``cancelled`` lets the
        operator abandon it; both are advisory, so a missing callback only costs
        visibility, never the capture.

        A query with no injected ``pool`` builds its own, one session per worker,
        each with an isolated profile directory. Sharing a single driver across
        the fan-out is what made every capture fail with ``Graph did not render``
        (FR-16.2).
        """

        today = datetime.now()
        if window is None:
            start, end = today.replace(hour=0, minute=0, second=0, microsecond=0), today
        else:
            start, end = window
        partition = normalize_day_key(day) if day is not None else None
        selected = self._select(target_ids)
        url = base_url or global_settings.netcare_base_url

        cached = self._cached_outcomes(partition, selected, progress)
        if cached is not None:
            return cached

        if stage is not None:
            stage(STAGE_OPENING_BROWSER, 0, max(1, len(selected)))

        effective_workers = normalize_workers(workers)
        owned_pool = pool is None
        if pool is None:
            pool = SessionPool(
                lambda worker: build_session_from_settings(worker=worker),
                effective_workers,
                on_login_stage=_narrator(stage, len(selected)),
            )
        else:
            # Never ask a pool for more sessions than it agreed to serve.
            if effective_workers > pool.size:
                logger.warning(
                    "Limiting the query to %d worker(s); the supplied pool has %d",
                    pool.size,
                    pool.size,
                )
                effective_workers = pool.size

        try:
            return scrape_round(
                self.cache,
                selected,
                None,
                effective_workers,
                progress,
                partition,
                capturer_factory=lambda index: _per_target_capture(
                    pool.get(index), url, window=(start, end)
                ),
                stage=stage,
                cancelled=cancelled,
            )
        finally:
            if owned_pool:
                pool.close()

    def _cached_outcomes(
        self,
        partition: str | None,
        selected: Sequence[NetcareTarget],
        progress: ProgressCallback | None,
    ) -> list[ScrapeOutcome] | None:
        """Return cache-derived outcomes when a historical partition is complete.

        The live partition is always re-captured: it is the "latest" view, so a
        cached hit there would make the dashboard stale instead of fresh.
        """

        if partition is None or not selected:
            return None
        if not self.cache.partition_ready(partition, [t.target for t in selected]):
            return None

        logger.info("Netcare partition %s served from cache", partition)
        manifest = self.cache.read_manifest(partition)
        outcomes: list[ScrapeOutcome] = []
        total = len(selected)
        for index, target in enumerate(selected, start=1):
            status = str(manifest.get(target.target, {}).get("status", STATUS_OK))
            if status in (STATUS_OK, STATUS_STALE):
                outcomes.append(ScrapeOutcome.from_ok(target.target))
            else:
                outcomes.append(
                    ScrapeOutcome.from_error(target.target, "Cached capture unavailable")
                )
            if progress is not None:
                progress(outcomes[-1], index, total)
        return outcomes

    def _select(self, target_ids: Sequence[str] | None) -> list[NetcareTarget]:
        """Return the requested targets in catalog order, defaulting to all."""

        if not target_ids:
            return list(self.targets)
        wanted = set(target_ids)
        return [t for t in self.targets if t.target in wanted]


class NetcareDaemon:
    """Continuous scrape loop with graceful shutdown."""

    def __init__(
        self,
        service: NetcareService,
        run_round: RoundRunner,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        sleeper: Callable[[int], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.run_round = run_round
        self.interval_seconds = max(1, interval_seconds)
        self._sleep = sleeper or _wait
        self._on_close = on_close

    def install_signal_handlers(self, stop_event: threading.Event) -> None:
        """Ask the loop to stop on SIGINT/SIGTERM (best effort on non-POSIX)."""

        install_shutdown_handlers(stop_event)

    def run(self, stop_event: threading.Event | None = None, max_rounds: int | None = None) -> int:
        """Run scrape rounds until stopped. Returns the number of completed rounds."""

        stop = stop_event or threading.Event()
        completed = 0
        while not stop.is_set():
            if max_rounds is not None and completed >= max_rounds:
                break
            try:
                outcomes = self.run_round()
                completed += 1
                ok = sum(1 for o in outcomes if o.status == "ok")
                logger.info("Netcare round complete: %s/%s targets captured", ok, len(outcomes))
            except Exception as exc:
                logger.error("Netcare round failed: %s", exc)
                completed += 1
            if max_rounds is not None and completed >= max_rounds:
                break
            self._sleep(self.interval_seconds)
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception as exc:  # pragma: no cover - defensive shutdown
                logger.debug("Netcare pool close failed: %s", exc)
        logger.info("Netcare daemon stopped after %s round(s)", completed)
        return completed


def _wait(seconds: int) -> None:
    """Interruptible sleep so SIGTERM is handled promptly."""

    threading.Event().wait(seconds)


class SessionPool:
    """Hands each worker index its own browser session.

    A Selenium driver is not thread-safe, so sharing one across the pool would
    interleave navigations and corrupt captures. Sessions are keyed on the
    worker *index* rather than on whichever thread happened to ask, so worker
    ``i`` always drives the same browser, and thus the same isolated profile
    directory, for the life of the pool (FR-16.2).

    An out-of-range index raises instead of folding into another worker's
    session: silently handing two threads one driver is exactly the failure
    this pool exists to prevent.
    """

    def __init__(
        self,
        factory: SessionFactory,
        size: int = DEFAULT_WORKERS,
        on_login_stage: Callable[[str], None] | None = None,
    ) -> None:
        self._factory = factory
        self._size = max(1, int(size))
        self._on_login_stage = on_login_stage
        self._sessions: dict[int, NetcareSession] = {}
        self._index_locks: dict[int, threading.Lock] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """Return the configured number of worker sessions."""

        return self._size

    def get(self, worker: int) -> NetcareSession:
        """Return worker ``worker``'s session, building and logging in on first use."""

        index = self._index(worker)
        with self._lock:
            session = self._sessions.get(index)
            if session is not None:
                return session
            # A per-index lock lets different workers log in concurrently while
            # still guaranteeing one session per index.
            index_lock = self._index_locks.setdefault(index, threading.Lock())

        with index_lock:
            with self._lock:
                session = self._sessions.get(index)
            if session is not None:
                return session
            session = self._build(index)
            with self._lock:
                self._sessions[index] = session
            return session

    def close(self) -> None:
        """Close every session the pool created. Safe to call more than once."""

        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._index_locks.clear()
        for session in sessions:
            session.close()

    def _index(self, worker: int) -> int:
        """Validate a worker index against the pool size."""

        try:
            index = int(worker)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Worker index must be an integer, got {worker!r}") from exc
        if not 0 <= index < self._size:
            raise ValueError(f"Worker index {index} is outside a pool of {self._size} session(s)")
        return index

    def _build(self, index: int) -> NetcareSession:
        """Create and authenticate the session for one worker."""

        session = self._factory(index)
        if not self._login(session):
            session.close()
            raise RuntimeError("Unable to establish a TelkomCare session")
        return session

    def _login(self, session: NetcareSession) -> bool:
        """Log in, narrating the slow part when the caller wants a stage hook."""

        if self._on_login_stage is None:
            return session.login()
        return session.login(on_stage=self._on_login_stage)


def _per_target_capture(
    session: NetcareSession,
    base_url: str,
    day: datetime | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> TargetCapturer:
    """Return a capturer that drives the portal form for one target.

    ``window`` pins an explicit start/end pair; otherwise the whole of ``day``
    (defaulting to today) is requested.
    """

    def capture(target: NetcareTarget) -> bytes:
        driver = session.driver
        if driver is None:
            raise RuntimeError("Netcare browser session is not running")
        if window is not None:
            start, end = format_date_range(*window)
        else:
            start, end = format_date_filter(day or datetime.now())
        logger.debug(
            "Netcare target %s -> %s [%s .. %s]",
            target.target,
            build_graph_url(target, base_url),
            start,
            end,
        )
        return _capture_via_driver(
            driver,
            target,
            start,
            end,
            session.page_timeout_seconds,
            base_url=base_url,
        )

    def relogin() -> bool:
        """Re-authenticate the portal session this capturer drives.

        Attached to the capturer rather than kept beside it, because the round
        only ever holds the callable: an expired session has to be recoverable
        from whatever the caller handed in, with no extra plumbing per worker.
        """

        logger.info("TelkomCare session expired; logging in again")
        return session.login()

    capture.relogin = relogin  # type: ignore[attr-defined]
    return capture


class NetcareSessionExpiredError(RuntimeError):
    """The portal bounced the capture to its login form.

    Raised as its own type because the fix is a re-login, not a longer wait: a
    round that kept waiting would burn the full graph timeout on every remaining
    target and still come back empty.
    """


def _portal_helpers(driver: Any, timeout_seconds: int) -> tuple[Any, Any, Any, Any]:
    """Import Selenium lazily and return the helpers the capture path needs.

    Selenium is imported here rather than at module scope so the FastAPI process
    never loads it until a capture actually needs a browser.
    """

    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait

    return By, ec, WebDriverWait(driver, timeout_seconds), Keys


def _reject_expired_session(driver: Any) -> None:
    """Fail fast when the portal has redirected the capture to the login form."""

    try:
        current = str(getattr(driver, "current_url", "") or "")
    except Exception:  # pragma: no cover - a dead driver fails loudly further down
        return
    if LOGIN_PATH in current:
        raise NetcareSessionExpiredError(
            f"TelkomCare session expired: redirected to {current}; re-login is required"
        )


def _visible_overlay(driver: Any) -> Any | None:
    """Return the first loading overlay currently on screen, if any.

    An overlay element that exists in the DOM but is hidden is not blocking
    anything, so visibility — not mere presence — decides. Treating a hidden
    overlay as pending would stall every capture for the full timeout on a
    portal that keeps its spinners permanently in the markup.
    """

    from selenium.webdriver.common.by import By

    try:
        elements = driver.find_elements(By.CSS_SELECTOR, LOADING_OVERLAY_SELECTOR)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not inspect TelkomCare loading overlays: %s", exc)
        return None
    for element in elements:
        try:
            if element.is_displayed():
                return element
        except Exception:  # pragma: no cover - stale element
            continue
    return None


def _dismiss_alert_if_present(driver: Any) -> bool:
    """Accept a pending browser alert, returning True when one was dismissed.

    The portal raises modal JavaScript alerts for its own failures — most often
    a DataTables warning when a table request returns JSON where HTML was
    expected. A modal alert blocks every click and keystroke, so a capture that
    does not clear it first hangs on the next ``wait.until`` and is eventually
    reported as ``Graph did not render``, which names the wrong cause.

    Absent alert is the overwhelmingly common case and must stay cheap: the
    settle sleep is only paid when something was actually dismissed.
    """

    try:
        alert = driver.switch_to.alert
    except Exception:
        return False

    text = ""
    try:
        text = str(getattr(alert, "text", "") or "").strip()
    except Exception:  # pragma: no cover - a driver that cannot read the text
        # The alert text is only used for the log line; failing to read it must
        # not stop the alert from being accepted.
        logger.debug("TelkomCare alert exposed no readable text")
    try:
        alert.accept()
    except Exception as exc:  # pragma: no cover - dismissed by someone else
        logger.debug("TelkomCare alert vanished before it could be accepted: %s", exc)
        return False
    logger.warning("Dismissed a TelkomCare alert: %s", text or "(no text)")
    _sleep(ALERT_SETTLE_SECONDS)
    return True


def _wait_for_loading_overlay(
    driver: Any,
    timeout_seconds: float = OVERLAY_TIMEOUT_SECONDS,
) -> bool:
    """Wait for every TelkomCare loading overlay to detach. True when it settled.

    A filter click fires an AJAX request that renders the graph before it
    finishes. Reading ``graph.php`` in that window finds the *previous* branch's
    image still on the page, which is how a capture silently stored the wrong
    graph. Timing out is not treated as fatal: the graph wait that follows is
    the real gate, and an overlay that outlives its timeout means a slow portal,
    not a missing graph.
    """

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        overlay = _visible_overlay(driver)
        if overlay is None:
            return True
        if time.monotonic() >= deadline:
            logger.warning("TelkomCare overlay still present after %.0fs", timeout_seconds)
            return False
        _sleep(OVERLAY_POLL_SECONDS)


def _submit_target(
    driver: Any,
    by: Any,
    ec: Any,
    wait: Any,
    keys: Any,
    target: NetcareTarget,
) -> None:
    """Type the target into the search form and press "Show Graph".

    This is the step the portal has no shortcut for: the graph URL is identical
    for every branch, so without a submitted target the page stays on an empty
    form and no image is ever produced.
    """

    field = wait.until(ec.presence_of_element_located((by.NAME, TARGET_INPUT_NAMES[target.type])))
    field.clear()
    field.send_keys(target.target)
    field.send_keys(keys.ENTER)

    button = wait.until(ec.element_to_be_clickable((by.CSS_SELECTOR, SHOW_GRAPH_SELECTOR)))
    # Scroll first, then click through JS: this portal floats a transparent
    # overlay over the button that swallows a real click.
    driver.execute_script("arguments[0].scrollIntoView(true);", button)
    driver.execute_script("arguments[0].click();", button)


def _wait_for_detail_ready(
    driver: Any,
    by: Any,
    ec: Any,
    wait: Any,
    target: NetcareTarget,
) -> bool:
    """Wait for the filter controls that only exist once a target is open.

    SID mode is identified by its unlabelled Filter button; graph-title mode by
    the three ids it gives its own date filter.
    """

    try:
        if target.type is NetcareTargetType.SID:
            wait.until(ec.presence_of_element_located((by.XPATH, SID_FILTER_BUTTON_XPATH)))
            return True
        for element_id in ("startdate", "enddate", "graphfilter"):
            wait.until(ec.presence_of_element_located((by.ID, element_id)))
        return True
    except Exception as exc:
        logger.debug("Detail page not ready for %s: %s", target.target, exc)
        return False


def _capture_via_driver(
    driver: Any,
    target: NetcareTarget,
    start: str,
    end: str,
    timeout_seconds: int = 25,
    *,
    base_url: str | None = None,
) -> bytes:
    """Render one branch graph through the portal's own search form.

    The portal does not accept a target in the URL. The browser has to type the
    target ID into the form, submit it, click "Show Graph", apply the date range,
    and only then does a ``graph.php`` image appear. Navigating straight to the
    graph page and waiting for an image leaves the browser on an empty form, so
    every target used to fail with ``Graph did not render`` after the full
    timeout.

    The rendered image is isolated from the rest of the page before it is
    screenshotted, so pop-ups, banners, and overlays cannot leak into the
    capture.

    Raises:
        NetcareSessionExpiredError: if the portal redirected to the login form,
            so a round stops immediately instead of waiting out every target.
        RuntimeError: if the form, the filter controls, or the graph never appear.
    """

    by, ec, wait, keys = _portal_helpers(driver, timeout_seconds)
    driver.get(build_graph_url(target, base_url or global_settings.netcare_base_url))
    _reject_expired_session(driver)
    # Before any DOM work: a modal alert raised by a previous target would
    # swallow every click and keystroke that follows.
    _dismiss_alert_if_present(driver)

    _submit_target(driver, by, ec, wait, keys, target)
    # Submitting the form is itself what an expired session trips, so the check
    # repeats before any further waiting.
    _reject_expired_session(driver)

    if not _wait_for_detail_ready(driver, by, ec, wait, target):
        raise RuntimeError(f"Detail page did not become ready for {target.target}")

    # An unapplied filter would still render *a* graph, just for the wrong
    # window, so this fails loudly instead of quietly charting the wrong range.
    if not _apply_date_filter(driver, by, ec, wait, target, start, end):
        raise RuntimeError(f"Date filter could not be applied for {target.target}")

    # The filter click only starts the AJAX render; reading the image before the
    # overlay detaches picks up whatever graph was already on the page.
    _wait_for_loading_overlay(driver)

    if not _wait_for_graph(driver, by, timeout_seconds):
        raise RuntimeError(f"Graph did not render for {target.target}")

    _isolate_graph(driver)
    try:
        element = _find_graph(driver, by)
        if element is not None:
            return bytes(element.screenshot_as_png)
    finally:
        _restore_graph_page(driver)
    raise RuntimeError("Graph element lost before capture")


def _find_graph(driver: Any, by: Any) -> Any | None:
    """Return the first ``graph.php`` image wide enough to be a real graph."""

    for element in driver.find_elements(by.XPATH, GRAPH_IMAGE_XPATH):
        try:
            width = driver.execute_script("return arguments[0].naturalWidth;", element)
        except Exception:  # pragma: no cover - stale element, try the next candidate
            continue
        if width and int(width) > MIN_GRAPH_WIDTH_PX:
            return element
    return None


def _wait_for_graph(driver: Any, by: Any, timeout_seconds: int) -> bool:
    """Poll until the graph image has rendered, or the timeout elapses.

    Each pass also looks for a modal alert and a visible loading overlay: either
    one freezes the render, so waiting without clearing them burns the whole
    timeout on a page that is merely blocked rather than broken.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _dismiss_alert_if_present(driver)
        _wait_for_loading_overlay(driver, min(OVERLAY_TIMEOUT_SECONDS, timeout_seconds))
        if _find_graph(driver, by) is not None:
            return True
        _sleep(1)
    return False


def _set_field_value(driver: Any, field: Any, value: str) -> None:
    """Write a value into a portal field and announce the change.

    The value is assigned through JS rather than typed, and a ``change`` event is
    dispatched, because the portal keeps its filter state on the event; silent
    keystrokes leave the graph showing the wrong range.
    """

    driver.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('change'));",
        field,
        value,
    )


def _apply_sid_date_filter(driver: Any, by: Any, ec: Any, wait: Any, start: str, end: str) -> None:
    """Set the two unlabelled date inputs that precede SID mode's Filter button.

    The SID page gives neither input an id, so they are located positionally.
    Fewer than two visible inputs means the page layout changed, so this raises
    rather than writing the range into whatever fields happen to be there.
    """

    button_locator = (by.XPATH, SID_FILTER_BUTTON_XPATH)
    wait.until(ec.presence_of_element_located(button_locator))
    inputs = driver.find_elements(by.XPATH, SID_DATE_INPUT_XPATH)
    if len(inputs) < 2:
        raise LookupError(
            f"Expected 2 SID date inputs before the Filter button, found {len(inputs)}"
        )
    _set_field_value(driver, inputs[-2], start)
    _set_field_value(driver, inputs[-1], end)
    button = wait.until(ec.element_to_be_clickable(button_locator))
    driver.execute_script("arguments[0].click();", button)


def _apply_graph_title_date_filter(
    driver: Any,
    by: Any,
    ec: Any,
    wait: Any,
    start: str,
    end: str,
) -> None:
    """Set the date range through the ids graph-title mode puts on its fields."""

    _set_field_value(
        driver,
        wait.until(ec.presence_of_element_located((by.ID, "startdate"))),
        start,
    )
    _set_field_value(
        driver,
        wait.until(ec.presence_of_element_located((by.ID, "enddate"))),
        end,
    )
    button = wait.until(ec.element_to_be_clickable((by.ID, "graphfilter")))
    driver.execute_script("arguments[0].click();", button)


def _apply_date_filter(
    driver: Any,
    by: Any,
    ec: Any,
    wait: Any,
    target: NetcareTarget,
    start: str,
    end: str,
) -> bool:
    """Apply the requested date range through the mode's own filter controls.

    Returns False when the controls never appear, so a caller can tell a portal
    that changed shape apart from one that simply had no traffic to draw.
    """

    try:
        if target.type is NetcareTargetType.SID:
            _apply_sid_date_filter(driver, by, ec, wait, start, end)
        else:
            _apply_graph_title_date_filter(driver, by, ec, wait, start, end)
    except Exception as exc:
        logger.warning("Date filter unavailable for %s: %s", target.target, exc)
        return False
    return True


def _isolate_graph(driver: Any) -> None:
    """Hide every sibling element so only the graph image is captured."""

    try:
        driver.execute_script(GRAPH_ISOLATION_SCRIPT)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Graph isolation failed: %s", exc)


def _restore_graph_page(driver: Any) -> None:
    """Undo DOM isolation after a capture."""

    try:
        driver.execute_script(GRAPH_RESTORE_SCRIPT)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Graph isolation restore failed: %s", exc)


def build_service_from_settings(
    config: Settings | None = None,
    cache_dir: Path | str | None = None,
) -> NetcareService:
    """Assemble a NetcareService from application settings."""

    cfg = config or global_settings
    resolved_cache = Path(cache_dir) if cache_dir is not None else Path(cfg.netcare_cache_dir)
    targets = resolve_targets(cfg.netcare_catalog_file)
    return NetcareService(cache=NetcareCache(resolved_cache), targets=targets)


def worker_profile_dir(profile_dir: Path, worker: int | None) -> Path:
    """Return the profile directory dedicated to one worker.

    Concurrent Chrome instances sharing one user-data directory collide on its
    lockfile, so every worker gets its own subdirectory (FR-16.2). ``None``
    keeps the configured directory as-is for a caller that runs a single
    browser.
    """

    base = Path(profile_dir).expanduser()
    return base if worker is None else base / f"worker_{worker}"


def build_session_from_settings(
    config: Settings | None = None,
    worker: int | None = None,
) -> NetcareSession:
    """Assemble a NetcareSession from application settings.

    ``worker`` selects an isolated profile directory so parallel workers never
    share a Chrome user-data directory (FR-16.2).
    """

    cfg = config or global_settings
    solver = GeminiCaptchaSolver(
        api_keys=list(cfg.gemini_api_keys),
        models=list(cfg.gemini_models),
        timeout_seconds=cfg.gemini_timeout_seconds,
    )
    return NetcareSession(
        base_url=cfg.netcare_base_url,
        cookie_path=Path(cfg.netcare_cache_dir) / "cookies.json",
        profile_dir=worker_profile_dir(Path(cfg.netcare_profile_dir), worker),
        headless=cfg.netcare_headless,
        username=cfg.telkom_user,
        password=cfg.telkom_password,
        totp_secret=cfg.totp_secret,
        auto_login_enabled=cfg.netcare_auto_login,
        captcha_solver=solver,
        page_timeout_seconds=cfg.netcare_page_timeout_seconds,
    )


def build_daemon_from_settings(config: Settings | None = None) -> NetcareDaemon:
    """Assemble a fully wired daemon: cache, session pool, targets, scrape round."""

    cfg = config or global_settings
    service = build_service_from_settings(cfg)
    # Each worker drives its own browser in its own profile directory, so a
    # round never interleaves navigations on a shared driver (FR-16.2).
    pool = SessionPool(
        lambda worker: build_session_from_settings(cfg, worker=worker),
        cfg.netcare_workers,
    )

    def run_round() -> list[ScrapeOutcome]:
        today = datetime.now()
        priority = set(service.drain_priority())
        ordered = [t for t in service.targets if t.target in priority]
        ordered += [t for t in service.targets if t.target not in priority]

        def capturer_for(worker: int) -> TargetCapturer:
            return _per_target_capture(pool.get(worker), cfg.netcare_base_url, today)

        return scrape_round(
            service.cache,
            ordered,
            None,
            cfg.netcare_workers,
            capturer_factory=capturer_for,
        )

    return NetcareDaemon(
        service=service,
        run_round=run_round,
        interval_seconds=cfg.netcare_poll_interval_seconds,
        on_close=pool.close,
    )


def install_shutdown_handlers(stop_event: threading.Event) -> None:
    """Ask the scrape loop to stop on SIGINT/SIGTERM."""

    def _handler(_signum: int, _frame: Any) -> None:
        logger.info("Netcare shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):  # pragma: no cover - non-main thread
            logger.debug("Could not install handler for %s", sig)


__all__ = [
    "ALERT_SETTLE_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_WORKERS",
    "GRAPH_IMAGE_XPATH",
    "GRAPH_ISOLATION_SCRIPT",
    "GRAPH_RESTORE_SCRIPT",
    "LOADING_OVERLAY_SELECTOR",
    "LOGIN_PATH",
    "MIN_GRAPH_WIDTH_PX",
    "OVERLAY_POLL_SECONDS",
    "OVERLAY_TIMEOUT_SECONDS",
    "SHOW_GRAPH_SELECTOR",
    "SID_DATE_INPUT_XPATH",
    "SID_FILTER_BUTTON_XPATH",
    "TARGET_INPUT_NAMES",
    "NetcareDaemon",
    "NetcareService",
    "NetcareSessionExpiredError",
    "SessionFactory",
    "SessionPool",
    "build_daemon_from_settings",
    "build_service_from_settings",
    "build_session_from_settings",
    "install_shutdown_handlers",
    "normalize_workers",
    "partition_targets",
    "scrape_round",
    "worker_profile_dir",
]
