"""Shared pytest fixtures.

The CLI configures file logging as soon as it starts (FR-16.1), so without this
guard a test that calls ``main()`` would create ``logs/mrtg-cmp.log`` in the
repository root. Every test therefore gets a throwaway log destination.

The same hazard applies to the Netcare graph cache: rendering the dashboard
promotes the newest date partition into the flat root cache, so a test that hit
``/`` unguarded would rewrite the operator's real captures in
``data/netcare_cache/``. Every test therefore gets a throwaway cache directory.

The Netcare target catalog is isolated for a third reason: it is a deployment
file (``config/netcare_targets.csv`` is gitignored because it names that site's
own circuits), so whether it exists depends on whose checkout is running the
suite. Leaving it visible would make assertions about target ids and rendered
names pass or fail based on the developer. Every test therefore resolves against
the built-in anonymous catalog, and the override path is covered explicitly by
the tests that exercise it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mrtg_cmp.config import settings


@pytest.fixture(autouse=True)
def isolated_log_file(tmp_path: Path) -> Iterator[Path]:
    """Point application logging at a temporary file for the duration of a test."""

    log_file = tmp_path / "logs" / "mrtg-cmp.log"
    previous = settings.log_file
    settings.log_file = log_file
    try:
        yield log_file
    finally:
        settings.log_file = previous


@pytest.fixture(autouse=True)
def isolated_netcare_cache(tmp_path: Path) -> Iterator[Path]:
    """Point the Netcare graph cache at a temporary directory for the duration."""

    cache_dir = tmp_path / "netcare_cache"
    previous = settings.netcare_cache_dir
    settings.netcare_cache_dir = cache_dir
    try:
        yield cache_dir
    finally:
        settings.netcare_cache_dir = previous


@pytest.fixture(autouse=True)
def isolated_netcare_catalog(tmp_path: Path) -> Iterator[Path]:
    """Hide the checkout's own ``config/netcare_targets.csv`` from every test.

    The path is deliberately set rather than cleared: an empty setting makes
    :func:`resolve_targets` fall back to auto-discovery, which is exactly the
    per-developer behaviour this fixture exists to remove. A guaranteed-missing
    path takes the documented override branch and lands on the built-in
    anonymous catalog on every machine.
    """

    missing = tmp_path / "config" / "netcare_targets.csv"
    previous = settings.netcare_catalog_file
    settings.netcare_catalog_file = str(missing)
    try:
        yield missing
    finally:
        settings.netcare_catalog_file = previous
