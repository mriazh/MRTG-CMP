"""On-demand Netcare graph queries with live progress reporting.

Picking a time preset on the dashboard should not block the browser, so
``POST /api/netcare/query`` starts a background job and hands the client a job
id. The client polls ``GET /api/netcare/status`` while the polite loading dialog
counts down the ETA (FR-15.3).

The tracker is deliberately process-local: the scraper daemon owns the browser
sessions, so a query that needs new captures is delegated to a caller-supplied
runner. A runner that reports a historical day is already captured short-circuits
to a cache hit instead of waking a browser (FR-15.4).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .scraper import ScrapeOutcome, normalize_day_key

logger = logging.getLogger("mrtg_cmp.netcare.query")

#: Seconds one worker takes per target, used until real timings arrive. A target
#: is a form submission plus a filtered graph render rather than a bare page
#: load, and a measured 18-branch round over 3 workers lands near 1m 15s. The
#: opening figure is deliberately rounded up to two minutes so the dialog starts
#: calm rather than having to jump forward: the measured rate takes over from the
#: first completed target anyway, so an optimistic start buys nothing.
SECONDS_PER_TARGET_PER_WORKER = 20.0

#: Pool width assumed when a caller does not name one. Matches the default the
#: web layer reads from the settings.
DEFAULT_WORKERS = 3

#: Number of most recent jobs kept in memory.
JOB_HISTORY = 20

#: Operator-facing stage labels, streamed to the dashboard while a job runs.
#: Indonesian so they read natively next to the rest of the dialog.
STAGE_QUEUED = "Menunggu worker..."
STAGE_OPENING_BROWSER = "Membuka browser & login..."
STAGE_SOLVING_CAPTCHA = "Menyelesaikan CAPTCHA..."
STAGE_DONE = "Selesai"
STAGE_FAILED = "Gagal"
STAGE_CANCELLED = "Dibatalkan operator"

#: Runs one window and returns its outcomes. Injected so the web layer can
#: delegate to the daemon (or a stub in tests) without importing it here.
QueryRunner = Callable[..., Sequence[ScrapeOutcome]]

#: Receives the current stage text and the running (done, total) counters.
StageCallback = Callable[[str, int, int], None]


def branch_stage(name: str, done: int, total: int) -> str:
    """Return the stage shown while one branch graph is being captured."""

    return f"Mengambil link: {name} ({done}/{total})..."


@dataclass
class QueryJob:
    """One in-flight or finished Netcare query."""

    job_id: str
    preset: str
    start: datetime
    end: datetime
    total: int
    day: str | None
    target_ids: tuple[str, ...] = ()
    #: Width of the pool the runner is fanning out over. Stored on the job
    #: because the opening ETA is read straight off the snapshot.
    workers: int = DEFAULT_WORKERS
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    completed: int = 0
    outcomes: list[ScrapeOutcome] = field(default_factory=list)
    error: str | None = None
    served_from_cache: bool = False
    stage: str = STAGE_QUEUED
    cancel_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def running(self) -> bool:
        """Return True while the job has not settled."""

        return self.finished_at is None

    def record(self, outcome: ScrapeOutcome, completed: int) -> None:
        """Store one finished target capture."""

        with self.lock:
            self.outcomes.append(outcome)
            self.completed = completed

    def set_stage(self, stage: str) -> None:
        """Publish the stage the operator should see while this job runs."""

        with self.lock:
            self.stage = stage

    def request_cancel(self) -> bool:
        """Flag the job for cancellation. False when it already settled."""

        with self.lock:
            if self.finished_at is not None or self.cancel_requested:
                return False
            self.cancel_requested = True
            self.stage = STAGE_CANCELLED
            return True

    def finish(self, error: str | None = None) -> None:
        """Mark the job settled, keeping the first error reported."""

        with self.lock:
            if self.finished_at is None:
                self.finished_at = time.monotonic()
            if error and self.error is None:
                self.error = error
            if not self.cancel_requested:
                self.stage = STAGE_FAILED if self.error else STAGE_DONE

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe progress report for the status endpoint."""

        with self.lock:
            completed = self.completed
            finished_at = self.finished_at
            error = self.error
            stage = self.stage
            cancelled = self.cancel_requested
            ok = sum(1 for o in self.outcomes if o.status == "ok")
            stale = sum(1 for o in self.outcomes if o.status == "stale")
            failed = len(self.outcomes) - ok - stale
            end_of_window = finished_at if finished_at is not None else time.monotonic()
            elapsed = end_of_window - self.started_at

        total = max(1, self.total)
        remaining = max(0, self.total - completed)
        if remaining == 0:
            eta = 0.0
        elif completed > 0:
            eta = elapsed / completed * remaining
        else:
            # Nothing has completed yet, so there is no rate to extrapolate from.
            # Fall back to the per-target cost divided by the pool width, the same
            # way estimate_seconds does, so the dialog counts down from the real
            # duration instead of a serial one.
            eta = remaining / max(1, self.workers) * SECONDS_PER_TARGET_PER_WORKER
        return {
            "job_id": self.job_id,
            "preset": self.preset,
            "day": self.day,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "running": self.running,
            "completed": completed,
            "total": self.total,
            "targets": list(self.target_ids),
            "percent": round(min(100.0, completed / total * 100), 1),
            "eta_seconds": int(round(eta)),
            "elapsed_seconds": int(round(elapsed)),
            "ok": ok,
            "stale": stale,
            "failed": failed,
            "served_from_cache": self.served_from_cache,
            "error": error,
            "stage": stage,
            "cancelled": cancelled,
        }




