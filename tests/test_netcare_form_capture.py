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
from selenium.common.exceptions import NoAlertPresentException, NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from mrtg_cmp.netcare import service as service_module
from mrtg_cmp.netcare.service import (
    NetcareSessionExpiredError,
    _capture_via_driver,
    _dismiss_alert_if_present,
    _per_target_capture,
    _wait_for_loading_overlay,
)
from mrtg_cmp.netcare.targets import NetcareTarget, NetcareTargetType

BASE_URL = "https://portal.example.test"
SID_URL = f"{BASE_URL}/mrtgnetcare2/graph/monitoring"
GRAPH_TITLE_URL = f"{BASE_URL}/mrtgnetcare2/graph"
LOGIN_URL = f"{BASE_URL}/public/login"
START = "18/09/2026 00:00"
END = "18/09/2026 23:55"
PNG = b"\x89PNG\r\n\x1a\n rendered graph"
DATA_TABLES_WARNING = "DataTables warning: request to server failed."

Locator = tuple[str, str]
SID_INPUT: Locator = (By.NAME, "sid")
GRAPH_TITLE_INPUT: Locator = (By.NAME, "graphtitle")
SHOW_GRAPH: Locator = (By.CSS_SELECTOR, service_module.SHOW_GRAPH_SELECTOR)
SID_FILTER: Locator = (By.XPATH, service_module.SID_FILTER_BUTTON_XPATH)
SID_DATES: Locator = (By.XPATH, service_module.SID_DATE_INPUT_XPATH)
STARTDATE: Locator = (By.ID, "startdate")
ENDDATE: Locator = (By.ID, "enddate")
GRAPHFILTER: Locator = (By.ID, "graphfilter")
OVERLAYS: Locator = (By.CSS_SELECTOR, service_module.LOADING_OVERLAY_SELECTOR)


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
        self.portal.events.append("key-press")

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


class FakeAlert:
    """A modal browser alert, which blocks every click until it is accepted."""

    def __init__(self, portal: FakePortal, text: str) -> None:
        self.portal = portal
        self.text = text
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True
        self.portal.accepted_alerts.append(self)
        self.portal.pending_alert = None
        self.portal.events.append("alert-accepted")
        if self.portal.graph_waits_for_this_alert:
            # The graph is only in the DOM once the modal is gone, which is what
            # makes clearing the alert a precondition for the render.
            self.portal.graph_renders = True


class FakeSwitchTo:
    """The subset of ``driver.switch_to`` the alert handling touches."""

    def __init__(self, portal: FakePortal) -> None:
        self._portal = portal

    @property
    def alert(self) -> FakeAlert:
        alert = self._portal.pending_alert
        if alert is None:
            # Selenium raises exactly this when no alert is open; the capture
            # path must treat it as the normal case, not an error.
            raise NoAlertPresentException("no alert open")
        return alert


class FakePortal:
    """A TelkomCare graph page pair: a search form and a target detail view.

    Pressing "Show Graph" swaps the element map to the detail view, and pressing
    the date filter publishes the ``graph.php`` image. That ordering is the whole
    point of these tests: a capture that skips the form must never see a graph.

    ``alert_on_load`` / ``alert_on_filter`` raise a modal alert at those points,
    and ``overlay_polls`` keeps a loading overlay on screen for that many polls
    before detaching it, which is what an AJAX filter submission looks like.
    """

    def __init__(
        self,
        target_type: NetcareTargetType = NetcareTargetType.SID,
        login_on_load: bool = False,
        login_after_submit: bool = False,
        detail_ready: bool = True,
        graph_renders: bool = True,
        graph_width: int = 600,
        alert_on_load: bool = False,
        alert_on_filter: bool = False,
        overlay_polls: int = 0,
        overlay_never_detaches: bool = False,
        overlay_hidden: bool = False,
    ) -> None:
        self.target_type = target_type
        self.login_on_load = login_on_load
        self.login_after_submit = login_after_submit
        self.detail_ready = detail_ready
        self.graph_renders = graph_renders
        self.graph_width = graph_width
        self.alert_on_load = alert_on_load
        self.alert_on_filter = alert_on_filter
        self.overlay_polls = overlay_polls
        self.overlay_never_detaches = overlay_never_detaches
        self.overlay_hidden = overlay_hidden
        self.graph_waits_for_this_alert = False

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
        self.accepted_alerts: list[FakeAlert] = []
        self.pending_alert: FakeAlert | None = None
        self.overlay_active = overlay_polls > 0 or overlay_never_detaches
        self.events: list[str] = []
        self.switch_to = FakeSwitchTo(self)
        self.overlay = FakeElement("loading-overlay", self)
        self.overlay.displayed = not overlay_hidden

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
        if self.alert_on_load:
            self.raise_alert()

    def find_element(self, by: str, value: str) -> Any:
        matches = self._visible().get((by, value))
        if not matches:
            raise NoSuchElementException(f"no element for {by}={value!r}")
        return matches[0]

    def find_elements(self, by: str, value: str) -> list[Any]:
        if (by, value) == OVERLAYS:
            return self._overlays()
        if value == service_module.GRAPH_IMAGE_XPATH:
            # While the overlay is up the previous request's image is what the
            # DOM still holds, so the portal only publishes the new one after it.
            rendered = self.filter_applied and self.graph_renders and not self.overlay_active
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

    def raise_alert(self, text: str = DATA_TABLES_WARNING) -> FakeAlert:
        """Open a modal alert, the way the portal reports a DataTables failure."""

        alert = FakeAlert(self, text)
        self.pending_alert = alert
        self.events.append("alert-raised")
        return alert

    def _overlays(self) -> list[FakeElement]:
        """Report the loading overlay while it is still attached to the page."""

        if not self.overlay_active:
            return []
        if not self.overlay.displayed:
            # Mirrors the production visibility check: a hidden overlay is gone
            # for this portal's purposes.
            self.overlay_active = False
            self.events.append("overlay-cleared")
            return []
        self.events.append("overlay-poll")
        if not self.overlay_never_detaches:
            self.overlay_polls -= 1
            if self.overlay_polls <= 0:
                self.overlay_active = False
                self.events.append("overlay-cleared")
        return [self.overlay] if self.overlay_active else []

    def _press(self, element: Any) -> None:
        self.js_clicks.append(element)
        if element.kind == "show-graph":
            if self.login_after_submit:
                self.current_url = LOGIN_URL
                return
            self.on_detail = True
        elif element.kind in {"sid-filter", "graphfilter"}:
            self.filter_applied = True
            if self.alert_on_filter:
                self.raise_alert()
            elif self.overlay_polls > 0 or self.overlay_never_detaches:
                # The AJAX render the filter kicked off shows a busy overlay.
                self.overlay_active = True


