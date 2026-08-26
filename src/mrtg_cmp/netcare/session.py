"""Persistent TelkomCare browser session with cookie cache and TOTP auto-login.

The session layer owns three concerns:

* a persistent browser profile directory so the portal session survives restarts,
* an exported ``cookies.json`` cache so Gemini quota is only spent when the
  portal session has actually expired,
* the zero-touch auto-login flow (credentials -> Gemini CAPTCHA -> TOTP MFA).

Selenium is imported lazily so the FastAPI process never pays for it, and the
driver is typed against a small structural protocol so tests can drive the flow
with a plain fake.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import pyotp

from .captcha import GeminiCaptchaSolver
from .query import STAGE_SOLVING_CAPTCHA

logger = logging.getLogger("mrtg_cmp.netcare.session")

COOKIE_FILE_NAME = "cookies.json"
DASHBOARD_PATH = "/mrtgnetcare2"
DEFAULT_CACHE_DIR = "data/netcare_cache"

CAPTCHA_IMAGE_SELECTOR = "#captcha-element img"
OTP_INPUT_SELECTOR = "input[name='otp[]']"
OTP_INPUT_COUNT = 6
LOGIN_REJECT_FRAGMENT = "/public/login/msg/"

#: Seconds the login form gets to appear after a navigation. The portal paints it
#: asynchronously, so a raw ``find_element`` on a cold profile raises while the
#: page is still rendering.
LOGIN_FIELD_TIMEOUT_SECONDS = 10

#: Cookie keys that Chrome refuses when re-adding through the WebDriver API.
_DROPPED_COOKIE_KEYS = ("sameSite", "storeId", "hostOnly", "session", "expirySource")


class BrowserLike(Protocol):
    """Structural subset of the Selenium WebDriver API used by this module."""

    current_url: str
    page_source: str
    window_handles: list[str]

    def get(self, url: str) -> Any: ...

    def quit(self) -> Any: ...

    def find_element(self, by: str, value: str) -> Any: ...

    def find_elements(self, by: str, value: str) -> list[Any]: ...

    def execute_script(self, script: str, *args: Any) -> Any: ...

    def get_cookies(self) -> list[dict[str, Any]]: ...

    def add_cookie(self, cookie: dict[str, Any]) -> Any: ...

    def delete_all_cookies(self) -> Any: ...


#: Callable that types the 6-digit TOTP code into the MFA form.
OtpFiller = Callable[[str], bool]


def is_authenticated_url(url: str) -> bool:
    """Return True when the URL sits on the authenticated MRTG dashboard."""

    if not url:
        return False
    path = urlparse(url).path.lower()
    if path.startswith(("/public/login", "/public/mfa")):
        return False
    return path == DASHBOARD_PATH or path.startswith(f"{DASHBOARD_PATH}/")


def generate_totp_code(secret: str, at: float | None = None) -> str:
    """Return the current 6-digit TOTP code for ``secret``."""

    cleaned = secret.strip()
    if not cleaned:
        raise ValueError("TOTP secret must not be empty")
    totp = pyotp.TOTP(cleaned)
    return totp.now() if at is None else totp.at(int(at))


def load_cookie_file(path: Path) -> list[dict[str, Any]]:
    """Read the cookie cache, returning an empty list when unusable."""

    resolved = Path(path)
    if not resolved.is_file():
        return []
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable cookie cache %s: %s", resolved, exc)
        return []
    if not isinstance(payload, list) or not payload:
        return []
    return [item for item in payload if isinstance(item, dict)]


def save_cookie_file(path: Path, cookies: list[dict[str, Any]]) -> bool:
    """Persist the cookie cache with owner-only permissions where supported."""

    resolved = Path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        if os.name != "nt":
            with contextlib.suppress(OSError):  # best-effort owner-only hardening
                os.chmod(resolved, 0o600)
    except (OSError, TypeError, ValueError) as exc:
        logger.error("Failed to persist cookie cache %s: %s", resolved, exc)
        return False
    return True


def sanitize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    """Strip WebDriver-rejected cookie keys before re-adding a saved cookie."""

    cleaned = dict(cookie)
    for key in _DROPPED_COOKIE_KEYS:
        cleaned.pop(key, None)
    if isinstance(cleaned.get("expiry"), float):
        cleaned["expiry"] = int(cleaned["expiry"])
    return cleaned


def _login_field_wait(driver: Any) -> tuple[Any, Any]:
    """Import Selenium lazily and return the expected-conditions helper and a wait.

    Selenium is imported here rather than at module scope so the FastAPI process
    never loads it until a login actually needs a browser.
    """

    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait

    return ec, WebDriverWait(driver, LOGIN_FIELD_TIMEOUT_SECONDS)


class NetcareSession:
    """Manage the persistent TelkomCare browser session."""

    def __init__(
        self,
        driver: BrowserLike | None = None,
        base_url: str = "https://telkomcare.telkom.co.id",
        cookie_path: Path | None = None,
        profile_dir: Path | None = None,
        headless: bool = True,
        username: str = "",
        password: str = "",
        totp_secret: str = "",
        auto_login_enabled: bool = True,
        captcha_solver: GeminiCaptchaSolver | None = None,
        otp_filler: OtpFiller | None = None,
        page_timeout_seconds: int = 30,
        login_attempts: int = 3,
    ) -> None:
        self.driver = driver
        self.base_url = base_url.rstrip("/")
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.cookie_path = (
            Path(cookie_path) if cookie_path else Path(DEFAULT_CACHE_DIR) / COOKIE_FILE_NAME
        )
        self.headless = headless
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.auto_login_enabled = auto_login_enabled
        self.page_timeout_seconds = page_timeout_seconds
        self.login_attempts = max(1, login_attempts)
        self.captcha_solver = captcha_solver
        self.otp_filler = otp_filler

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Launch the headless browser with a persistent profile (idempotent)."""

        if self.driver is not None:
            return True
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
        except ImportError:  # pragma: no cover - selenium is an optional runtime dep
            logger.error("selenium is not installed; cannot start the Netcare browser")
            return False

        profile_dir = self.profile_dir
        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)

        options = ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        if profile_dir is not None:
            options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        try:
            self.driver = webdriver.Chrome(options=options)  # type: ignore[assignment]
        except Exception as exc:
            logger.error("Failed to start Chrome for the Netcare scraper: %s", exc)
            self.driver = None
            return False
        return True

    def close(self) -> None:
        """Quit the browser and drop the driver reference. Idempotent."""

        driver, self.driver = self.driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception as exc:  # pragma: no cover - defensive shutdown
            logger.debug("Ignoring error while closing browser: %s", exc)

    # -- session state -----------------------------------------------------

    def is_logged_in(self) -> bool:
        """Return True when the current page is the authenticated dashboard."""

        driver = self.driver
        if driver is None:
            return False
        try:
            return is_authenticated_url(driver.current_url)
        except Exception as exc:
            logger.debug("Unable to read current URL: %s", exc)
            return False

    def save_cookies(self) -> bool:
        """Export browser cookies into the cookie cache."""

        driver = self.driver
        if driver is None:
            logger.warning("No browser session; nothing to persist")
            return False
        try:
            return save_cookie_file(self.cookie_path, driver.get_cookies())
        except Exception as exc:
            logger.error("Failed reading cookies from the browser: %s", exc)
            return False

    def load_cookies(self) -> bool:
        """Import the cookie cache into the browser session."""

        driver = self.driver
        if driver is None:
            return False
        cookies = load_cookie_file(self.cookie_path)
        if not cookies:
            return False
        try:
            driver.get(self.base_url)
            driver.delete_all_cookies()
            for cookie in cookies:
                try:
                    driver.add_cookie(sanitize_cookie(cookie))
                except Exception as exc:
                    logger.debug("Skipping cookie %r: %s", cookie.get("name"), exc)
        except Exception as exc:
            logger.error("Failed applying cookie cache: %s", exc)
            return False
        return True

    def restore_session(self) -> bool:
        """Reload cached cookies and confirm the portal still accepts them."""

        driver = self.driver
        if driver is None or not self.load_cookies():
            return False
        try:
            driver.get(self.base_url)
        except Exception as exc:
            logger.error("Failed navigating after restoring cookies: %s", exc)
            return False
        return self.is_logged_in()

    def login(self, on_stage: Callable[[str], None] | None = None) -> bool:
        """Establish an authenticated session, preferring cached cookies.

        ``on_stage`` narrates the slow parts — launching Chrome, solving the
        CAPTCHA — so a caller can show what the operator is waiting for instead
        of an undifferentiated spinner.
        """

        if self.driver is None and not self.start():
            return False
        if self.restore_session():
            logger.info("Reused the cached TelkomCare session")
            return True
        try:
            self.driver.get(self.base_url)  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("Failed opening the TelkomCare portal: %s", exc)
            return False
        if self.is_logged_in():
            return True
        return self.auto_login(on_stage=on_stage)

    # -- auto login --------------------------------------------------------

    def auto_login(self, on_stage: Callable[[str], None] | None = None) -> bool:
        """Run the zero-touch login flow: credentials, Gemini CAPTCHA, TOTP."""

        driver = self.driver
        if driver is None:
            return False
        if not self.auto_login_enabled:
            logger.debug("Netcare auto-login is disabled")
            return False
        if not self.username or not self.password:
            logger.warning("Auto-login needs TELKOM_USER and TELKOM_PASSWORD to be set")
            return False
        if not self.totp_secret:
            logger.warning("Auto-login needs TOTP_SECRET to be set")
            return False

        for attempt in range(1, self.login_attempts + 1):
            if self.is_logged_in():
                return self._finalize_login()
            try:
                if self._run_login_attempt(on_stage=on_stage):
                    return self._finalize_login()
            except Exception as exc:
                logger.warning(
                    "Auto-login attempt %s/%s failed: %s",
                    attempt,
                    self.login_attempts,
                    exc,
                )
            if attempt < self.login_attempts:
                time.sleep(1)

        logger.error("Netcare auto-login failed after %s attempts", self.login_attempts)
        return False

    def _run_login_attempt(self, on_stage: Callable[[str], None] | None = None) -> bool:
        driver = self.driver
        if driver is None:
            return False

        driver.get(self.base_url)
        ec, wait = _login_field_wait(driver)
        # The form is rendered client-side, so both fields are waited for rather
        # than assumed present the moment the navigation returns.
        username_input = wait.until(ec.presence_of_element_located(("css selector", "#uname")))
        password_input = wait.until(ec.presence_of_element_located(("css selector", "#passw")))
        username_input.clear()
        username_input.send_keys(self.username)
        password_input.clear()
        password_input.send_keys(self.password)

        if on_stage is not None:
            on_stage(STAGE_SOLVING_CAPTCHA)
        captcha_code = self._solve_captcha(driver)
        if not captcha_code:
            return False

        captcha_input = driver.find_element("css selector", "#captcha-input")
        captcha_input.clear()
        captcha_input.send_keys(captcha_code)

        agree = driver.find_element("css selector", "#agree")
        if not agree.is_selected():
            driver.execute_script("arguments[0].click();", agree)

        driver.find_element("css selector", "#submit").click()

        if LOGIN_REJECT_FRAGMENT in driver.current_url:
            logger.warning("TelkomCare rejected the CAPTCHA; retrying with a fresh image")
            return False

        if self.is_logged_in():
            return True

        otp_inputs = driver.find_elements("css selector", OTP_INPUT_SELECTOR)
        if len(otp_inputs) == OTP_INPUT_COUNT:
            return self._submit_totp(otp_inputs)
        return False

    def _solve_captcha(self, driver: BrowserLike) -> str:
        solver = self.captcha_solver
        if solver is None:
            logger.warning("No CAPTCHA solver configured")
            return ""
        try:
            image_element = driver.find_element("css selector", CAPTCHA_IMAGE_SELECTOR)
            return solver.solve(image_element.screenshot_as_png)
        except Exception as exc:
            logger.warning("Failed solving the TelkomCare CAPTCHA: %s", exc)
            return ""

    def _submit_totp(self, otp_inputs: list[Any]) -> bool:
        filler = self.otp_filler or self._default_otp_filler
        code = generate_totp_code(self.totp_secret)
        if not filler(code):
            return False
        return self.is_logged_in()

    def _default_otp_filler(self, code: str) -> bool:
        driver = self.driver
        if driver is None:
            return False
        try:
            otp_inputs = driver.find_elements("css selector", OTP_INPUT_SELECTOR)
        except Exception as exc:
            logger.warning("Unable to locate the OTP inputs: %s", exc)
            return False
        if len(otp_inputs) != OTP_INPUT_COUNT:
            return False
        try:
            for index, char in enumerate(code):
                element = otp_inputs[index]
                driver.execute_script("arguments[0].removeAttribute('disabled');", element)
                element.clear()
                element.send_keys(char)
        except Exception as exc:
            logger.warning("Failed filling the TOTP code: %s", exc)
            return False
        return True

    def _finalize_login(self) -> bool:
        self.save_cookies()
        return self.is_logged_in()


__all__ = [
    "CAPTCHA_IMAGE_SELECTOR",
    "COOKIE_FILE_NAME",
    "DASHBOARD_PATH",
    "DEFAULT_CACHE_DIR",
    "LOGIN_REJECT_FRAGMENT",
    "OTP_INPUT_COUNT",
    "OTP_INPUT_SELECTOR",
    "BrowserLike",
    "NetcareSession",
    "OtpFiller",
    "generate_totp_code",
    "is_authenticated_url",
    "load_cookie_file",
    "sanitize_cookie",
    "save_cookie_file",
]
