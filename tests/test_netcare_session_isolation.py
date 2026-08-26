"""Regression tests for per-worker session isolation in on-demand queries.

``NetcareService.query()`` used to build a single ``NetcareSession`` and hand it
to a 3-thread fan-out. Selenium's WebDriver is not thread-safe, so three threads
navigated the same browser and every capture failed with
``RuntimeError("Graph did not render")`` while the dashboard reported success
(FR-16.2).
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mrtg_cmp.netcare.scraper import NetcareCache
from mrtg_cmp.netcare.service import NetcareService
from mrtg_cmp.netcare.targets import DEFAULT_TARGETS, NetcareTarget


def _graph_bytes() -> bytes:
    image = Image.new("RGB", (600, 300), "white")
    pixels = image.load()
    assert pixels is not None
    for x in range(600):
        for y in range(150, 280):
            pixels[x, y] = (0, 204, 0) if (x // 17) % 2 else (0, 140, 0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ConcurrencyProbe:
    """Records how many threads are inside a driver's ``get()`` at once.

    A correct implementation never drives one driver from two threads at the
    same moment, which is precisely the condition that corrupted the DOM.
    """

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self.used_drivers: set[int] = set()
        self._lock = threading.Lock()

    def enter(self, driver_id: int) -> None:
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.used_drivers.add(driver_id)

    def leave(self) -> None:
        with self._lock:
            self.inflight -= 1


class FakeDriver:
    """Minimal driver stand-in that detects concurrent use of one instance."""

    def __init__(self, probe: ConcurrencyProbe, driver_id: int) -> None:
        self._probe = probe
        self._id = driver_id
        self.page_timeout_seconds = 25

    def get(self, url: str) -> None:
        self._probe.enter(self._id)
        try:
            # Overlap the three workers so a shared driver is guaranteed to
            # be inside get() at the same time as its siblings.
            threading.Event().wait(0.05)
        finally:
            self._probe.leave()


class FakeSession:
    """Session double recording the profile directory it was built with."""

    created: list[FakeSession] = []
    _lock = threading.Lock()

    def __init__(self, worker: int, probe: ConcurrencyProbe, base: Path) -> None:
        self.worker = worker
        self.profile_dir = base / f"worker_{worker}"
        self.page_timeout_seconds = 25
        self.driver = FakeDriver(probe, id(self))
        self.closed = False
        self.logins = 0
        #: Set to simulate a worker whose captures all fail.
        self.fail = False
        with FakeSession._lock:
            FakeSession.created.append(self)

    def login(self, on_stage: Any = None) -> bool:
        self.logins += 1
        return True

    def close(self) -> None:
        self.closed = True


def _install_capture_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Selenium capture with a driver-only stub.

    The DOM walk needs a real browser, but the property under test is which
    driver object each thread drives, so driving ``driver.get()`` and returning
    a canned graph is enough.
    """

    from mrtg_cmp.netcare import service as service_module

    def fake_per_target_capture(
        session: Any,
        base_url: str,
        day: Any = None,
        window: Any = None,
    ) -> Any:
        def capture(_target: NetcareTarget) -> bytes:
            driver = session.driver
            if driver is None:
                raise RuntimeError("Netcare browser session is not running")
            driver.get("https://example.invalid/graph")
            if getattr(session, "fail", False):
                raise RuntimeError("Graph did not render")
            return _graph_bytes()

        return capture

    monkeypatch.setattr(service_module, "_per_target_capture", fake_per_target_capture)


@pytest.fixture
def probe() -> ConcurrencyProbe:
    return ConcurrencyProbe()


@pytest.fixture
def sessions() -> list[FakeSession]:
    FakeSession.created = []
    return FakeSession.created


@pytest.fixture
def service(tmp_path: Path) -> NetcareService:
    return NetcareService(cache=NetcareCache(tmp_path), targets=list(DEFAULT_TARGETS))


def _install_sessions(
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    base: Path,
) -> None:
    """Point service.query() at a fake session factory that tracks worker index."""

    from mrtg_cmp.netcare import service as service_module

    # The default keeps this callable against the current no-argument call, so
    # the suite fails on the shared driver rather than on a signature mismatch.
    def factory(worker: int = 0) -> Any:
        return FakeSession(worker, probe, base)

    monkeypatch.setattr(service_module, "build_session_from_settings", factory)
    _install_capture_probe(monkeypatch)


# --- The regression itself -------------------------------------------------


