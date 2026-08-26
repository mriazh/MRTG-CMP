"""Tests for the centralized file & console logging engine (FR-16.1, FR-16.3)."""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from mrtg_cmp.logging_setup import (
    DEFAULT_LOG_FILE,
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    configure_logging,
    quiet_noisy_loggers,
)

#: One log line as the operator reads it in logs/mrtg-cmp.log.
TIMESTAMPED_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] [\w.]+: .+$"
)

#: Loggers this suite silences, so a leak cannot bleed into other tests.
NOISY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "tests.noisy.logger",
)


@pytest.fixture
def isolated_root_logger() -> Iterator[logging.Logger]:
    """Hand each test a pristine root logger and restore the real one afterwards."""

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    try:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        yield root
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        for name, previous in noisy.items():
            logging.getLogger(name).setLevel(previous)


def _log_text(log_file: Path) -> str:
    return log_file.read_text(encoding="utf-8")


def _file_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """Only this module installs file handlers, so the count is unambiguous.

    pytest attaches its own stream handlers to the root logger, which is why
    absence of a file handler is asserted rather than a total handler count.
    """

    return [handler for handler in logger.handlers if isinstance(handler, logging.FileHandler)]


# --- The documented log contract ------------------------------------------


def test_log_format_is_the_documented_one() -> None:
    """FR-16.1 pins one format for both the console and the file."""

    assert LOG_FORMAT == "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    assert LOG_DATE_FORMAT == "%Y-%m-%d %H:%M:%S"


def test_default_log_file_is_logs_mrtg_cmp_log() -> None:
    """The operator asked for logs/mrtg-cmp.log without configuration."""

    expected = Path("logs") / "mrtg-cmp.log"
    assert expected == DEFAULT_LOG_FILE


def test_settings_default_log_file_is_the_documented_path() -> None:
    from mrtg_cmp.config import Settings

    assert Settings(_env_file=None).log_file == DEFAULT_LOG_FILE
    assert Settings(_env_file=None).log_level == "INFO"


def test_settings_read_the_log_file_override() -> None:
    from mrtg_cmp.config import Settings

    assert Settings(_env_file=None, log_file=Path("var/mrtg.log")).log_file == Path(
        "var/mrtg.log"
    )


def test_settings_treat_an_empty_log_file_as_file_logging_disabled() -> None:
    """An operator must be able to opt out with LOG_FILE= rather than a broken path."""

    from mrtg_cmp.config import Settings

    assert Settings(_env_file=None, log_file="").log_file is None


# --- File output -----------------------------------------------------------


