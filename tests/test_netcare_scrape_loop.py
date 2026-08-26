"""Unit tests for the Netcare scrape round loop and daemon lifecycle."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mrtg_cmp.netcare.scraper import NetcareCache, ScrapeOutcome
from mrtg_cmp.netcare.service import (
    NetcareDaemon,
    NetcareService,
    NetcareSessionExpiredError,
    _per_target_capture,
    build_service_from_settings,
)
from mrtg_cmp.netcare.targets import DEFAULT_TARGETS, NetcareTarget


def _graph_bytes() -> bytes:
    image = Image.new("RGB", (600, 300), "white")
    pixels = image.load()
    assert pixels is not None
    for x in range(600):
        for y in range(150, 280):
            pixels[x, y] = (0, 204, 0) if (x // 17) % 2 else (0, 140, 0)
    for x in range(0, 600, 3):
        for y in range(10, 25):
            pixels[x, y] = (0, 0, 0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Scrape round ----------------------------------------------------------


class RecordingCapturer:
    """Capturer double returning canned payloads per target."""

    def __init__(self, payloads: dict[str, bytes], default: bytes | None = None) -> None:
        self.payloads = payloads
        self.default = default
        self.calls: list[NetcareTarget] = []

    def capture(self, target: NetcareTarget) -> bytes:
        self.calls.append(target)
        payload = self.payloads.get(target.target, self.default)
        if payload is None:
            raise RuntimeError("capture failed")
        return payload


def test_scrape_round_processes_targets_in_catalog_order(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    capturer = RecordingCapturer({t.target: _graph_bytes() for t in targets})

    outcomes = scrape_round(cache, targets, capturer.capture)

    assert [o.target for o in outcomes] == [t.target for t in targets]
    assert all(o.status == "ok" for o in outcomes)
    assert all(cache.has_image(t.target) for t in targets)


def test_scrape_round_writes_exactly_one_image_per_target(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    capturer = RecordingCapturer({t.target: _graph_bytes() for t in targets})

    scrape_round(cache, targets, capturer.capture)
    scrape_round(cache, targets, capturer.capture)

    pngs = sorted(p.name for p in tmp_path.glob("*.png"))
    assert len(pngs) == len(targets)
    assert not list(tmp_path.glob(".tmp-*"))


def test_scrape_round_records_blank_capture_as_no_graph(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecordingCapturer({target.target: _graph_bytes()})
    scrape_round(cache, [target], capturer.capture)

    blank = io.BytesIO()
    Image.new("RGB", (600, 300), "white").save(blank, format="PNG")

    outcomes = scrape_round(cache, [target], lambda _t: blank.getvalue())
    assert outcomes[0].status == "no_graph"
    # Previous image is retained, so the dashboard keeps a usable image.
    assert cache.has_image(target.target)
    assert cache.read_manifest()[target.target]["status"] == "stale"


def test_scrape_round_keeps_previous_image_on_capture_exception(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecordingCapturer({target.target: _graph_bytes()})
    scrape_round(cache, [target], capturer.capture)
    first_bytes = cache.image_path(target.target).read_bytes()

    def boom(_target: NetcareTarget) -> bytes:
        raise RuntimeError("portal timeout")

    outcomes = scrape_round(cache, [target], boom)
    assert outcomes[0].status == "error"
    assert outcomes[0].error is not None
    assert cache.image_path(target.target).read_bytes() == first_bytes
    assert cache.read_manifest()[target.target]["status"] == "stale"


def test_scrape_round_continues_after_a_failing_target(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    first, second = DEFAULT_TARGETS[0], DEFAULT_TARGETS[1]

    def capture(target: NetcareTarget) -> bytes:
        if target.target == first.target:
            raise RuntimeError("portal timeout")
        return _graph_bytes()

    outcomes = scrape_round(cache, [first, second], capture)
    assert [o.status for o in outcomes] == ["error", "ok"]
    assert cache.has_image(second.target)


def test_scrape_round_with_no_targets_is_a_noop(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    assert scrape_round(cache, [], lambda _t: b"") == []


def test_scrape_round_status_manifest_covers_every_target(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    capturer = RecordingCapturer({t.target: _graph_bytes() for t in targets})
    scrape_round(cache, targets, capturer.capture)

    manifest = cache.read_manifest()
    assert len(manifest) == len(targets)
    assert all(entry["status"] == "ok" for entry in manifest.values())


# --- Re-login recovery on an expired session (Task 33.2) -------------------


class RecoveringCapturer:
    """A capturer whose session expires, with an optional re-login hook."""

    def __init__(
        self,
        payload: bytes,
        *,
        expires_after: int = 0,
        expires_forever: bool = False,
        relogin_result: bool = True,
        relogin_raises: bool = False,
        with_hook: bool = True,
    ) -> None:
        self.payload = payload
        self.expires_after = expires_after
        self.expires_forever = expires_forever
        self.relogin_result = relogin_result
        self.relogin_raises = relogin_raises
        self.attempts: list[str] = []
        self.logins = 0
        if with_hook:
            self.relogin = self._relogin  # type: ignore[attr-defined]

    def _relogin(self) -> bool:
        self.logins += 1
        if self.relogin_raises:
            raise RuntimeError("captcha solver unavailable")
        return self.relogin_result

    def __call__(self, target: NetcareTarget) -> bytes:
        self.attempts.append(target.target)
        if self.expires_forever or len(self.attempts) <= self.expires_after:
            raise NetcareSessionExpiredError("TelkomCare session expired: re-login is required")
        return self.payload


def test_an_expired_session_is_retried_once_after_a_re_login(tmp_path: Path) -> None:
    """One stale cookie must cost a re-login, not an error on every branch."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecoveringCapturer(_graph_bytes(), expires_after=1)

    outcomes = scrape_round(cache, [target], capturer)

    assert [o.status for o in outcomes] == ["ok"]
    assert capturer.logins == 1
    assert capturer.attempts == [target.target, target.target]
    assert cache.has_image(target.target)
    assert cache.read_manifest()[target.target]["status"] == "ok"