def test_query_never_shares_one_driver_between_worker_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """FR-16.2: three workers must mean three drivers, never one shared driver.

    Before the fix this peaked at 3 threads inside a single driver, which is
    what made every branch graph fail to render.
    """

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    outcomes = service.query(workers=3)

    assert all(o.status == "ok" for o in outcomes)
    assert len(outcomes) == len(DEFAULT_TARGETS)
    assert probe.peak == 3, "the three workers did not run concurrently at all"
    assert len(sessions) == 3, "each worker must build its own session"
    assert len(probe.used_drivers) == 3, "the workers shared a driver instance"


def test_query_gives_every_worker_an_isolated_profile_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """FR-16.2: concurrent Chrome instances must not fight over one lockfile."""

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    service.query(workers=3)

    profiles = {session.profile_dir for session in sessions}
    assert len(profiles) == 3
    assert all(path.name.startswith("worker_") for path in profiles)
    assert {path.name for path in profiles} == {"worker_0", "worker_1", "worker_2"}
    assert all(path.parent == tmp_path / "profiles" for path in profiles)


def test_query_closes_every_session_it_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """A query owns its browsers, so it must not leak three Chrome processes."""

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    service.query(workers=3)

    assert sessions, "no sessions were created"
    assert all(session.closed for session in sessions)


def test_query_logs_in_each_worker_session_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """Re-login per target would triple the portal load for no reason."""

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    service.query(workers=3)

    assert all(session.logins == 1 for session in sessions)


def test_single_worker_query_still_captures_every_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """The serial path must keep working, with exactly one session."""

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    outcomes = service.query(workers=1)

    assert all(o.status == "ok" for o in outcomes)
    assert len(sessions) == 1
    assert len(probe.used_drivers) == 1
    assert probe.peak == 1


def test_query_with_no_targets_creates_no_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
    service: NetcareService,
) -> None:
    """An empty selection must not launch Chrome at all."""

    _install_sessions(monkeypatch, probe, sessions, tmp_path / "profiles")

    assert service.query(target_ids=["not-a-target"], workers=3) == []
    assert sessions == []


def test_query_still_reports_failure_per_target_without_aborting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    sessions: list[FakeSession],
) -> None:
    """One dead worker must not take the other 12 branches down with it."""

    from mrtg_cmp.netcare import service as service_module

    doomed_worker = 1

    class ExplodingSession(FakeSession):
        def __init__(self, worker: int, probe: ConcurrencyProbe, base: Path) -> None:
            super().__init__(worker, probe, base)
            self.fail = worker == doomed_worker

    def factory(worker: int = 0) -> Any:
        return ExplodingSession(worker, probe, tmp_path / "profiles")

    monkeypatch.setattr(service_module, "build_session_from_settings", factory)
    _install_capture_probe(monkeypatch)
    service = NetcareService(cache=NetcareCache(tmp_path), targets=list(DEFAULT_TARGETS))

    outcomes = service.query(workers=3)

    statuses = [o.status for o in outcomes]
    assert len(outcomes) == len(DEFAULT_TARGETS)
    assert "error" in statuses
    assert "ok" in statuses
    # Worker 1 owns exactly the middle third of the catalog.
    assert [o.status for o in outcomes[6:12]] == ["error"] * 6


# --- SessionPool isolation -------------------------------------------------


def test_session_pool_gives_each_worker_index_its_own_session() -> None:
    """The pool must key on the worker index, not on whichever thread arrived."""

    from mrtg_cmp.netcare.service import SessionPool

    created: list[int] = []

    def factory(worker: int) -> Any:
        created.append(worker)
        session = type(
            "S",
            (),
            {"worker": worker, "login": lambda _s: True, "close": lambda _s: None},
        )()
        return session

    pool = SessionPool(factory, 3)
    first, second, third = pool.get(0), pool.get(1), pool.get(2)

    assert created == [0, 1, 2]
    assert (first.worker, second.worker, third.worker) == (0, 1, 2)
    assert len({id(first), id(second), id(third)}) == 3
    pool.close()


def test_session_pool_reuses_the_session_for_a_worker_index() -> None:
    from mrtg_cmp.netcare.service import SessionPool

    created: list[int] = []

    def factory(worker: int) -> Any:
        created.append(worker)
        return type("S", (), {"login": lambda _s: True, "close": lambda _s: None})()

    pool = SessionPool(factory, 2)

    assert pool.get(1) is pool.get(1)
    assert created == [1]
    pool.close()


def test_session_pool_closes_each_session_once() -> None:
    from mrtg_cmp.netcare.service import SessionPool

    closed: list[int] = []

    def factory(worker: int) -> Any:
        return type(
            "S",
            (),
            {
                "worker": worker,
                "login": lambda _s: True,
                "close": lambda s: closed.append(s.worker),
            },
        )()

    pool = SessionPool(factory, 2)
    pool.get(0)
    pool.get(1)
    pool.close()
    pool.close()

    assert sorted(closed) == [0, 1]