def test_configure_logging_writes_a_timestamped_line_to_the_file(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """FR-16.1: every line carries an ISO-style date and time."""

    log_file = tmp_path / "mrtg-cmp.log"

    resolved = configure_logging(log_file)
    logging.getLogger("mrtg_cmp.netcare.service").info("captured 18/18 branch graphs")

    assert resolved == log_file
    assert log_file.is_file()
    line = _log_text(log_file).strip().splitlines()[-1]
    assert TIMESTAMPED_LINE.match(line), line
    assert line.endswith("captured 18/18 branch graphs")


def test_configure_logging_creates_the_missing_parent_directory(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """The logs/ directory does not exist on a fresh deployment."""

    log_file = tmp_path / "deeply" / "nested" / "logs" / "mrtg-cmp.log"

    configure_logging(log_file)
    logging.getLogger("mrtg_cmp.test").info("created on demand")

    assert log_file.parent.is_dir()
    assert log_file.is_file()


def test_configure_logging_expands_a_home_relative_path(
    isolated_root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOG_FILE=~/logs/app.log must not create a literal '~' directory."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    expected = tmp_path / "logs" / "mrtg-cmp.log"

    resolved = configure_logging("~/logs/mrtg-cmp.log")

    assert resolved == expected
    assert expected.is_file()


def test_configure_logging_mirrors_the_same_line_to_console_and_file(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """Console and file share one format so a pasted terminal line matches the log."""

    log_file = tmp_path / "mrtg-cmp.log"
    stream = io.StringIO()

    configure_logging(log_file, stream=stream)
    logging.getLogger("mrtg_cmp.collector").warning("tunnel handshake failed")

    console = stream.getvalue().strip().splitlines()[-1]
    filed = [line for line in _log_text(log_file).splitlines() if "tunnel handshake" in line][0]
    assert TIMESTAMPED_LINE.match(console), console
    assert console == filed


def test_configure_logging_honours_the_configured_level(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """A quiet deployment must not fill the disk with debug noise."""

    log_file = tmp_path / "mrtg-cmp.log"

    configure_logging(log_file, level="WARNING")
    logging.getLogger("mrtg_cmp.test").info("below the threshold")
    logging.getLogger("mrtg_cmp.test").warning("at the threshold")

    text = _log_text(log_file)
    assert "below the threshold" not in text
    assert "at the threshold" in text


def test_configure_logging_resolves_a_level_name(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    configure_logging(tmp_path / "mrtg-cmp.log", level="debug")
    assert isolated_root_logger.level == logging.DEBUG


def test_configure_logging_falls_back_to_info_for_an_unknown_level(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """A typo in LOG_LEVEL must not silence the service."""

    configure_logging(tmp_path / "mrtg-cmp.log", level="chatty")
    assert isolated_root_logger.level == logging.INFO


# --- Idempotence and politeness -------------------------------------------


def test_configure_logging_repeated_calls_do_not_duplicate_handlers(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """The CLI and the web lifespan both configure logging; records must not double."""

    log_file = tmp_path / "mrtg-cmp.log"

    configure_logging(log_file)
    first_count = len(isolated_root_logger.handlers)
    configure_logging(log_file)
    configure_logging(log_file)

    assert len(isolated_root_logger.handlers) == first_count
    assert len(_file_handlers(isolated_root_logger)) == 1

    logging.getLogger("mrtg_cmp.test").info("exactly once")
    assert len([line for line in _log_text(log_file).splitlines() if "exactly once" in line]) == 1


def test_configure_logging_switches_to_a_new_path(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """A different LOG_FILE must take effect, not be ignored as a repeat call."""

    first = tmp_path / "first.log"
    second = tmp_path / "second.log"

    configure_logging(first)
    configure_logging(second)
    logging.getLogger("mrtg_cmp.test").info("moved")

    assert _log_text(first).count("moved") == 0
    assert _log_text(second).count("moved") == 1


def test_configure_logging_keeps_handlers_it_did_not_install(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """uvicorn, pytest, and embedders own their handlers; we only manage ours."""

    foreign = logging.NullHandler()
    isolated_root_logger.addHandler(foreign)

    configure_logging(tmp_path / "mrtg-cmp.log")

    assert foreign in isolated_root_logger.handlers


def test_configure_logging_force_clears_foreign_handlers(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """force=True is the operator's explicit 'start from a clean slate'."""

    foreign = logging.NullHandler()
    isolated_root_logger.addHandler(foreign)

    configure_logging(tmp_path / "mrtg-cmp.log", force=True)

    assert foreign not in isolated_root_logger.handlers
    assert len(_file_handlers(isolated_root_logger)) == 1


# --- Degraded modes --------------------------------------------------------


def test_configure_logging_without_a_file_is_console_only(
    isolated_root_logger: logging.Logger
) -> None:
    """LOG_FILE= leaves the console handler doing the whole job."""

    stream = io.StringIO()

    resolved = configure_logging(None, stream=stream)
    logging.getLogger("mrtg_cmp.test").info("console only")

    assert resolved is None
    assert _file_handlers(isolated_root_logger) == []
    assert "console only" in stream.getvalue()


def test_configure_logging_survives_an_unusable_log_path(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """An unwritable log path must never take the monitoring host down."""

    blocked = tmp_path / "not-a-log-file"
    blocked.mkdir()
    stream = io.StringIO()

    resolved = configure_logging(blocked, stream=stream)
    logging.getLogger("mrtg_cmp.test").info("still logging to the console")

    assert resolved is None
    assert _file_handlers(isolated_root_logger) == []
    assert "still logging to the console" in stream.getvalue()


# --- Log hygiene (FR-16.3) -------------------------------------------------


def test_configure_logging_quiets_the_urllib3_connection_pool(
    isolated_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """ChromeDriver talks to a size-1 pool, so every discard warning is noise."""

    configure_logging(tmp_path / "mrtg-cmp.log")
    assert logging.getLogger("urllib3.connectionpool").level == logging.ERROR


def test_quiet_noisy_libraries_accepts_extra_loggers() -> None:
    quiet_noisy_loggers(("tests.noisy.logger",))
    assert logging.getLogger("tests.noisy.logger").level == logging.ERROR


def test_quiet_noisy_libraries_defaults_to_the_connection_pool() -> None:
    quiet_noisy_loggers()
    assert logging.getLogger("urllib3.connectionpool").level == logging.ERROR


# --- Gemini model sanity (FR-16.3) -----------------------------------------


def test_default_gemini_models_are_valid_vision_models() -> None:
    from mrtg_cmp.config import Settings
    from mrtg_cmp.netcare.captcha import VALID_GEMINI_VISION_MODELS

    assert Settings(_env_file=None).gemini_models == list(VALID_GEMINI_VISION_MODELS)


def test_valid_gemini_vision_models_are_real_model_names() -> None:
    """FR-16.3: the rotation must stay inside models Google actually serves."""

    from mrtg_cmp.netcare.captcha import VALID_GEMINI_VISION_MODELS

    assert VALID_GEMINI_VISION_MODELS == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    )
    assert all(model.startswith("gemini-") for model in VALID_GEMINI_VISION_MODELS)


def test_valid_gemini_vision_models_lead_with_the_high_quota_lites() -> None:
    """The 500 RPD / 15 RPM lites must be tried first or logins hit a rate limit.

    Order is the whole point of the catalog: a CAPTCHA is one short request, so
    the cheapest-quota model that can read the image should answer it before any
    metered model is touched.
    """

    from mrtg_cmp.netcare.captcha import VALID_GEMINI_VISION_MODELS

    assert VALID_GEMINI_VISION_MODELS[:2] == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    )


# --- Entry-point wiring ----------------------------------------------------


@pytest.fixture
def cli_stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Replace every long-running CLI dependency with an inert stub."""

    from mrtg_cmp import cli as cli_module

    class StubCollector:
        def run(self, **kwargs: object) -> None:
            return None

    class StubDaemon:
        interval_seconds = 300
        service = type("S", (), {"targets": ("t1", "t2")})()

        def install_signal_handlers(self, stop_event: object) -> None:
            return None

        def run(self, stop_event: object = None, max_rounds: int | None = None) -> int:
            return 0

    monkeypatch.setattr(cli_module.settings, "database_path", tmp_path / "cli.db")
    monkeypatch.setattr(cli_module.settings, "netcare_cache_dir", tmp_path / "netcare_cache")
    monkeypatch.setattr(cli_module.settings, "netcare_enabled", True)
    monkeypatch.setattr(cli_module, "TrafficCollector", StubCollector)
    monkeypatch.setattr(cli_module, "build_daemon_from_settings", lambda cfg=None: StubDaemon())
    monkeypatch.setattr(cli_module.uvicorn, "run", lambda *a, **k: None)
    return cli_module


@pytest.mark.parametrize(
    "argv",
    [
        ["init-db"],
        ["create-user", "-u", "operator", "-p", "OperatorPass1!"],
        ["collect", "-n", "1"],
        ["web"],
        ["netcare", "-n", "1"],
        ["all"],
        ["all", "--with-netcare"],
    ],
)
def test_every_cli_entrypoint_configures_logging(
    cli_stubs: object, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """FR-16.1: web, collect, netcare, and all must all write to the log file."""

    configured: list[object] = []
    monkeypatch.setattr(
        cli_stubs, "configure_logging", lambda *a, **k: configured.append(a) or None
    )

    assert cli_stubs.main(argv) == 0  # type: ignore[attr-defined]
    assert configured, f"{argv[0]} never configured logging"


def test_cli_startup_writes_a_timestamped_line_to_the_log_file(
    cli_stubs: object,
    isolated_root_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The operator can open logs/mrtg-cmp.log and see when the service started."""

    log_file = tmp_path / "logs" / "mrtg-cmp.log"
    monkeypatch.setattr(cli_stubs.settings, "log_file", log_file)

    assert cli_stubs.main(["init-db"]) == 0  # type: ignore[attr-defined]

    assert log_file.is_file()
    assert TIMESTAMPED_LINE.match(_log_text(log_file).strip().splitlines()[0])


def test_web_lifespan_configures_logging(
    cli_stubs: object,
    isolated_root_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`uvicorn mrtg_cmp.web.app:app` bypasses the CLI, so the app configures itself."""

    import asyncio

    from mrtg_cmp.web import app as app_module

    log_file = tmp_path / "logs" / "mrtg-cmp.log"
    monkeypatch.setattr(cli_stubs.settings, "log_file", log_file)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_stubs.settings, "database_path", tmp_path / "web.db")  # type: ignore[attr-defined]

    async def run_lifespan() -> None:
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(run_lifespan())

    assert log_file.is_file()
    assert TIMESTAMPED_LINE.match(_log_text(log_file).strip().splitlines()[0])