def test_the_retry_happens_only_once_never_in_a_loop(tmp_path: Path) -> None:
    """A second expiry fails the target; retrying again only delays the round."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecoveringCapturer(_graph_bytes(), expires_forever=True)

    outcomes = scrape_round(cache, [target], capturer)

    assert [o.status for o in outcomes] == ["error"]
    assert capturer.logins == 1, "one re-login, then the target is given up on"
    assert len(capturer.attempts) == 2
    assert outcomes[0].error is not None
    assert "session expired" in outcomes[0].error


def test_a_failed_re_login_records_the_expiry_rather_than_a_login_error(
    tmp_path: Path,
) -> None:
    """The operator needs to know the session died, not that the retry failed."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecoveringCapturer(_graph_bytes(), expires_forever=True, relogin_result=False)

    outcomes = scrape_round(cache, [target], capturer)

    assert [o.status for o in outcomes] == ["error"]
    assert outcomes[0].error is not None
    assert "session expired" in outcomes[0].error
    assert cache.read_manifest()[target.target]["status"] == "error"


def test_a_raising_re_login_does_not_escape_the_round(tmp_path: Path) -> None:
    """A re-login that blows up is contained, exactly like any capture failure."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    capturer = RecoveringCapturer(_graph_bytes(), expires_forever=True, relogin_raises=True)

    outcomes = scrape_round(cache, [target], capturer)

    assert [o.status for o in outcomes] == ["error"]
    assert capturer.logins == 1


def test_a_capturer_without_a_re_login_hook_still_fails_cleanly(tmp_path: Path) -> None:
    """A caller-supplied lambda has no session to re-authenticate."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    attempts: list[str] = []

    def capture(branch: NetcareTarget) -> bytes:
        attempts.append(branch.target)
        raise NetcareSessionExpiredError("session expired")

    outcomes = scrape_round(cache, [target], capture)

    assert [o.status for o in outcomes] == ["error"]
    assert attempts == [target.target], "without a hook there is nothing to retry with"


