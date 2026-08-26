"""Tests for the real TelkomCare target form interaction used during capture.

The portal does not take a target in the URL: the browser has to type the target
ID into the search form, press ENTER, click "Show Graph", apply the date range,
and only then does a ``graph.php`` image exist. These tests drive that sequence
against a fake portal so a regression in the form interaction fails here rather
than as an unexplained ``Graph did not render`` after 25 seconds per target.

Ported from the proven ``MRTG-TelkomCare-Report-Automation`` extractor.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from mrtg_cmp.netcare import service as service_module
from mrtg_cmp.netcare.service import (
    NetcareSessionExpiredError,
    _capture_via_driver,
    _per_target_capture,
)
from mrtg_cmp.netcare.targets import NetcareTarget, NetcareTargetType

BASE_URL = "https://portal.example.test"
SID_URL = f"{BASE_URL}/mrtgnetcare2/graph/monitoring"
GRAPH_TITLE_URL = f"{BASE_URL}/mrtgnetcare2/graph"
LOGIN_URL = f"{BASE_URL}/public/login"
START = "18/09/2026 00:00"
END = "18/09/2026 23:55"
PNG = b"\x89PNG\r\n\x1a\n rendered graph"

Locator = tuple[str, str]
SID_INPUT: Locator = (By.NAME, "sid")
GRAPH_TITLE_INPUT: Locator = (By.NAME, "graphtitle")
SHOW_GRAPH: Locator = (By.CSS_SELECTOR, service_module.SHOW_GRAPH_SELECTOR)
SID_FILTER: Locator = (By.XPATH, service_module.SID_FILTER_BUTTON_XPATH)
SID_DATES: Locator = (By.XPATH, service_module.SID_DATE_INPUT_XPATH)
STARTDATE: Locator = (By.ID, "startdate")
ENDDATE: Locator = (By.ID, "enddate")
GRAPHFILTER: Locator = (By.ID, "graphfilter")


def _target(target_type: NetcareTargetType, target_id: str = "target-001") -> NetcareTarget:
    return NetcareTarget(
        target=target_id,
        type=target_type,
        name="CGK Area Link 01",
        address="Jakarta - CGK",
        region="CGK",
    )


class FakeElement:
    """One portal control, recording everything the capture path does to it."""

    def __init__(self, kind: str, portal: FakePortal, natural_width: int = 0) -> None:
        self.kind = kind
        self.portal = portal
        self.natural_width = natural_width
        self.value = ""
        self.keys_sent: list[Any] = []
        self.displayed = True
        self.enabled = True

    def clear(self) -> None:
        self.value = ""

    def send_keys(self, text: Any) -> None:
        self.keys_sent.append(text)
        self.value += str(text)
        self.portal.key_presses.append((self.kind, text))

    def click(self) -> None:
        self.portal.native_clicks.append(self)

    def is_displayed(self) -> bool:
        return self.displayed

    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def screenshot_as_png(self) -> bytes:
        self.portal.captures.append(self)
        return PNG


class FakePortal:
    """A TelkomCare graph page pair: a search form and a target detail view.

    Pressing "Show Graph" swaps the element map to the detail view, and pressing
    the date filter publishes the ``graph.php`` image. That ordering is the whole
    point of these tests: a capture that skips the form must never see a graph.
    """

    def __init__(
        self,
        target_type: NetcareTargetType = NetcareTargetType.SID,
        login_on_load: bool = False,
        login_after_submit: bool = False,
        detail_ready: bool = True,
        graph_renders: bool = True,
        graph_width: int = 600,
    ) -> None:
        self.target_type = target_type
        self.login_on_load = login_on_load
        self.login_after_submit = login_after_submit
        self.detail_ready = detail_ready
        self.graph_renders = graph_renders
        self.graph_width = graph_width

        self.current_url = "about:blank"
        self.visited: list[str] = []
        self.on_detail = False
        self.filter_applied = False
        self.key_presses: list[tuple[str, Any]] = []
        self.native_clicks: list[FakeElement] = []
        self.js_clicks: list[FakeElement] = []
        self.field_sets: list[tuple[str, str]] = []
        self.scrolled: list[FakeElement] = []
        self.captures: list[FakeElement] = []
        self.scripts: list[str] = []

        input_locator = SID_INPUT if target_type is NetcareTargetType.SID else GRAPH_TITLE_INPUT
        self.target_input = FakeElement(input_locator[1], self)
        self.show_graph = FakeElement("show-graph", self)
        self.search: dict[Locator, list[FakeElement]] = {
            input_locator: [self.target_input],
            SHOW_GRAPH: [self.show_graph],
        }
        self.detail: dict[Locator, list[FakeElement]] = self._build_detail()
        self.graph = FakeElement("graph.php", self, natural_width=graph_width)

    def _build_detail(self) -> dict[Locator, list[FakeElement]]:
        """Register the filter controls this mode's detail view exposes."""

        if not self.detail_ready:
            return {}
        if self.target_type is NetcareTargetType.SID:
            # No ids: only the unlabelled Filter button can be located by name.
            return {
                SID_FILTER: [FakeElement("sid-filter", self)],
                SID_DATES: [FakeElement("sid-start", self), FakeElement("sid-end", self)],
            }
        return {
            STARTDATE: [FakeElement("startdate", self)],
            ENDDATE: [FakeElement("enddate", self)],
            GRAPHFILTER: [FakeElement("graphfilter", self)],
        }

    # -- WebDriver surface -------------------------------------------------

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = LOGIN_URL if self.login_on_load else url
        self.on_detail = False
        self.filter_applied = False

    def find_element(self, by: str, value: str) -> Any:
        matches = self._visible().get((by, value))
        if not matches:
            raise NoSuchElementException(f"no element for {by}={value!r}")
        return matches[0]

    def find_elements(self, by: str, value: str) -> list[Any]:
        if value == service_module.GRAPH_IMAGE_XPATH:
            rendered = self.filter_applied and self.graph_renders
            return [self.graph] if rendered else []
        return list(self._visible().get((by, value)) or [])

    def execute_script(self, script: str, *args: Any) -> Any:
        if "scrollIntoView" in script:
            self.scrolled.append(args[0])
            return None
        if "arguments[0].click" in script:
            self._press(args[0])
            return None
        if "dispatchEvent(new Event('change'))" in script:
            args[0].value = args[1]
            self.field_sets.append((args[0].kind, args[1]))
            return None
        # Match the exact width probe the capture uses, not any script that
        # happens to mention naturalWidth -- the isolation script does too.
        if script.strip() == "return arguments[0].naturalWidth;":
            return getattr(args[0], "natural_width", 0)
        self.scripts.append(script)
        return None

    # -- behaviour ---------------------------------------------------------

    def _visible(self) -> dict[Locator, list[FakeElement]]:
        if self.current_url == LOGIN_URL:
            return {}
        return self.detail if self.on_detail else self.search

    def _press(self, element: Any) -> None:
        self.js_clicks.append(element)
        if element.kind == "show-graph":
            if self.login_after_submit:
                self.current_url = LOGIN_URL
                return
            self.on_detail = True
        elif element.kind in {"sid-filter", "graphfilter"}:
            self.filter_applied = True


