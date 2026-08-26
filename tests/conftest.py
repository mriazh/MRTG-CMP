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
from starlette.testclient import TestClient

from mrtg_cmp.auth import hash_password
from mrtg_cmp.config import settings
from mrtg_cmp.db import Database, TrafficSample
from mrtg_cmp.web.app import app


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


@pytest.fixture(autouse=True)
def isolated_orbit_cache(tmp_path: Path) -> Iterator[Path]:
    """Point the Orbit status cache at a temporary directory for the duration."""

    cache_dir = tmp_path / "orbit_cache"
    previous = settings.orbit_cache_dir
    settings.orbit_cache_dir = cache_dir
    try:
        yield cache_dir
    finally:
        settings.orbit_cache_dir = previous


@pytest.fixture(autouse=True)
def isolated_orbit_catalog(tmp_path: Path) -> Iterator[Path]:
    """Hide the checkout's own Orbit catalog from tests by defaulting to fallback."""

    missing = tmp_path / "config" / "orbit_targets.csv"
    previous = settings.orbit_catalog_file
    settings.orbit_catalog_file = str(missing)
    try:
        yield missing
    finally:
        settings.orbit_catalog_file = previous


@pytest.fixture
def client_with_db(tmp_path: Path) -> TestClient:
    """Fixture providing a test client configured with a temporary database."""
    test_db_path = tmp_path / "web_test.db"
    db = Database(test_db_path)
    db.initialize()

    # Seed test user
    db.create_user("admin", hash_password("admin123"))

    # Seed some sample traffic records
    db.insert_traffic_sample(
        TrafficSample(
            timestamp="2026-09-15T08:00:00Z",
            rx_bytes=100_000_000,
            tx_bytes=50_000_000,
            rx_bps=10_000_000.0,
            tx_bps=5_000_000.0,
            epoch=1789459200,
            uptime="5d",
            status="UP",
        )
    )

    settings.database_path = test_db_path
    client = TestClient(app, follow_redirects=False)
    return client
