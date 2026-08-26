"""Unit tests for the persistent TelkomCare session manager and auto-login flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from selenium.common.exceptions import NoSuchElementException

from mrtg_cmp.netcare import session as session_module
from mrtg_cmp.netcare.captcha import GeminiCaptchaSolver
from mrtg_cmp.netcare.session import (
    CAPTCHA_IMAGE_SELECTOR,
    COOKIE_FILE_NAME,
    DEFAULT_CACHE_DIR,
    OTP_INPUT_SELECTOR,
    NetcareSession,
    generate_totp_code,
    is_authenticated_url,
    load_cookie_file,
    save_cookie_file,
)

#: Base32 test fixture only. This is the public example secret from the pyotp
#: documentation, not a real TelkomCare enrollment key.
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"

# --- Pure helpers ----------------------------------------------------------


def test_generate_totp_code_returns_six_digits() -> None:
    code = generate_totp_code(TEST_TOTP_SECRET)
    assert len(code) == 6
    assert code.isdigit()


def test_generate_totp_code_is_stable_within_same_window() -> None:
    secret = TEST_TOTP_SECRET
    assert generate_totp_code(secret) == generate_totp_code(secret)


def test_generate_totp_code_matches_pyotp_reference() -> None:
    import pyotp

    secret = TEST_TOTP_SECRET
    assert generate_totp_code(secret) == pyotp.TOTP(secret).now()


def test_generate_totp_code_rejects_blank_secret() -> None:
    with pytest.raises(ValueError):
        generate_totp_code("   ")


@pytest.mark.parametrize(
    "url",
    [
        "https://telkomcare.telkom.co.id/mrtgnetcare2",
        "https://telkomcare.telkom.co.id/mrtgnetcare2/graph",
        "https://telkomcare.telkom.co.id/mrtgnetcare2/graph/monitoring",
    ],
)
def test_is_authenticated_url_accepts_dashboard_paths(url: str) -> None:
    assert is_authenticated_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://telkomcare.telkom.co.id/",
        "https://telkomcare.telkom.co.id/public/login",
        "https://telkomcare.telkom.co.id/public/mfa",
        "https://telkomcare.telkom.co.id/public/login/msg/captcha",
    ],
)
def test_is_authenticated_url_rejects_login_paths(url: str) -> None:
    assert is_authenticated_url(url) is False


# --- Cookie persistence ----------------------------------------------------


def test_save_and_load_cookie_file_roundtrip(tmp_path: Path) -> None:
    cookies: list[dict[str, Any]] = [
        {"name": "PHPSESSID", "value": "abc", "domain": "telkomcare.telkom.co.id"},
        {"name": "csrf", "value": "xyz", "domain": "telkomcare.telkom.co.id"},
    ]
    path = tmp_path / COOKIE_FILE_NAME
    assert save_cookie_file(path, cookies) is True
    assert load_cookie_file(path) == cookies


def test_load_cookie_file_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_cookie_file(tmp_path / "absent.json") == []


def test_load_cookie_file_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / COOKIE_FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    assert load_cookie_file(path) == []


def test_load_cookie_file_ignores_non_list_payload(tmp_path: Path) -> None:
    path = tmp_path / COOKIE_FILE_NAME
    path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert load_cookie_file(path) == []


# --- Fake browser ----------------------------------------------------------


class FakeAlert:
    def __init__(self) -> None:
        self.accepted = False

    @property
    def text(self) -> str:
        return "alert"

    def accept(self) -> None:
        self.accepted = True


class NoAlert:
    @property
    def text(self) -> str:
        raise AssertionError("no alert expected")

    def accept(self) -> None:
        raise AssertionError("no alert expected")


class FakeSwitchTo:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    @property
    def alert(self) -> Any:
        if self._driver.alert is not None:
            alert, self._driver.alert = self._driver.alert, None
            return alert
        return NoAlert()

    def window(self, handle: str) -> None:
        self._driver.active_window = handle


class FakeElement:
    def __init__(self, driver: FakeDriver | None, kind: str) -> None:
        self.driver = driver
        self.kind = kind
        self.value: str = ""
        self.selected = False
        self.clicks = 0

    def clear(self) -> None:
        self.value = ""

    def send_keys(self, text: str) -> None:
        self.value += text

    def click(self) -> None:
        self.clicks += 1
        if self.driver is not None:
            self.driver.on_click(self.kind)

    def is_selected(self) -> bool:
        return self.selected

    @property
    def screenshot_as_png(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n captcha"

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n graph")


class FakeDriver:
    """Minimal WebDriver double covering every call the session manager makes.

    ``post_login_url`` models the portal: once cookies are loaded, navigating
    to the base URL lands the browser on the dashboard instead of the login form.
    """

    def __init__(
        self,
        elements: dict[str, list[FakeElement]] | None = None,
        post_login_url: str = "https://telkomcare.telkom.co.id/mrtgnetcare2",
    ) -> None:
        self.current_url = "https://telkomcare.telkom.co.id/"
        self.post_login_url = post_login_url
        self.page_source = "<html></html>"
        self.window_handles = ["win-1"]
        self.active_window = "win-1"
        self.cookies: list[dict[str, Any]] = []
        self.elements = elements or {}
        self.script_calls: list[str] = []
        self.visited: list[str] = []
        self.quit_called = False
        self.alert: Any = None
        self.switch_to = FakeSwitchTo(self)
        self.added_cookies: list[dict[str, Any]] = []
        self.deleted_all = False

    # -- navigation
    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = self.post_login_url if self.added_cookies else url

    def quit(self) -> None:
        self.quit_called = True

    # -- elements
    def find_element(self, by: str, value: str) -> Any:
        matches = self.elements.get(value) or []
        if not matches:
            raise KeyError(f"element not found: {value}")
        return matches[0]

    def find_elements(self, by: str, value: str) -> list[Any]:
        return list(self.elements.get(value) or [])

    def execute_script(self, script: str, *args: Any) -> Any:
        self.script_calls.append(script)
        if "arguments[0].click" in script:
            return None
        if "removeAttribute" in script:
            return None
        return None

    def on_click(self, kind: str) -> None:
        if kind == "submit":
            self.current_url = "https://telkomcare.telkom.co.id/public/mfa"
        elif kind == "dashboard-ready":
            self.current_url = "https://telkomcare.telkom.co.id/mrtgnetcare2/graph"

    # -- cookies
    def get_cookies(self) -> list[dict[str, Any]]:
        return list(self.cookies)

    def add_cookie(self, cookie: dict[str, Any]) -> None:
        self.added_cookies.append(cookie)

    def delete_all_cookies(self) -> None:
        self.deleted_all = True
        self.added_cookies = []


def _login_page_driver(
    post_login_url: str = "https://telkomcare.telkom.co.id/mrtgnetcare2",
) -> tuple[FakeDriver, dict[str, list[FakeElement]]]:
    """Build a driver exposing a complete TelkomCare login page.

    Returns the driver together with the element map, keyed by the selectors
    the session manager queries, so tests can assert on field values.
    """

    driver = FakeDriver(post_login_url=post_login_url)
    elements: dict[str, list[FakeElement]] = {
        "#uname": [FakeElement(driver, "uname")],
        "#passw": [FakeElement(driver, "passw")],
        CAPTCHA_IMAGE_SELECTOR: [FakeElement(driver, "captcha")],
        "#captcha-input": [FakeElement(driver, "captcha-input")],
        "#agree": [FakeElement(driver, "agree")],
        "#submit": [FakeElement(driver, "submit")],
    }
    driver.elements = elements
    return driver, elements


def _login_page_elements() -> dict[str, list[FakeElement]]:
    """Return a standalone login page element map (no driver side effects)."""

    return _login_page_driver()[1]


# --- Session behaviour -----------------------------------------------------


def test_session_reports_not_logged_in_without_driver() -> None:
    session = NetcareSession(driver=None)
    assert session.is_logged_in() is False


def test_session_is_logged_in_on_dashboard_url() -> None:
    driver = FakeDriver()
    driver.current_url = "https://telkomcare.telkom.co.id/mrtgnetcare2/graph"
    session = NetcareSession(driver=driver)
    assert session.is_logged_in() is True


def test_session_not_logged_in_on_login_url() -> None:
    driver = FakeDriver()
    driver.current_url = "https://telkomcare.telkom.co.id/public/login"
    session = NetcareSession(driver=driver)
    assert session.is_logged_in() is False


def test_session_save_cookies_writes_file(tmp_path: Path) -> None:
    driver = FakeDriver()
    driver.cookies = [{"name": "sid", "value": "1"}]
    session = NetcareSession(driver=driver, cookie_path=tmp_path / COOKIE_FILE_NAME)
    assert session.save_cookies() is True
    assert load_cookie_file(tmp_path / COOKIE_FILE_NAME) == [{"name": "sid", "value": "1"}]


def test_session_save_cookies_without_driver_returns_false(tmp_path: Path) -> None:
    session = NetcareSession(driver=None, cookie_path=tmp_path / COOKIE_FILE_NAME)
    assert session.save_cookies() is False


def test_session_load_cookies_applies_to_driver(tmp_path: Path) -> None:
    cookie_path = tmp_path / COOKIE_FILE_NAME
    save_cookie_file(cookie_path, [{"name": "sid", "value": "1", "sameSite": "Lax"}])
    driver = FakeDriver()
    session = NetcareSession(driver=driver, cookie_path=cookie_path)
    assert session.load_cookies() is True
    assert driver.deleted_all is True
    assert driver.added_cookies == [{"name": "sid", "value": "1"}]
    assert "https://telkomcare.telkom.co.id" in driver.visited


def test_session_load_cookies_returns_false_when_no_file(tmp_path: Path) -> None:
    driver = FakeDriver()
    session = NetcareSession(driver=driver, cookie_path=tmp_path / "missing.json")
    assert session.load_cookies() is False


def test_session_restore_session_detects_valid_cookies(tmp_path: Path) -> None:
    cookie_path = tmp_path / COOKIE_FILE_NAME
    save_cookie_file(cookie_path, [{"name": "sid", "value": "1"}])
    driver = FakeDriver()
    driver.current_url = "https://telkomcare.telkom.co.id/mrtgnetcare2"

    session = NetcareSession(driver=driver, cookie_path=cookie_path)
    # The driver navigates to base URL and stays on the dashboard path.
    assert session.restore_session() is True


def test_session_restore_session_detects_expired_cookies(tmp_path: Path) -> None:
    cookie_path = tmp_path / COOKIE_FILE_NAME
    save_cookie_file(cookie_path, [{"name": "sid", "value": "1"}])
    driver = FakeDriver(post_login_url="https://telkomcare.telkom.co.id/public/login")

    session = NetcareSession(driver=driver, cookie_path=cookie_path)
    assert session.restore_session() is False


def test_session_default_cookie_path_lives_in_the_cache_dir() -> None:
    """The cookie cache belongs beside the graph cache, not loose in data/."""
    session = NetcareSession(driver=None)
    assert session.cookie_path == Path(DEFAULT_CACHE_DIR) / COOKIE_FILE_NAME


def test_session_auto_login_requires_credentials() -> None:
    session = NetcareSession(
        driver=FakeDriver(_login_page_elements()),
        username="",
        password="secret",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=GeminiCaptchaSolver(api_keys=[], models=[]),
    )
    assert session.auto_login() is False


def test_session_auto_login_requires_totp_secret() -> None:
    session = NetcareSession(
        driver=FakeDriver(_login_page_elements()),
        username="user",
        password="secret",
        totp_secret="",
        captcha_solver=GeminiCaptchaSolver(api_keys=[], models=[]),
    )
    assert session.auto_login() is False


def test_session_auto_login_disabled_returns_false() -> None:
    session = NetcareSession(
        driver=FakeDriver(_login_page_elements()),
        username="user",
        password="secret",
        totp_secret=TEST_TOTP_SECRET,
        auto_login_enabled=False,
        captcha_solver=GeminiCaptchaSolver(api_keys=[], models=[]),
    )
    assert session.auto_login() is False


def test_session_auto_login_fills_credentials_and_captcha(tmp_path: Path) -> None:
    driver, elements = _login_page_driver()
    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
    )
    # The dashboard never becomes ready, so login ultimately fails, but the
    # credential and CAPTCHA fields must have been populated.
    assert session.auto_login() is False
    assert elements["#uname"][0].value == "user1"
    assert elements["#passw"][0].value == "secret1"
    assert elements["#captcha-input"][0].value == "aBc"
    assert elements["#submit"][0].clicks >= 1


def test_session_auto_login_fills_six_otp_boxes(tmp_path: Path) -> None:
    driver, elements = _login_page_driver()
    otp_elements = [FakeElement(driver, f"otp-{index}") for index in range(6)]
    elements[OTP_INPUT_SELECTOR] = otp_elements

    def fill_otp(code: str) -> bool:
        for el in otp_elements:
            el.value = ""
        for index, char in enumerate(code):
            otp_elements[index].send_keys(char)
        driver.current_url = "https://telkomcare.telkom.co.id/mrtgnetcare2"
        return True

    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
        otp_filler=fill_otp,
    )
    assert session.auto_login() is True
    assert "".join(el.value for el in otp_elements) == generate_totp_code(
        TEST_TOTP_SECRET
    )
    assert driver.current_url.startswith("https://telkomcare.telkom.co.id/mrtgnetcare2")


def test_session_close_is_idempotent() -> None:
    driver = FakeDriver()
    session = NetcareSession(driver=driver)
    session.close()
    session.close()
    assert driver.quit_called is True
    assert session.driver is None


def test_auto_login_announces_the_captcha_stage(tmp_path: Path) -> None:
    """The Gemini round trip is narrated so the dialog is not just a spinner."""

    from mrtg_cmp.netcare.query import STAGE_SOLVING_CAPTCHA

    driver, _ = _login_page_driver()
    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
    )
    stages: list[str] = []
    session.auto_login(on_stage=stages.append)

    # Auto-login retries, so the stage repeats once per attempt.
    assert stages
    assert set(stages) == {STAGE_SOLVING_CAPTCHA}


def test_auto_login_runs_without_a_stage_callback(tmp_path: Path) -> None:
    """Narration is optional: a caller that ignores stages still logs in."""

    driver, elements = _login_page_driver()
    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
    )
    session.auto_login()
    assert elements["#captcha-input"][0].value == "aBc"


class _StaticCaptchaSolver:
    """CAPTCHA solver double returning a fixed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def solve(self, image_bytes: bytes) -> str:
        self.calls += 1
        return self.code


