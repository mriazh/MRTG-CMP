"""Unit tests for on-demand Netcare queries and their live progress reporting."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from mrtg_cmp.netcare.query import (
    SECONDS_PER_TARGET_PER_WORKER,
    STAGE_CANCELLED,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_OPENING_BROWSER,
    STAGE_QUEUED,
    STAGE_SOLVING_CAPTCHA,
    NetcareQueryTracker,
    QueryJob,
    branch_stage,
    estimate_seconds,
    partition_for,
)
from mrtg_cmp.netcare.scraper import ScrapeOutcome


def _window(days_ago: int = 0) -> tuple[datetime, datetime]:
    start = datetime.now() - timedelta(days=days_ago, hours=2)
    return start, start + timedelta(hours=2)


class RecordingRunner:
    """Runner double that reports a configurable number of targets."""

    def __init__(
        self,
        total: int = 18,
        error: str | None = None,
        blocking: bool = False,
        stages: Sequence[str] | None = None,
    ) -> None:
        self.total = total
        self.error = error
        self.blocking = blocking
        self.stages = list(stages) if stages is not None else None
        self.calls: list[dict[str, Any]] = []
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, **kwargs: Any) -> list[ScrapeOutcome]:
        self.calls.append(kwargs)
        progress = kwargs.get("progress")
        stage = kwargs.get("stage")
        if stage is not None:
            for text in self.stages or [
                branch_stage(f"Branch {index + 1}", index + 1, self.total)
                for index in range(self.total)
            ]:
                stage(text, 0, self.total)
        if progress is not None:
            for index in range(self.total):
                progress(ScrapeOutcome.from_ok(f"t{index}"), index + 1, self.total)
        if self.blocking:
            assert self.release.wait(timeout=5), "test never released the runner"
        if self.error:
            raise RuntimeError(self.error)
        self.finished.set()
        return []


def _settle(runner: RecordingRunner) -> None:
    assert runner.finished.wait(timeout=5), "query runner never finished"


# --- ETA and partition helpers ---------------------------------------------


def test_estimate_matches_the_documented_120s_for_18_targets_over_3_workers() -> None:
    """A measured round runs ~1m 15s, so the opening countdown starts at 2 minutes.

    The static figure is deliberately the slower of the two: a countdown that
    over-promises and then has to jump forward reads as a stall, while one that
    starts slightly long and counts down is calm.
    """

    assert estimate_seconds(18, 3) == 120


def test_estimate_scales_down_as_workers_are_added() -> None:
    """More workers means a lower opening estimate, matching the real scrape."""

    assert estimate_seconds(18, 1) > estimate_seconds(18, 3) > estimate_seconds(18, 6)


def test_estimate_never_returns_zero() -> None:
    assert estimate_seconds(1, 8) >= 1
    assert estimate_seconds(0, 3) >= 1


def test_estimate_clamps_a_bogus_worker_count() -> None:
    assert estimate_seconds(18, 0) == 18 * SECONDS_PER_TARGET_PER_WORKER
    assert estimate_seconds(18, 999) >= 1


def test_partition_for_treats_a_current_window_as_live() -> None:
    now = datetime(2026, 9, 20, 10, 0)
    assert partition_for(now, now=now) is None
    assert partition_for(now - timedelta(hours=1), now=now) is None


def test_partition_for_assigns_a_past_window_its_own_day() -> None:
    now = datetime(2026, 9, 20, 10, 0)
    assert partition_for(now - timedelta(days=1), now=now) == "2026-09-19"
    assert partition_for(now - timedelta(days=5), now=now) == "2026-09-15"


# --- Job snapshots ---------------------------------------------------------


def test_a_fresh_job_reports_zero_completion_and_a_positive_eta() -> None:
    job = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
    )
    snapshot = job.snapshot()

    assert snapshot["running"] is True
    assert snapshot["completed"] == 0
    assert snapshot["percent"] == 0.0
    assert snapshot["eta_seconds"] > 0
    assert snapshot["ok"] == 0


def test_the_initial_eta_divides_the_remaining_work_by_the_worker_pool() -> None:
    """The dialog counts down from the pool width, not from the target count.

    Without the divisor the opening estimate was the serial cost, so 18 targets
    opened at the full three-times figure instead of the 120s the dialog is
    calibrated to.
    """

    serial = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
        workers=1,
    )
    parallel = QueryJob(
        job_id="j2",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
        workers=3,
    )

    assert parallel.snapshot()["eta_seconds"] == 120
    assert parallel.snapshot()["eta_seconds"] == serial.snapshot()["eta_seconds"] // 3


def test_a_job_with_a_bogus_worker_count_still_reports_a_positive_eta() -> None:
    """A zero worker count must not divide by zero or zero out the countdown."""

    job = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
        workers=0,
    )

    assert job.snapshot()["eta_seconds"] == 18 * SECONDS_PER_TARGET_PER_WORKER


def test_the_measured_rate_takes_over_from_the_first_completed_target() -> None:
    """Once a target lands, real timings beat the static per-worker guess."""

    job = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
        workers=3,
    )
    job.record(ScrapeOutcome.from_ok("t0"), 1)
    job.started_at -= 6.0  # one target took six seconds

    snapshot = job.snapshot()
    assert snapshot["eta_seconds"] == round(6.0 * 17)


def test_a_finished_job_reports_a_zero_eta() -> None:
    job = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
    )
    for index in range(18):
        job.record(ScrapeOutcome.from_ok(f"t{index}"), index + 1)
    job.finish()

    snapshot = job.snapshot()
    assert snapshot["running"] is False
    assert snapshot["completed"] == 18
    assert snapshot["percent"] == 100.0
    assert snapshot["eta_seconds"] == 0
    assert snapshot["ok"] == 18
    assert snapshot["failed"] == 0


def test_a_failed_job_keeps_the_first_error() -> None:
    job = QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=1,
        day=None,
    )
    job.finish("first failure")
    job.finish("second failure")

    assert job.snapshot()["error"] == "first failure"
    assert job.running is False


def test_snapshot_carries_the_window_and_day() -> None:
    job = QueryJob(
        job_id="j1",
        preset="yesterday",
        start=datetime(2026, 9, 19, 0, 0),
        end=datetime(2026, 9, 19, 23, 55),
        total=1,
        day="2026-09-19",
    )
    snapshot = job.snapshot()

    assert snapshot["preset"] == "yesterday"
    assert snapshot["day"] == "2026-09-19"
    assert snapshot["start"] == "2026-09-19T00:00"
    assert snapshot["end"] == "2026-09-19T23:55"


# --- Tracker ---------------------------------------------------------------


def test_start_returns_immediately_and_records_progress() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    _settle(runner)

    assert job.job_id
    assert job.snapshot()["running"] is False
    assert job.snapshot()["completed"] == 18
    assert len(runner.calls) == 1
    assert runner.calls[0]["window"] == (start, end)
    assert runner.calls[0]["day"] is None


def test_start_stamps_the_configured_worker_count_on_the_job() -> None:
    """The job's fallback ETA has to know how wide the scrape pool is."""

    release = threading.Event()

    def stalled_runner(**kwargs: Any) -> list[ScrapeOutcome]:
        assert release.wait(timeout=5), "test never released the runner"
        return []

    tracker = NetcareQueryTracker(stalled_runner, workers=4)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    try:
        assert job.workers == 4
        # No target has completed here, so this is the opening estimate.
        assert job.snapshot()["eta_seconds"] == estimate_seconds(18, 4)
    finally:
        release.set()