def _capture(portal: FakePortal, target: NetcareTarget, timeout_seconds: int = 1) -> bytes:
    return _capture_via_driver(
        portal,  # type: ignore[arg-type]
        target,
        START,
        END,
        timeout_seconds,
        base_url=BASE_URL,
    )


# --- navigation and target input -------------------------------------------


@pytest.mark.parametrize(
    ("target_type", "expected_url", "expected_input"),
    [
        (NetcareTargetType.SID, SID_URL, "sid"),
        (NetcareTargetType.GRAPH_TITLE, GRAPH_TITLE_URL, "graphtitle"),
    ],
)
def test_capture_uses_the_mode_url_and_types_the_target_then_shows_the_graph(
    target_type: NetcareTargetType,
    expected_url: str,
    expected_input: str,
) -> None:
    """The target is typed into the mode's own input, not handed over in the URL."""

    portal = FakePortal(target_type)

    assert _capture(portal, _target(target_type)) == PNG
    assert portal.visited == [expected_url]
    assert portal.target_input.keys_sent == ["target-001", Keys.ENTER]
    assert portal.key_presses == [
        (expected_input, "target-001"),
        (expected_input, Keys.ENTER),
    ]


@pytest.mark.parametrize("target_type", [NetcareTargetType.SID, NetcareTargetType.GRAPH_TITLE])
def test_capture_clicks_show_graph_through_javascript(target_type: NetcareTargetType) -> None:
    """``a.btn-graph`` is clicked via JS, which survives the portal's overlay."""

    portal = FakePortal(target_type)

    _capture(portal, _target(target_type))

    assert portal.js_clicks[0] is portal.show_graph
    assert portal.scrolled == [portal.show_graph], "the button must be scrolled into view first"
    assert portal.native_clicks == [], "a native click is what the overlay swallows"