def _capture(portal: FakePortal, target: NetcareTarget, timeout_seconds: int = 1) -> bytes:
    return _capture_via_driver(
        portal,  # type: ignore[arg-type]
        target,
        START,
        END,
        timeout_seconds,
        base_url=BASE_URL,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the deliberate sleeps instead of paying them.

    The alert settle and overlay poll waits are real wall-clock pauses in
    production. Paying them in a test would either slow the suite down by seconds
    or force the assertions to accept "no longer than 1s", which proves nothing
    about whether the sleep happened at all.
    """

    recorded: list[float] = []
    monkeypatch.setattr(service_module, "_sleep_hook", recorded.append)
    return recorded


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


# --- modal alert handling --------------------------------------------------


def test_dismissing_an_alert_accepts_it_and_lets_the_page_settle(sleeps: list[float]) -> None:
    """An accepted alert still needs a beat before the DOM is usable again."""

    portal = FakePortal()
    portal.raise_alert()

    assert _dismiss_alert_if_present(portal) is True

    assert portal.accepted_alerts[0].text == DATA_TABLES_WARNING
    assert portal.pending_alert is None
    assert sleeps == [service_module.ALERT_SETTLE_SECONDS]


def test_no_alert_is_the_cheap_case(sleeps: list[float]) -> None:
    """Absent alert is the normal case, so it must not cost a settle sleep."""

    portal = FakePortal()

    assert _dismiss_alert_if_present(portal) is False

    assert portal.accepted_alerts == []
    assert sleeps == [], "an absent alert must not sleep"


def test_a_capture_clears_an_alert_before_it_touches_the_form(sleeps: list[float]) -> None:
    """A modal alert swallows every click, so it goes before the first keypress."""

    portal = FakePortal(alert_on_load=True)

    assert _capture(portal, _target(NetcareTargetType.SID)) == PNG

    assert [alert.text for alert in portal.accepted_alerts] == [DATA_TABLES_WARNING]
    assert portal.events.index("alert-accepted") < portal.events.index("key-press")
    assert portal.key_presses, "the capture must still type the target afterwards"


def test_a_capture_clears_an_alert_raised_by_the_filter_click(sleeps: list[float]) -> None:
    """The portal's DataTables warning arrives with the filter, not the page load."""

    portal = FakePortal(alert_on_filter=True)

    assert _capture(portal, _target(NetcareTargetType.SID)) == PNG

    assert len(portal.accepted_alerts) == 1
    assert portal.accepted_alerts[0].text == DATA_TABLES_WARNING


def test_a_capture_clears_an_alert_that_appears_while_waiting_for_the_graph(
    sleeps: list[float],
) -> None:
    """An alert raised mid-wait freezes the render; the poll must clear it."""

    portal = FakePortal(graph_renders=False)
    portal.graph_waits_for_this_alert = True
    original_find_elements = portal.find_elements

    def find_elements(by: str, value: str) -> list[Any]:
        # Raise the alert once the capture is already polling for the graph.
        if value == service_module.GRAPH_IMAGE_XPATH and not portal.pending_alert:
            portal.raise_alert()
        return original_find_elements(by, value)

    portal.find_elements = find_elements  # type: ignore[method-assign]

    assert _capture(portal, _target(NetcareTargetType.SID), timeout_seconds=5) == PNG

    assert len(portal.accepted_alerts) == 1
    assert portal.events.index("alert-raised") > portal.events.index("key-press"), (
        "the alert has to arrive during the graph wait, not before the form"
    )


def test_a_capture_still_fails_fast_when_only_an_alert_is_in_the_way(sleeps: list[float]) -> None:
    """The alert must be cleared, not mistaken for a portal that lost its form."""

    portal = FakePortal(alert_on_load=True)
    portal.search = {}

    with pytest.raises(Exception):  # noqa: B017 - the specific timeout type is Selenium's
        _capture(portal, _target(NetcareTargetType.SID), timeout_seconds=25)

    assert portal.accepted_alerts, "the alert was dismissed; the form really is gone"


# --- loading overlay synchronisation ---------------------------------------


def test_waiting_for_the_overlay_returns_immediately_when_there_is_none(
    sleeps: list[float],
) -> None:
    """No overlay means no reason to spend a single poll."""

    portal = FakePortal()

    assert _wait_for_loading_overlay(portal) is True

    assert sleeps == []


def test_the_overlay_wait_polls_until_the_overlay_detaches(sleeps: list[float]) -> None:
    """The AJAX render is only finished once the busy indicator is gone."""

    portal = FakePortal(overlay_polls=3)

    assert _wait_for_loading_overlay(portal, timeout_seconds=10) is True

    assert portal.events.count("overlay-poll") == 3
    assert portal.events[-1] == "overlay-cleared"
    assert portal.overlay_active is False
    assert sleeps == [service_module.OVERLAY_POLL_SECONDS] * 2, (
        "the last poll is the one that observed the overlay gone, so it must not sleep"
    )


def test_the_overlay_wait_gives_up_at_its_timeout_instead_of_hanging(
    sleeps: list[float],
) -> None:
    """An overlay that outlives its budget means a slow portal, not a hang."""

    portal = FakePortal(overlay_never_detaches=True)

    assert _wait_for_loading_overlay(portal, timeout_seconds=0) is False
    assert sleeps == [], "a zero budget must not sleep before reporting"

    sleeps.clear()
    assert _wait_for_loading_overlay(portal, timeout_seconds=1) is False
    assert sleeps and all(seconds == service_module.OVERLAY_POLL_SECONDS for seconds in sleeps)


def test_a_hidden_overlay_is_not_treated_as_a_blocked_page(sleeps: list[float]) -> None:
    """Portal markup often keeps its spinner in the DOM permanently.

    Waiting on mere presence would stall every capture for the full timeout on a
    page that is not actually busy, so only a *displayed* overlay counts.
    """

    portal = FakePortal(overlay_never_detaches=True, overlay_hidden=True)

    assert _wait_for_loading_overlay(portal, timeout_seconds=10) is True
    assert sleeps == []


def test_the_capture_reads_the_graph_only_after_the_overlay_clears(
    sleeps: list[float],
) -> None:
    """The stale-image bug: the previous branch's graph is still on the page.

    While the overlay is up this portal serves the *wrong* image, so a capture
    that read it immediately would store another branch's traffic under this
    branch's name.
    """

    portal = FakePortal(overlay_polls=2)
    wrong_graph = FakeElement("stale-graph.php", portal, natural_width=600)
    real_find_elements = portal.find_elements

    def find_elements(by: str, value: str) -> list[Any]:
        if value == service_module.GRAPH_IMAGE_XPATH:
            if portal.overlay_active:
                # Still the previous request's image, which is what the DOM holds.
                return [wrong_graph]
            return [portal.graph] if portal.filter_applied else []
        return real_find_elements(by, value)

    portal.find_elements = find_elements  # type: ignore[method-assign]

    assert _capture(portal, _target(NetcareTargetType.SID)) == PNG

    assert portal.captures == [portal.graph], "the graph read while busy was the stale one"
    assert portal.overlay_active is False


def test_a_capture_survives_an_overlay_that_never_detaches(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck overlay is not fatal: the graph wait that follows is the real gate.

    The overlay helper is stubbed to time out rather than being driven by a
    portal that never clears, because a fake whose overlay never detaches can
    only end the poll loop on the wall clock — which these tests deliberately do
    not spend. ``test_the_overlay_wait_gives_up_at_its_timeout_instead_of_hanging``
    covers the timeout itself.
    """

    portal = FakePortal()
    monkeypatch.setattr(service_module, "_wait_for_loading_overlay", lambda *_a, **_k: False)

    assert _capture(portal, _target(NetcareTargetType.SID), timeout_seconds=5) == PNG

    assert portal.captures == [portal.graph]


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