def test_session_pool_refuses_a_worker_index_it_cannot_serve() -> None:
    """A miscounted worker must not be handed another worker's session.

    Silently sharing a driver is the exact failure this pool prevents, so an
    out-of-range index raises instead of quietly reusing a browser.
    """

    from mrtg_cmp.netcare.service import SessionPool

    created: list[int] = []

    def factory(worker: int) -> Any:
        created.append(worker)
        return type("S", (), {"login": lambda _s: True, "close": lambda _s: None})()

    pool = SessionPool(factory, 2)
    pool.get(0)
    with pytest.raises(ValueError, match="outside a pool of 2"):
        pool.get(2)

    assert created == [0]
    pool.close()


def test_session_pool_rejects_a_failed_login() -> None:
    from mrtg_cmp.netcare.service import SessionPool

    def factory(worker: int) -> Any:
        return type("S", (), {"login": lambda _s: False, "close": lambda _s: None})()

    pool = SessionPool(factory, 1)
    with pytest.raises(RuntimeError, match="TelkomCare session"):
        pool.get(0)
    pool.close()


# --- Session construction --------------------------------------------------


def test_build_session_from_settings_isolates_the_profile_per_worker() -> None:
    """FR-16.2: worker profiles live under the configured profile directory."""

    from mrtg_cmp.config import Settings
    from mrtg_cmp.netcare.service import build_session_from_settings

    settings = Settings(_env_file=None, netcare_profile_dir=Path("~/profiles"))
    base = Path("~/profiles").expanduser()

    assert build_session_from_settings(settings, worker=0).profile_dir == base / "worker_0"
    assert build_session_from_settings(settings, worker=2).profile_dir == base / "worker_2"


def test_build_session_from_settings_defaults_to_the_base_profile() -> None:
    """A caller that passes no worker index keeps the single shared profile."""

    from mrtg_cmp.config import Settings
    from mrtg_cmp.netcare.service import build_session_from_settings

    settings = Settings(_env_file=None, netcare_profile_dir=Path("~/profiles"))
    assert build_session_from_settings(settings).profile_dir == Path("~/profiles").expanduser()


def test_daemon_wires_one_isolated_session_per_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 24/7 daemon must not regress into sharing one browser profile.

    The pool is captured rather than driven, because materializing a real
    session would launch Chrome; what matters here is that the daemon asks for
    a distinct worker index per session.
    """

    from mrtg_cmp.config import Settings
    from mrtg_cmp.netcare import service as service_module

    settings = Settings(_env_file=None, netcare_cache_dir=tmp_path)
    monkeypatch.setattr(settings, "netcare_profile_dir", tmp_path / "profiles")
    requested: list[int | None] = []

    def fake_build(config: Any = None, worker: int | None = None) -> Any:
        requested.append(worker)
        return type("S", (), {"login": lambda _s: True, "close": lambda _s: None})()

    pools: list[Any] = []

    class RecordingPool:
        def __init__(self, factory: Any, size: int, on_login_stage: Any = None) -> None:
            self.factory = factory
            self.size = size
            pools.append(self)

        def close(self) -> None:
            return None

    monkeypatch.setattr(service_module, "build_session_from_settings", fake_build)
    monkeypatch.setattr(service_module, "SessionPool", RecordingPool)

    service_module.build_daemon_from_settings(settings)

    assert len(pools) == 1
    assert pools[0].size == settings.netcare_workers
    for worker in range(settings.netcare_workers):
        pools[0].factory(worker)
    assert requested == list(range(settings.netcare_workers))


def test_daemon_round_captures_with_one_capturer_per_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's round must resolve a capturer per worker index."""

    from mrtg_cmp.config import Settings
    from mrtg_cmp.netcare import service as service_module

    settings = Settings(_env_file=None, netcare_cache_dir=tmp_path)
    requested: list[int] = []
    payload = _graph_bytes()

    def fake_build(config: Any = None, worker: int | None = None) -> Any:
        requested.append(worker if worker is not None else -1)
        session = type("S", (), {"login": lambda _s: True, "close": lambda _s: None})()
        return session

    monkeypatch.setattr(service_module, "build_session_from_settings", fake_build)
    monkeypatch.setattr(
        service_module,
        "_per_target_capture",
        lambda session, *_args, **_kwargs: (lambda _target: payload),
    )

    daemon = service_module.build_daemon_from_settings(settings)
    outcomes = daemon.run_round()

    assert len(outcomes) == len(DEFAULT_TARGETS)
    assert all(o.status == "ok" for o in outcomes)
    assert requested == sorted(set(requested)), "a worker index was requested twice"
    assert set(requested) == set(range(settings.netcare_workers))