def test_capture_clears_the_input_before_typing() -> None:
    """A leftover value from the previous target would submit the wrong link."""

    portal = FakePortal()
    portal.target_input.value = "target-002"

    _capture(portal, _target(NetcareTargetType.SID))

    typed = "".join(str(key) for key in portal.target_input.keys_sent if key != Keys.ENTER)
    assert typed == "target-001"
    assert "target-002" not in portal.target_input.value


def test_capture_fails_when_the_detail_view_never_opens() -> None:
    """An empty search form must not be mistaken for a rendered detail page."""

    portal = FakePortal(detail_ready=False)

    with pytest.raises(RuntimeError, match="Detail page did not become ready"):
        _capture(portal, _target(NetcareTargetType.SID))


def test_capture_fails_when_the_search_form_never_appears() -> None:
    """A portal that lost its search input is an error, not a silent capture."""

    from selenium.common.exceptions import TimeoutException

    portal = FakePortal()
    portal.search = {}

    with pytest.raises(TimeoutException):
        _capture(portal, _target(NetcareTargetType.SID))


# --- date filter -----------------------------------------------------------


def test_sid_filter_sets_the_two_inputs_preceding_the_filter_button() -> None:
    """SID mode has no ids on its date fields, so they are found positionally."""

    portal = FakePortal(NetcareTargetType.SID)

    _capture(portal, _target(NetcareTargetType.SID))

    assert portal.field_sets == [("sid-start", START), ("sid-end", END)]
    assert portal.js_clicks[-1].kind == "sid-filter"


def test_sid_filter_uses_the_last_two_inputs_before_the_button() -> None:
    """An earlier unrelated input on the page must not become the range."""

    portal = FakePortal(NetcareTargetType.SID)
    stray = FakeElement("search-box", portal)
    portal.detail = dict(portal.detail)
    portal.detail[SID_DATES] = [stray, *portal.detail[SID_DATES]]

    _capture(portal, _target(NetcareTargetType.SID))

    assert portal.field_sets == [("sid-start", START), ("sid-end", END)]


def test_sid_filter_refuses_to_write_into_fewer_than_two_inputs() -> None:
    """Guessing which field is which would silently chart the wrong window."""

    portal = FakePortal(NetcareTargetType.SID)
    portal.detail = dict(portal.detail)
    portal.detail[SID_DATES] = [FakeElement("sid-only", portal)]

    with pytest.raises(RuntimeError, match="Date filter could not be applied"):
        _capture(portal, _target(NetcareTargetType.SID))
    assert portal.field_sets == []


def test_a_missing_filter_never_degrades_into_a_misleading_graph_timeout() -> None:
    """The whole point of Phase 25: the error must name what actually failed."""

    portal = FakePortal(NetcareTargetType.SID)
    portal.detail = dict(portal.detail)
    del portal.detail[SID_DATES]

    with pytest.raises(RuntimeError, match="Date filter could not be applied") as excinfo:
        _capture(portal, _target(NetcareTargetType.SID))

    assert "Graph did not render" not in str(excinfo.value)


def test_graph_title_filter_uses_the_real_element_ids() -> None:
    """``By.ID`` never matches ``#startdate``; the hash is not part of an id."""

    portal = FakePortal(NetcareTargetType.GRAPH_TITLE)

    _capture(portal, _target(NetcareTargetType.GRAPH_TITLE))

    assert portal.field_sets == [("startdate", START), ("enddate", END)]
    assert portal.js_clicks[-1].kind == "graphfilter"


def test_sid_capture_never_touches_the_graph_title_ids() -> None:
    """A SID branch has no ``#startdate`` field, so writing to it proves nothing."""

    portal = FakePortal(NetcareTargetType.SID)

    _capture(portal, _target(NetcareTargetType.SID))

    assert {kind for kind, _ in portal.field_sets} == {"sid-start", "sid-end"}


# --- graph detection and capture -------------------------------------------


def test_graph_is_only_accepted_after_the_form_was_completed() -> None:
    """The bug this phase fixes: an unfilled form yielded no image at all."""

    portal = FakePortal()

    assert _capture(portal, _target(NetcareTargetType.SID)) == PNG
    assert portal.filter_applied, "the graph must come from a filtered view, not the empty form"