class _LateLoginFormDriver(FakeDriver):
    """Driver whose login form only appears after a few lookups.

    The portal renders the form asynchronously after the navigation, so a raw
    ``find_element`` used to raise while the page was still painting.
    """

    def __init__(self, elements: dict[str, list[FakeElement]], delayed: int = 2) -> None:
        super().__init__(elements=elements)
        self.delayed = delayed
        self.misses = 0

    def find_element(self, by: str, value: str) -> Any:
        if value == "#uname" and self.misses < self.delayed:
            self.misses += 1
            raise NoSuchElementException("the login form is still rendering")
        return super().find_element(by, value)


def test_auto_login_waits_for_the_login_form_to_render(tmp_path: Path) -> None:
    """A slow page must be waited out, not failed on the first lookup."""

    _, elements = _login_page_driver()
    driver = _LateLoginFormDriver(elements)
    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
        login_attempts=1,  # retries must not be what rescues a slow render
    )

    session.auto_login()

    assert driver.misses == 2
    assert elements["#uname"][0].value == "user1"
    assert elements["#captcha-input"][0].value == "aBc"


def test_auto_login_gives_up_when_the_login_form_never_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form that never paints fails the attempt instead of hanging forever."""

    monkeypatch.setattr(session_module, "LOGIN_FIELD_TIMEOUT_SECONDS", 0.1)
    _, elements = _login_page_driver()
    driver = _LateLoginFormDriver(elements, delayed=99)
    session = NetcareSession(
        driver=driver,
        cookie_path=tmp_path / COOKIE_FILE_NAME,
        username="user1",
        password="secret1",
        totp_secret=TEST_TOTP_SECRET,
        captcha_solver=_StaticCaptchaSolver("aBc"),
    )

    assert session.auto_login() is False
    assert elements["#uname"][0].value == ""