def test_start_forwards_the_day_partition_to_the_runner() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window(days_ago=1)

    tracker.start("yesterday", start, end, "2026-09-19", total=18)
    _settle(runner)

    assert runner.calls[0]["day"] == "2026-09-19"


def test_start_forwards_the_requested_target_subset() -> None:
    runner = RecordingRunner(total=2)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=2, target_ids=["a", "b"])
    _settle(runner)

    assert runner.calls[0]["target_ids"] == ["a", "b"]
    assert job.snapshot()["targets"] == ["a", "b"]


def test_a_second_identical_request_joins_the_running_job() -> None:
    """A double-click must not start a second browser fan-out."""

    runner = RecordingRunner(blocking=True)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    first = tracker.start("today", start, end, None, total=18)
    second = tracker.start("today", start, end, None, total=18)

    assert first.job_id == second.job_id
    assert len(runner.calls) == 1

    runner.release.set()
    _settle(runner)


def test_a_different_window_starts_a_separate_job() -> None:
    runner = RecordingRunner(blocking=True)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    first = tracker.start("today", start, end, None, total=18)
    second = tracker.start("3h", start - timedelta(hours=1), end, None, total=18)
    runner.release.set()
    _settle(runner)

    assert first.job_id != second.job_id
    assert len(runner.calls) == 2