def test_one_branch_expiring_does_not_stop_the_rest_of_the_round(tmp_path: Path) -> None:
    """The recovery is per target: the round still completes every branch."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = [DEFAULT_TARGETS[0], DEFAULT_TARGETS[1]]
    payload = _graph_bytes()
    doomed, healthy = targets
    state = {"doomed_attempts": 0}

    def capture(target: NetcareTarget) -> bytes:
        if target.target == doomed.target:
            state["doomed_attempts"] += 1
            if state["doomed_attempts"] == 1:
                raise NetcareSessionExpiredError("session expired")
        return payload

    def relogin() -> bool:
        return True

    capture.relogin = relogin  # type: ignore[attr-defined]

    outcomes = scrape_round(cache, targets, capture, 1)

    assert [o.status for o in outcomes] == ["ok", "ok"]
    assert state["doomed_attempts"] == 2


def test_each_worker_recovers_through_its_own_session(tmp_path: Path) -> None:
    """A worker re-logs its own session, so the hooks must be per index."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payload = _graph_bytes()
    logins: dict[int, int] = {}

    def factory(worker: int) -> Any:
        capturer = RecoveringCapturer(payload, expires_after=1)

        def login() -> bool:
            logins[worker] = logins.get(worker, 0) + 1
            return True

        capturer.relogin = login  # type: ignore[attr-defined]
        return capturer

    outcomes = scrape_round(cache, targets, None, 3, capturer_factory=factory)

    assert all(o.status == "ok" for o in outcomes)
    assert sum(logins.values()) == 3, "one re-login per worker that hit an expiry"