class NetcareQueryTracker:
    """Starts background queries and tracks their progress for the dashboard."""

    def __init__(
        self,
        runner: QueryRunner,
        total_targets: int = 18,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        self._runner = runner
        self._default_total = total_targets
        self._workers = workers
        self._jobs: dict[str, QueryJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def active(self) -> QueryJob | None:
        """Return the running job, if any, so a second request can join it."""

        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job is not None and job.running:
                    return job
        return None

    def get(self, job_id: str) -> QueryJob | None:
        """Return a job by id."""

        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> QueryJob | None:
        """Flag a running job for cancellation and return it.

        Cancellation is cooperative: the runner decides when to stop, so a job
        that already settled — or a stale id — returns None and the caller can
        answer 404 without pretending anything was cancelled.
        """

        job = self.get(job_id)
        if job is None:
            return None
        return job if job.request_cancel() else None

    def start(
        self,
        preset: str,
        start: datetime,
        end: datetime,
        day: str | None,
        total: int,
        target_ids: Sequence[str] | None = None,
    ) -> QueryJob:
        """Begin a query in the background and return the job immediately.

        If a query for the same window is already running it is returned as-is
        rather than starting a second browser fan-out.
        """

        existing = self._match_running(preset, start, end, day, target_ids)
        if existing is not None:
            return existing

        job = QueryJob(
            job_id=uuid.uuid4().hex,
            preset=preset,
            start=start,
            end=end,
            total=max(1, total),
            day=day,
            target_ids=tuple(target_ids or ()),
            workers=self._workers,
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._trim()
        thread = threading.Thread(
            target=self._execute,
            args=(job, target_ids),
            name=f"netcare-query-{job.preset}",
            daemon=True,
        )
        thread.start()
        return job

    def _match_running(
        self,
        preset: str,
        start: datetime,
        end: datetime,
        day: str | None,
        target_ids: Sequence[str] | None,
    ) -> QueryJob | None:
        """Return a running job covering the same window and target set."""

        wanted = set(target_ids or ())
        with self._lock:
            candidates = [self._jobs[j] for j in self._order if j in self._jobs]
        for job in reversed(candidates):
            if not job.running:
                continue
            if job.preset != preset or job.day != day:
                continue
            if job.start != start or job.end != end:
                continue
            if set(job.target_ids) == wanted:
                return job
        return None

    def _trim(self) -> None:
        """Drop the oldest settled jobs so memory stays bounded."""

        while len(self._order) > JOB_HISTORY:
            for index, job_id in enumerate(self._order):
                job = self._jobs.get(job_id)
                if job is not None and not job.running:
                    del self._order[index]
                    self._jobs.pop(job_id, None)
                    break
            else:
                return

    def _execute(self, job: QueryJob, target_ids: Sequence[str] | None) -> None:
        """Run the job off the request thread, never raising into the pool."""

        def on_progress(outcome: ScrapeOutcome, completed: int, _total: int) -> None:
            job.record(outcome, completed)

        def on_stage(stage: str, done: int, total: int) -> None:
            job.set_stage(stage)

        try:
            self._runner(
                window=(job.start, job.end),
                target_ids=target_ids,
                progress=on_progress,
                stage=on_stage,
                day=job.day,
                cancelled=lambda: job.cancel_requested,
            )
        except Exception as exc:
            logger.warning("Netcare query %s failed: %s", job.preset, exc)
            job.finish(str(exc))
        else:
            job.finish()


def partition_for(start: datetime, now: datetime | None = None) -> str | None:
    """Return the cache partition a window starting at ``start`` belongs to.

    A window that starts today or later refreshes the live flat cache; anything
    older gets its own ``{YYYY-MM-DD}`` partition so a fetched day is never
    overwritten by a later fetch.
    """

    reference = now or datetime.now()
    return None if start.date() >= reference.date() else normalize_day_key(start)


def estimate_seconds(total: int, workers: int = DEFAULT_WORKERS) -> int:
    """Return the opening ETA the dialog shows before any target completes.

    The per-worker cost is divided by the pool width, so adding workers lowers
    the estimate the way the real scrape does.
    """

    safe_total = max(1, int(total))
    effective = max(1, min(int(workers or 1), safe_total))
    return max(1, int(round(safe_total / effective * SECONDS_PER_TARGET_PER_WORKER)))


__all__ = [
    "DEFAULT_WORKERS",
    "JOB_HISTORY",
    "SECONDS_PER_TARGET_PER_WORKER",
    "STAGE_CANCELLED",
    "STAGE_DONE",
    "STAGE_FAILED",
    "STAGE_OPENING_BROWSER",
    "STAGE_QUEUED",
    "STAGE_SOLVING_CAPTCHA",
    "NetcareQueryTracker",
    "QueryJob",
    "QueryRunner",
    "StageCallback",
    "branch_stage",
    "estimate_seconds",
    "partition_for",
]