def test_a_settled_job_does_not_block_a_fresh_request() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    first = tracker.start("today", start, end, None, total=18)
    _settle(runner)
    second = tracker.start("today", start, end, None, total=18)
    _settle(runner)

    assert first.job_id != second.job_id
    assert len(runner.calls) == 2


def test_a_failing_runner_settles_the_job_with_its_error() -> None:
    runner = RecordingRunner(error="portal unreachable")
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)

    for _ in range(200):
        if not job.running:
            break
        threading.Event().wait(0.02)

    snapshot = job.snapshot()
    assert snapshot["running"] is False
    assert "portal unreachable" in (snapshot["error"] or "")


def test_get_returns_none_for_an_unknown_job() -> None:
    tracker = NetcareQueryTracker(RecordingRunner())
    assert tracker.get("never-issued") is None


def test_get_returns_a_started_job() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    _settle(runner)

    assert tracker.get(job.job_id) is job


def test_active_reports_the_running_job() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    _settle(runner)

    assert tracker.active() is None
    assert job.running is False


@pytest.mark.parametrize("total", [0, -3])
def test_start_normalises_a_nonsensical_total(total: int) -> None:
    runner = RecordingRunner(total=0)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=total)
    _settle(runner)

    assert job.total >= 1


def test_history_is_bounded_so_long_lived_processes_do_not_leak() -> None:
    """Only the most recent jobs are retained, so memory stays flat."""

    runner = RecordingRunner(total=0)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    for _ in range(40):
        job = tracker.start("today", start, end, None, total=1)
        _settle(runner)
        assert tracker.get(job.job_id) is not None

    assert len(tracker._jobs) <= 20
    assert len(tracker._order) <= 20


# --- Granular stage reporting ----------------------------------------------


def _job() -> QueryJob:
    return QueryJob(
        job_id="j1",
        preset="today",
        start=datetime(2026, 9, 20, 0, 0),
        end=datetime(2026, 9, 20, 10, 0),
        total=18,
        day=None,
    )


def test_a_fresh_job_starts_in_a_queued_stage() -> None:
    assert _job().snapshot()["stage"] == STAGE_QUEUED


def test_set_stage_publishes_the_narrative_text() -> None:
    job = _job()
    job.set_stage(STAGE_SOLVING_CAPTCHA)
    assert job.snapshot()["stage"] == STAGE_SOLVING_CAPTCHA
    job.set_stage(branch_stage("Cabang A", 3, 18))
    assert job.snapshot()["stage"] == "Mengambil link: Cabang A (3/18)..."


def test_the_ordered_stage_vocabulary_matches_the_dialog_copy() -> None:
    """The operator-facing wording is a contract, not a free-form string."""

    assert STAGE_OPENING_BROWSER == "Membuka browser & login..."
    assert STAGE_SOLVING_CAPTCHA == "Menyelesaikan CAPTCHA..."
    assert branch_stage("Cabang A", 1, 18) == "Mengambil link: Cabang A (1/18)..."


def test_a_finished_job_reports_the_done_stage() -> None:
    job = _job()
    job.finish()
    assert job.snapshot()["stage"] == STAGE_DONE