def test_per_target_capture_publishes_a_relogin_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook is how the round reaches the session behind a capturer."""

    from mrtg_cmp.netcare import service as service_module

    logins: list[int] = []

    class StubSession:
        page_timeout_seconds = 25
        driver = object()

        def login(self) -> bool:
            logins.append(1)
            return True

    monkeypatch.setattr(
        service_module,
        "_capture_via_driver",
        lambda *_a, **_k: _graph_bytes(),
    )

    capture = _per_target_capture(StubSession(), "https://portal.example.test")  # type: ignore[arg-type]

    assert capture(DEFAULT_TARGETS[0])  # type: ignore[call-arg]
    hook = capture.relogin  # type: ignore[attr-defined]
    assert hook() is True
    assert logins == [1]


# --- Worker pool (FR-15.1) -------------------------------------------------


def test_partition_targets_balances_18_targets_over_3_workers() -> None:
    from mrtg_cmp.netcare.service import partition_targets

    buckets = partition_targets(list(DEFAULT_TARGETS), 3)
    assert [len(b) for b in buckets] == [6, 6, 6]
    assert [t.target for b in buckets for t in b] == [t.target for t in DEFAULT_TARGETS]


def test_partition_targets_remainder_spreads_to_the_first_buckets() -> None:
    from mrtg_cmp.netcare.service import partition_targets

    buckets = partition_targets(list(DEFAULT_TARGETS[:5]), 3)
    assert [len(b) for b in buckets] == [2, 2, 1]


def test_partition_targets_never_exceeds_available_targets() -> None:
    from mrtg_cmp.netcare.service import partition_targets

    assert len(partition_targets(list(DEFAULT_TARGETS[:2]), 3)) == 2
    assert partition_targets([], 3) == []


@pytest.mark.parametrize("workers", [0, -4])
def test_partition_targets_degrades_a_bad_worker_count_to_one_bucket(workers: int) -> None:
    from mrtg_cmp.netcare.service import partition_targets

    buckets = partition_targets(list(DEFAULT_TARGETS), workers)
    assert [len(b) for b in buckets] == [len(DEFAULT_TARGETS)]


def test_scrape_round_uses_a_capturer_per_worker_index(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    built: list[int] = []
    payload = _graph_bytes()

    def factory(worker: int) -> Any:
        built.append(worker)
        return lambda _target: payload

    outcomes = scrape_round(
        cache,
        list(DEFAULT_TARGETS),
        lambda _target: payload,
        3,
        capturer_factory=factory,
    )

    assert built == [0, 1, 2]
    assert len(outcomes) == len(DEFAULT_TARGETS)
    assert all(o.status == "ok" for o in outcomes)


def test_scrape_round_runs_targets_concurrently(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payload = _graph_bytes()
    barrier = threading.Barrier(3, timeout=5)
    inflight = {"peak": 0, "current": 0}
    guard = threading.Lock()

    def capture(_target: NetcareTarget) -> bytes:
        with guard:
            inflight["current"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["current"])
        try:
            # Only passes when three captures are genuinely in flight at once.
            barrier.wait()
            return payload
        finally:
            with guard:
                inflight["current"] -= 1

    outcomes = scrape_round(cache, targets, capture, 3)

    assert inflight["peak"] == 3
    assert sum(1 for o in outcomes if o.status == "ok") == len(targets)


def test_scrape_round_preserves_catalog_order_under_the_pool(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    capturer = RecordingCapturer({t.target: _graph_bytes() for t in targets})

    outcomes = scrape_round(cache, targets, capturer.capture, 3)

    assert [o.target for o in outcomes] == [t.target for t in targets]
    assert len(capturer.calls) == len(targets)


def test_scrape_round_manifest_survives_concurrent_writes(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payload = _graph_bytes()

    outcomes = scrape_round(cache, targets, lambda _t: payload, 3)

    manifest = cache.read_manifest()
    assert [o.target for o in outcomes] == [t.target for t in targets]
    assert all(manifest[t.target]["status"] == "ok" for t in targets)
    assert not list(tmp_path.glob(".tmp-*"))


def test_scrape_round_records_failures_per_worker_without_aborting_the_pool(
    tmp_path: Path,
) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payload = _graph_bytes()
    doomed = {targets[0].target, targets[7].target, targets[13].target}

    def capture(target: NetcareTarget) -> bytes:
        if target.target in doomed:
            raise RuntimeError("portal timeout")
        return payload

    outcomes = scrape_round(cache, targets, capture, 3)
    by_target = {o.target: o for o in outcomes}

    assert set(by_target) == {t.target for t in targets}
    assert all(by_target[t].status == "error" for t in doomed)
    assert all(by_target[t.target].status == "ok" for t in targets if t.target not in doomed)


def test_scrape_round_reports_progress_monotonically(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    payload = _graph_bytes()
    seen: list[tuple[str, int, int]] = []
    guard = threading.Lock()

    def progress(outcome: ScrapeOutcome, completed: int, total: int) -> None:
        with guard:
            seen.append((outcome.target, completed, total))

    scrape_round(cache, targets, lambda _t: payload, 3, progress)

    assert sorted(completed for _, completed, _ in seen) == list(range(1, len(targets) + 1))
    assert {total for _, _, total in seen} == {len(targets)}


def test_scrape_round_writes_to_a_date_partition_when_asked(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    target = DEFAULT_TARGETS[0]
    day = "2026-09-20"

    outcomes = scrape_round(cache, [target], lambda _t: _graph_bytes(), 1, None, day)

    assert outcomes[0].status == "ok"
    assert cache.image_path(target.target, day) == tmp_path / day / f"{target.target}.png"
    assert cache.image_path(target.target, day).is_file()
    assert not cache.image_path(target.target).is_file()
    assert cache.read_manifest(day)[target.target]["status"] == "ok"


# --- Stage narration and cancellation inside a round -----------------------


def test_scrape_round_names_the_branch_it_is_capturing(tmp_path: Path) -> None:
    """The operator is told which link is in flight, not just how many are left."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    seen: list[tuple[str, int, int]] = []
    guard = threading.Lock()

    def stage(text: str, done: int, total: int) -> None:
        with guard:
            seen.append((text, done, total))

    scrape_round(cache, targets, lambda _t: _graph_bytes(), 1, None, None, None, stage)

    assert len(seen) == len(targets)
    assert [text for text, _, _ in seen] == [
        f"Mengambil link: {t.name} ({index}/{len(targets)})..."
        for index, t in enumerate(targets, start=1)
    ]
    assert {total for _, _, total in seen} == {len(targets)}


