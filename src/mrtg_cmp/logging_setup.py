"""Application-wide logging: one timestamped format on the console and in a file.

A 24/7 monitoring host is diagnosed after the fact, so every record has to land
in ``logs/mrtg-cmp.log`` with an ISO-style timestamp, not only on the terminal
that may have scrolled away. The console keeps the same format so a line copied
from the terminal is byte-identical to the one in the log.

Wiring is deliberately idempotent: the CLI and the FastAPI lifespan both call
:func:`configure_logging`, and a duplicated handler would write every record
twice. Only handlers this module installed are ever removed, so uvicorn's and
pytest's own logging stays exactly as it was.

The file rotates because a daemon that never stops would otherwise fill the
disk with the very evidence it is supposed to keep (FR-16.1).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

#: One line, one format, on every destination: ``2026-09-29 14:03:21 [INFO] name: msg``.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Where the operator looks first. Overridable per deployment via ``LOG_FILE``.
DEFAULT_LOG_FILE = Path("logs/mrtg-cmp.log")

#: Rotate at 5 MiB and keep five generations: weeks of history, bounded disk.
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
FILE_ENCODING = "utf-8"

#: Third-party loggers that only add noise on this host.
#: ChromeDriver drives a single-connection pool against localhost, so urllib3
#: warns "Connection pool is full, discarding connection" for every scraper
#: command while nothing is actually wrong (FR-16.3).
NOISY_LOGGERS = ("urllib3.connectionpool",)

#: Marks the handlers this module owns, so reconfiguration never touches
#: handlers installed by uvicorn, pytest, or an embedding application.
_MANAGED_FLAG = "_mrtg_cmp_managed"

logger = logging.getLogger("mrtg_cmp.logging_setup")


@dataclass(frozen=True)
class _LoggingState:
    """What the currently installed handlers were built from."""

    requested: Path | None
    effective: Path | None
    level: int
    handlers: tuple[logging.Handler, ...]

    def matches(self, requested: Path | None, level: int) -> bool:
        return self.requested == requested and self.level == level


_ACTIVE: _LoggingState | None = None


def _resolve_log_file(log_file: Path | str | None) -> Path | None:
    """Return the expanded log path, or None when file logging is disabled."""

    if log_file is None or not str(log_file).strip():
        return None
    return Path(log_file).expanduser()


def _resolve_level(level: int | str) -> int:
    """Return a numeric level, falling back to INFO rather than silencing the app."""

    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if not isinstance(resolved, int):
        logger.warning("Unknown log level %r; falling back to INFO", level)
        return logging.INFO
    return resolved


def _formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    """Attach a handler this module owns, with the shared format."""

    handler.setFormatter(_formatter())
    setattr(handler, _MANAGED_FLAG, True)
    root.addHandler(handler)


def _remove_managed_handlers(root: logging.Logger) -> list[logging.Handler]:
    """Detach and close the handlers a previous call installed."""

    removed: list[logging.Handler] = []
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_FLAG, False):
            root.removeHandler(handler)
            removed.append(handler)
            with_handler = getattr(handler, "close", None)
            if callable(with_handler):
                with_handler()
    return removed


def _build_file_handler(log_file: Path) -> logging.Handler:
    """Create the rotating file handler, creating its parent directory first."""

    log_file.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding=FILE_ENCODING,
    )


def quiet_noisy_loggers(names: tuple[str, ...] = NOISY_LOGGERS) -> tuple[str, ...]:
    """Raise the level of loggers that only repeat expected behaviour."""

    for name in names:
        logging.getLogger(name).setLevel(logging.ERROR)
    return names


def configure_logging(
    log_file: Path | str | None = None,
    level: int | str = logging.INFO,
    stream: TextIO | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """Install one console handler and one rotating file handler on the root logger.

    Args:
        log_file: Destination for file output, or None for console-only logging.
        level: Minimum level for the whole process, by name or number.
        stream: Console destination, defaulting to stdout.
        force: Drop every existing handler, including ones this module did not
            install, before configuring.

    Returns:
        The resolved log file path, or None when file logging is disabled or the
        path could not be opened. A failure to write the log never prevents the
        service from starting: the console handler still receives everything.
    """

    global _ACTIVE

    root = logging.getLogger()
    requested = _resolve_log_file(log_file)
    numeric_level = _resolve_level(level)

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with_close = getattr(handler, "close", None)
            if callable(with_close):
                with_close()
        _ACTIVE = None
    elif _ACTIVE is not None and _ACTIVE.matches(requested, numeric_level):
        # Already wired for this destination and level. Reconfiguring would
        # close the open file for nothing, so only verify the handlers survived.
        if all(handler in root.handlers for handler in _ACTIVE.handlers):
            return _ACTIVE.effective

    _remove_managed_handlers(root)

    installed: list[logging.Handler] = []
    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    _install(root, console)
    installed.append(console)

    effective: Path | None = None
    if requested is not None:
        try:
            file_handler = _build_file_handler(requested)
        except OSError as exc:
            logger.warning("File logging disabled; cannot open %s: %s", requested, exc)
        else:
            _install(root, file_handler)
            installed.append(file_handler)
            effective = requested

    root.setLevel(numeric_level)
    quiet_noisy_loggers()
    _ACTIVE = _LoggingState(
        requested=requested,
        effective=effective,
        level=numeric_level,
        handlers=tuple(installed),
    )

    if effective is not None:
        logger.info("Logging to console and %s", effective)
    else:
        logger.info("Logging to console only (no log file configured)")
    return effective


__all__ = [
    "BACKUP_COUNT",
    "DEFAULT_LOG_FILE",
    "LOG_DATE_FORMAT",
    "LOG_FORMAT",
    "MAX_LOG_BYTES",
    "NOISY_LOGGERS",
    "configure_logging",
    "quiet_noisy_loggers",
]