def test_the_tracker_forwards_the_stage_callback_to_the_runner() -> None:
    runner = RecordingRunner(total=2)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=2)
    _settle(runner)

    assert callable(runner.calls[0]["stage"])
    # The last branch stage is superseded once the round actually completes.
    assert job.snapshot()["stage"] == STAGE_DONE


def test_a_blocking_runner_reports_intermediate_stages_while_running() -> None:
    """The stage is visible mid-flight, not only after the job settles."""

    release = threading.Event()

    def slow_runner(**kwargs: Any) -> list[ScrapeOutcome]:
        stage = kwargs["stage"]
        stage(STAGE_OPENING_BROWSER, 0, 18)
        assert release.wait(timeout=5), "test never released the runner"
        return []

    tracker = NetcareQueryTracker(slow_runner)
    start, end = _window()
    job = tracker.start("today", start, end, None, total=18)

    snapshot = job.snapshot()
    assert snapshot["running"] is True
    assert snapshot["stage"] == STAGE_OPENING_BROWSER

    release.set()
    for _ in range(200):
        if not job.running:
            break
        threading.Event().wait(0.02)
    assert job.snapshot()["stage"] == STAGE_DONE


def test_a_failed_job_never_reports_the_done_stage() -> None:
    """A crash mid-capture must not be labelled 'Selesai'."""

    def exploding_runner(**kwargs: Any) -> list[ScrapeOutcome]:
        kwargs["stage"](branch_stage("Cabang A", 4, 18), 4, 18)
        raise RuntimeError("portal unreachable")

    tracker = NetcareQueryTracker(exploding_runner)
    start, end = _window()
    job = tracker.start("today", start, end, None, total=18)

    for _ in range(200):
        if not job.running:
            break
        threading.Event().wait(0.02)

    assert job.snapshot()["stage"] == STAGE_FAILED
    assert "portal unreachable" in (job.snapshot()["error"] or "")


# --- Cancellation ----------------------------------------------------------


def test_request_cancel_marks_the_job_and_reports_it_once() -> None:
    job = _job()
    assert job.request_cancel() is True
    assert job.request_cancel() is False
    assert job.snapshot()["cancelled"] is True
    assert job.snapshot()["stage"] == STAGE_CANCELLED


def test_a_settled_job_cannot_be_cancelled() -> None:
    job = _job()
    job.finish()
    assert job.request_cancel() is False
    assert job.snapshot()["cancelled"] is False


def test_the_tracker_cancels_a_running_job() -> None:
    runner = RecordingRunner(blocking=True)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    assert tracker.cancel(job.job_id) is job
    assert job.snapshot()["cancelled"] is True

    runner.release.set()
    _settle(runner)


def test_the_tracker_refuses_to_cancel_a_settled_job() -> None:
    runner = RecordingRunner()
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    _settle(runner)

    assert tracker.cancel(job.job_id) is None
    assert job.snapshot()["cancelled"] is False


def test_cancelling_an_unknown_job_returns_none() -> None:
    tracker = NetcareQueryTracker(RecordingRunner())
    assert tracker.cancel("never-issued") is None


def test_a_cancelled_job_keeps_the_cancelled_stage_after_settling() -> None:
    """Finishing the round must not relabel a cancelled job as 'Selesai'."""

    job = _job()
    job.request_cancel()
    job.finish()
    assert job.snapshot()["stage"] == STAGE_CANCELLED
    assert job.snapshot()["cancelled"] is True


def test_the_runner_receives_a_cancellation_probe() -> None:
    """The runner is handed a callable, not a snapshot, so it stays live."""

    runner = RecordingRunner(blocking=True)
    tracker = NetcareQueryTracker(runner)
    start, end = _window()

    job = tracker.start("today", start, end, None, total=18)
    probe = runner.calls[0]["cancelled"]
    assert probe() is False

    tracker.cancel(job.job_id)
    assert probe() is True

    runner.release.set()
    _settle(runner)