def test_scrape_round_keeps_narrating_when_no_stage_callback_is_given(
    tmp_path: Path,
) -> None:
    """Stage reporting is optional plumbing, never a precondition for capture."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)

    outcomes = scrape_round(cache, targets, lambda _t: _graph_bytes(), 3)

    assert all(o.status == "ok" for o in outcomes)


def test_scrape_round_stops_between_targets_when_cancelled(tmp_path: Path) -> None:
    """An operator who cancels must not wait out all 18 captures."""

    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    captured: list[str] = []

    def capture(target: NetcareTarget) -> bytes:
        captured.append(target.target)
        return _graph_bytes()

    outcomes = scrape_round(
        cache, targets, capture, 1, None, None, None, None, lambda: True
    )

    assert captured == []
    assert outcomes == []


def test_scrape_round_cancellation_stops_after_the_captures_already_started(
    tmp_path: Path,
) -> None:
    from mrtg_cmp.netcare.service import scrape_round

    cache = NetcareCache(tmp_path)
    targets = list(DEFAULT_TARGETS)
    captured: list[str] = []

    def capture(target: NetcareTarget) -> bytes:
        captured.append(target.target)
        return _graph_bytes()

    outcomes = scrape_round(
        cache,
        targets,
        capture,
        1,
        None,
        None,
        None,
        None,
        lambda: len(captured) >= 3,
    )

    assert len(captured) == 3
    assert len(outcomes) == 3


def test_query_announces_the_browser_stage_before_capturing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The longest silent phase — launching Chrome — is narrated first."""

    from mrtg_cmp.netcare import service as service_module
    from mrtg_cmp.netcare.query import STAGE_OPENING_BROWSER, STAGE_SOLVING_CAPTCHA

    targets = list(DEFAULT_TARGETS)
    service = NetcareService(cache=NetcareCache(tmp_path), targets=targets)
    stages: list[str] = []
    closed = threading.Event()

    class FakeSession:
        def login(self, on_stage: Any = None) -> bool:
            if on_stage is not None:
                on_stage("Menyelesaikan CAPTCHA...")
            return True

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(
        service_module, "build_session_from_settings", lambda worker=0: FakeSession()
    )

    def fake_scrape_round(*args: Any, **kwargs: Any) -> list[Any]:
        """Resolve one capturer so the session lifecycle is actually exercised."""

        kwargs["capturer_factory"](0)
        return []

    monkeypatch.setattr(service_module, "scrape_round", fake_scrape_round)

    def stage(text: str, done: int, total: int) -> None:
        stages.append(text)

    service.query(workers=1, stage=stage)

    assert stages[0] == STAGE_OPENING_BROWSER
    assert STAGE_SOLVING_CAPTCHA in stages
    assert closed.is_set()


def test_settings_default_worker_count_is_three() -> None:
    from mrtg_cmp.config import Settings

    assert Settings(_env_file=None).netcare_workers == 3


def test_settings_worker_count_rejects_a_nonsensical_value() -> None:
    from pydantic import ValidationError

    from mrtg_cmp.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, netcare_workers=0)