def test_capture_fails_when_the_graph_does_not_render() -> None:
    portal = FakePortal(graph_renders=False)

    with pytest.raises(RuntimeError, match="Graph did not render"):
        _capture(portal, _target(NetcareTargetType.SID))


def test_a_narrow_image_is_not_treated_as_a_graph() -> None:
    """A placeholder image is narrower than a real RRDtool render."""

    portal = FakePortal(graph_width=120)

    with pytest.raises(RuntimeError, match="Graph did not render"):
        _capture(portal, _target(NetcareTargetType.SID))


def test_capture_isolates_and_restores_the_page() -> None:
    """Screenshots are taken with the rest of the page hidden, then put back."""

    portal = FakePortal()

    _capture(portal, _target(NetcareTargetType.SID))

    assert portal.captures == [portal.graph]
    assert portal.scripts[0] == service_module.GRAPH_ISOLATION_SCRIPT
    assert portal.scripts[-1] == service_module.GRAPH_RESTORE_SCRIPT


# --- expired session -------------------------------------------------------


def test_an_expired_session_fails_fast_instead_of_waiting_for_a_graph() -> None:
    """A login redirect costs one navigation, not a full timeout per target."""

    portal = FakePortal(login_on_load=True)

    started = time.monotonic()
    with pytest.raises(NetcareSessionExpiredError, match="session expired"):
        _capture(portal, _target(NetcareTargetType.SID), timeout_seconds=25)
    elapsed = time.monotonic() - started

    assert portal.visited == [SID_URL]
    assert portal.key_presses == [], "no form interaction once the session is gone"
    assert elapsed < 5, f"expired session burned {elapsed:.1f}s instead of failing fast"


def test_a_session_that_expires_on_submit_is_caught_before_waiting() -> None:
    """Submitting the form can be what finally bounces an expired session."""

    portal = FakePortal(login_after_submit=True)

    started = time.monotonic()
    with pytest.raises(NetcareSessionExpiredError, match="session expired"):
        _capture(portal, _target(NetcareTargetType.SID), timeout_seconds=25)
    elapsed = time.monotonic() - started

    assert portal.field_sets == [], "no date filter is applied to a login page"
    assert elapsed < 5, f"expired session burned {elapsed:.1f}s instead of failing fast"


# --- capturer wiring -------------------------------------------------------


class _StubSession:
    page_timeout_seconds = 25

    def __init__(self, driver: Any) -> None:
        self.driver = driver


def test_per_target_capture_hands_the_target_and_base_url_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capturer must pass the target object, not a pre-built URL."""

    recorded: list[dict[str, Any]] = []

    def fake_capture(
        driver: Any,
        target: NetcareTarget,
        start: str,
        end: str,
        timeout_seconds: int,
        *,
        base_url: str | None = None,
    ) -> bytes:
        recorded.append(
            {
                "driver": driver,
                "target": target,
                "start": start,
                "end": end,
                "timeout": timeout_seconds,
                "base_url": base_url,
            }
        )
        return PNG

    monkeypatch.setattr(service_module, "_capture_via_driver", fake_capture)

    target = _target(NetcareTargetType.GRAPH_TITLE, target_id="3784")
    capture = _per_target_capture(_StubSession("driver-object"), BASE_URL)  # type: ignore[arg-type]

    assert capture(target) == PNG
    assert recorded[0]["target"] is target
    assert recorded[0]["driver"] == "driver-object"
    assert recorded[0]["base_url"] == BASE_URL
    assert recorded[0]["timeout"] == 25
    assert recorded[0]["start"] < recorded[0]["end"]


def test_per_target_capture_honours_an_explicit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """An on-demand range is honoured rather than the whole of today."""

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service_module,
        "_capture_via_driver",
        lambda _d, _t, start, end, *_a, **_k: recorded.append((start, end)) or PNG,
    )

    window = (datetime(2026, 9, 18, 6, 0), datetime(2026, 9, 18, 12, 0))
    capture = _per_target_capture(  # type: ignore[arg-type]
        _StubSession("driver-object"),
        BASE_URL,
        window=window,
    )

    capture(_target(NetcareTargetType.SID))

    assert recorded == [("18/09/2026 06:00", "18/09/2026 12:00")]