def test_session_pool_gives_each_worker_its_own_session_under_concurrency() -> None:
    """FR-16.2: workers racing for sessions must each end up with a private one."""

    from mrtg_cmp.netcare.service import SessionPool

    created: list[object] = []
    guard = threading.Lock()

    class FakeSession:
        def __init__(self, worker: int) -> None:
            self.worker = worker
            self.closed = False
            self.logins = 0

        def login(self) -> bool:
            self.logins += 1
            return True

        def close(self) -> None:
            self.closed = True

    def factory(worker: int) -> Any:
        session = FakeSession(worker)
        with guard:
            created.append(session)
        return session

    pool = SessionPool(factory, 3)
    seen: list[int] = []
    start = threading.Barrier(3, timeout=5)

    def worker(index: int) -> None:
        start.wait()
        seen.append(id(pool.get(index)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 3
    assert len(set(seen)) == 3
    assert len(created) == 3
    assert all(session.logins == 1 for session in created)  # type: ignore[attr-defined]

    pool.close()
    assert all(session.closed for session in created)  # type: ignore[attr-defined]
    pool.close()


def test_session_pool_reuses_one_session_per_worker_index() -> None:
    from mrtg_cmp.netcare.service import SessionPool

    created: list[Any] = []

    def factory(worker: int) -> Any:
        session = type("S", (), {"login": lambda _s: True, "close": lambda _s: None})()
        created.append(session)
        return session

    pool = SessionPool(factory, 2)
    first = pool.get(1)
    assert pool.get(1) is first
    assert len(created) == 1
    pool.close()


def test_session_pool_propagates_a_failed_login(tmp_path: Path) -> None:
    from mrtg_cmp.netcare.service import SessionPool

    def dead_factory(worker: int) -> Any:
        return DeadSession()

    class DeadSession:
        def login(self) -> bool:
            return False

        def close(self) -> None:
            return None

    pool = SessionPool(dead_factory, 1)
    with pytest.raises(RuntimeError, match="TelkomCare session"):
        pool.get(0)


# --- Daemon lifecycle ------------------------------------------------------


def test_daemon_runs_one_round_then_sleeps(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    rounds: list[int] = []
    stop = threading.Event()

    def run_round() -> list[ScrapeOutcome]:
        rounds.append(1)
        stop.set()
        return []

    daemon = NetcareDaemon(
        service=NetcareService(cache=cache, targets=[]),
        run_round=run_round,
        interval_seconds=300,
        sleeper=lambda _seconds: None,
    )
    daemon.run(stop_event=stop, max_rounds=1)

    assert len(rounds) == 1


def test_daemon_respects_max_rounds(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    stop = threading.Event()
    counter = {"n": 0}

    def run_round() -> list[ScrapeOutcome]:
        counter["n"] += 1
        return []

    daemon = NetcareDaemon(
        service=NetcareService(cache=cache, targets=[]),
        run_round=run_round,
        interval_seconds=300,
        sleeper=lambda _seconds: None,
    )
    daemon.run(stop_event=stop, max_rounds=3)

    assert counter["n"] == 3


def test_daemon_stops_when_event_is_set_before_first_round(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    stop = threading.Event()
    stop.set()
    counter = {"n": 0}

    def run_round() -> list[ScrapeOutcome]:
        counter["n"] += 1
        return []

    daemon = NetcareDaemon(
        service=NetcareService(cache=cache, targets=[]),
        run_round=run_round,
        interval_seconds=300,
        sleeper=lambda _seconds: None,
    )
    daemon.run(stop_event=stop)

    assert counter["n"] == 0


def test_daemon_survives_a_failing_round(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    stop = threading.Event()
    calls = {"n": 0}

    def run_round() -> list[ScrapeOutcome]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("browser crashed")
        stop.set()
        return []

    daemon = NetcareDaemon(
        service=NetcareService(cache=cache, targets=[]),
        run_round=run_round,
        interval_seconds=300,
        sleeper=lambda _seconds: None,
    )
    daemon.run(stop_event=stop, max_rounds=5)

    assert calls["n"] == 2


def test_daemon_sleep_is_interrupted_by_stop_event(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    stop = threading.Event()
    slept: list[int] = []

    def sleeper(seconds: int) -> None:
        slept.append(seconds)
        stop.set()

    def run_round() -> list[ScrapeOutcome]:
        return []

    daemon = NetcareDaemon(
        service=NetcareService(cache=cache, targets=[]),
        run_round=run_round,
        interval_seconds=300,
        sleeper=sleeper,
    )
    daemon.run(stop_event=stop, max_rounds=2)

    assert slept == [300]


# --- Service wiring --------------------------------------------------------


def test_service_reports_cache_stats(tmp_path: Path) -> None:
    cache = NetcareCache(tmp_path)
    cache.store(DEFAULT_TARGETS[0].target, _graph_bytes())
    cache.mark_ok(DEFAULT_TARGETS[0].target)

    service = NetcareService(cache=cache, targets=list(DEFAULT_TARGETS))
    stats = service.stats()
    assert stats["total"] == len(DEFAULT_TARGETS)
    assert stats["ok"] == 1
    assert stats["cached_images"] == 1


def test_service_refresh_targets_queue_primes_priority_scrape(tmp_path: Path) -> None:
    service = NetcareService(cache=NetcareCache(tmp_path), targets=list(DEFAULT_TARGETS))
    known = DEFAULT_TARGETS[0].target
    assert service.refresh_targets([known]) is True
    assert service.pending_priority() == {known}


def test_service_refresh_rejects_unknown_target(tmp_path: Path) -> None:
    service = NetcareService(cache=NetcareCache(tmp_path), targets=list(DEFAULT_TARGETS))
    assert service.refresh_targets(["not-a-target"]) is False


def test_service_priority_drain_returns_queued_targets(tmp_path: Path) -> None:
    service = NetcareService(cache=NetcareCache(tmp_path), targets=list(DEFAULT_TARGETS))
    known = DEFAULT_TARGETS[0].target
    service.refresh_targets([known])
    assert service.drain_priority() == [known]
    assert service.drain_priority() == []


def test_build_service_from_settings_wires_cache_and_targets() -> None:
    from mrtg_cmp.config import Settings

    settings = Settings(_env_file=None)
    service = build_service_from_settings(settings, cache_dir=Path("data/netcare_cache"))
    assert service.cache.cache_dir == Path("data/netcare_cache")
    assert len(service.targets) == len(DEFAULT_TARGETS)


def test_build_service_uses_catalog_override(tmp_path: Path) -> None:
    from mrtg_cmp.config import Settings

    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "type,target,name,address,region,ocr_enabled\nSID,111-1,One,Addr,CGK,true\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, netcare_catalog_file=catalog)
    service = build_service_from_settings(settings, cache_dir=tmp_path)
    assert [t.target for t in service.targets] == ["111-1"]


def test_service_stats_of_empty_cache(tmp_path: Path) -> None:
    service = NetcareService(cache=NetcareCache(tmp_path), targets=[])
    stats: dict[str, Any] = service.stats()
    assert stats["total"] == 0
    assert stats["ok"] == 0
    assert stats["cached_images"] == 0


def test_daemon_default_interval_is_five_minutes(tmp_path: Path) -> None:
    daemon = NetcareDaemon(
        service=NetcareService(cache=NetcareCache(tmp_path), targets=[]),
        run_round=lambda: [],
    )
    assert daemon.interval_seconds == 300


@pytest.mark.parametrize("targets", [[], list(DEFAULT_TARGETS[:1])])
def test_service_accepts_any_target_selection(tmp_path: Path, targets: list[NetcareTarget]) -> None:
    service = NetcareService(cache=NetcareCache(tmp_path), targets=targets)
    assert service.targets == targets
