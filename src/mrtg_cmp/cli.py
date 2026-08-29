"""Command-line interface (CLI) for MRTG operations."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
from collections.abc import Sequence

import uvicorn

from .auth import change_password_and_revoke_sessions, ensure_admin_user, hash_password
from .collector import TrafficCollector
from .config import settings
from .db import Database
from .logging_setup import configure_logging
from .netcare.service import build_daemon_from_settings
from .orbit.service import build_orbit_daemon_from_settings

logger = logging.getLogger("mrtg_cmp.cli")


def cmd_init_db(args: argparse.Namespace) -> int:
    """Initialize SQLite database schema and seed default administrator."""
    db = Database(settings.database_path)
    logger.info("Initializing database at: %s", settings.database_path)
    db.initialize()
    admin = ensure_admin_user(db, settings)
    logger.info("Database initialized. Administrator user '%s' is ready.", admin["username"])
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    """Create or update a dashboard user password."""
    db = Database(settings.database_path)
    db.initialize()

    username = args.username.strip()
    if not username:
        logger.error("Username cannot be empty")
        return 1

    password = args.password
    if not password:
        password = getpass.getpass(f"Enter password for '{username}': ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            logger.error("Passwords do not match")
            return 1

    hashed = hash_password(password)
    user = db.get_user(username)
    if user:
        # Update existing user's password
        with db.connection() as conn, conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user["id"]))
        logger.info("Password updated successfully for existing user '%s'.", username)
    else:
        user_id = db.create_user(username=username, password_hash=hashed)
        logger.info("Created new user '%s' with id %d.", username, user_id)
    return 0


def cmd_change_password(args: argparse.Namespace) -> int:
    """Change user password and revoke all active sessions for that user."""
    db = Database(settings.database_path)
    db.initialize()

    username = args.username.strip()
    if not username:
        logger.error("Username cannot be empty")
        return 1

    # Get old password securely
    old_password = getpass.getpass(f"Enter current password for '{username}': ")
    new_password = getpass.getpass("Enter new password: ")
    confirm = getpass.getpass("Confirm new password: ")
    
    if new_password != confirm:
        logger.error("Passwords do not match")
        return 1
    
    if not change_password_and_revoke_sessions(db, username, old_password, new_password):
        logger.error("Incorrect current password")
        return 1
    
    logger.info("Password changed successfully for user '%s' and all sessions revoked.", username)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Run the continuous traffic collection daemon."""
    logger.info("Starting MRTG collector daemon...")
    collector = TrafficCollector()
    stop_event = threading.Event()

    try:
        collector.run(
            interval=args.interval,
            max_iterations=args.iterations,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        logger.info("Collector interrupted by user; shutting down...")
        stop_event.set()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Run the FastAPI web dashboard server."""
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    logger.info("Starting web server on http://%s:%d ...", host, port)
    uvicorn.run("mrtg_cmp.web.app:app", host=host, port=port, reload=False)
    return 0


def cmd_netcare(args: argparse.Namespace) -> int:
    """Run the TelkomCare Netcare scraper daemon."""
    if not settings.netcare_enabled:
        logger.error("Netcare scraper is disabled (NETCARE_ENABLED=false).")
        return 1

    daemon = build_daemon_from_settings(settings)
    interval = args.interval or settings.netcare_poll_interval_seconds
    if interval != daemon.interval_seconds:
        daemon.interval_seconds = max(1, interval)

    logger.info(
        "Starting Netcare scraper daemon (every %ds, %d targets)...",
        daemon.interval_seconds,
        len(daemon.service.targets),
    )

    stop_event = threading.Event()
    daemon.install_signal_handlers(stop_event)
    try:
        daemon.run(stop_event=stop_event, max_rounds=args.iterations)
    except KeyboardInterrupt:
        logger.info("Netcare scraper interrupted by user; shutting down...")
        stop_event.set()
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """Run both collector daemon and web dashboard concurrently."""
    stop_event = threading.Event()
    collector = TrafficCollector()
    threads: list[threading.Thread] = []

    def collector_worker() -> None:
        try:
            collector.run(interval=args.interval, stop_event=stop_event)
        except Exception as exc:
            logger.error("Collector worker encountered error: %s", exc)

    threads.append(
        threading.Thread(target=collector_worker, daemon=True, name="CollectorThread")
    )

    if getattr(args, "with_netcare", False):
        daemon = build_daemon_from_settings(settings)
        daemon.install_signal_handlers(stop_event)
        threads.append(
            threading.Thread(
                target=daemon.run, kwargs={"stop_event": stop_event}, daemon=True,
                name="NetcareDaemonThread",
            )
        )

    for thread in threads:
        thread.start()

    host = args.host or settings.web_host
    port = args.port or settings.web_port
    logger.info("Running unified collector and web service on http://%s:%d", host, port)

    try:
        uvicorn.run("mrtg_cmp.web.app:app", host=host, port=port, reload=False)
    except KeyboardInterrupt:
        logger.info("Shutting down unified services...")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3)
    return 0


def cmd_orbit_sync(args: argparse.Namespace) -> int:
    """Synchronize Telkomsel Orbit modem quotas with MyOrbit portal."""
    from .orbit.cache import OrbitCache
    from .orbit.service import OrbitService
    from .orbit.targets import resolve_orbit_catalog

    catalog_path = getattr(args, "catalog", None) or settings.orbit_catalog_file
    cache_path = getattr(args, "cache_file", None) or (settings.orbit_cache_dir / "modems.json")

    catalog = resolve_orbit_catalog(catalog_path)
    cache = OrbitCache(cache_path)
    logger.info("Starting Orbit modem sync for %d modems...", len(catalog))

    service = OrbitService(headless=not getattr(args, "no_headless", False))
    statuses = service.sync_all(catalog, cache)

    for st in statuses:
        m = st.target
        ssid_display = f" ({m.ssid})" if m.ssid else ""
        if st.error:
            print(f"[{st.error}] {m.phone or m.imei}{ssid_display}: {st.error}")
        else:
            rem_str = f"{st.total_remaining_gb:.2f} GB"
            tot_str = f"{st.total_quota_gb:.2f} GB"
            print(f"[OK] {m.phone or m.imei}{ssid_display}: {rem_str} / {tot_str}")

    logger.info("Orbit sync completed for %d modems.", len(statuses))
    return 0


def cmd_orbit(args: argparse.Namespace) -> int:
    """Run the Telkomsel Orbit modem scraper daemon (24/7 background)."""
    if not settings.orbit_enabled:
        logger.error("Orbit scraper is disabled (ORBIT_ENABLED=false).")
        return 1

    daemon = build_orbit_daemon_from_settings(settings)
    interval = getattr(args, "interval", None)
    if interval is not None:
        daemon.interval_seconds = max(1, interval)

    logger.info(
        "Starting Orbit scraper daemon (every %ds)...",
        daemon.interval_seconds,
    )

    stop_event = threading.Event()
    daemon.install_signal_handlers(stop_event)
    try:
        daemon.run(stop_event=stop_event, max_rounds=getattr(args, "iterations", None))
    except KeyboardInterrupt:
        logger.info("Orbit scraper interrupted by user; shutting down...")
        stop_event.set()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build and return command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="mrtg-cmp",
        description="MRTG Traffic Monitor - MikroTik WAN Traffic Monitoring & Reporting",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize SQLite schema and seed admin")
    p_init.set_defaults(func=cmd_init_db)

    # create-user
    p_user = subparsers.add_parser("create-user", help="Create or update a dashboard user")
    p_user.add_argument("--username", "-u", required=True, help="Username to create or update")
    p_user.add_argument("--password", "-p", help="Password (prompted securely if omitted)")
    p_user.set_defaults(func=cmd_create_user)

    # collect
    p_collect = subparsers.add_parser("collect", help="Run background traffic collector daemon")
    p_collect.add_argument("--interval", "-i", type=int, help="Polling interval in seconds")
    p_collect.add_argument(
        "--iterations", "-n", type=int, help="Maximum polling cycles (for tests)"
    )
    p_collect.set_defaults(func=cmd_collect)

    # web
    p_web = subparsers.add_parser("web", help="Run FastAPI web dashboard")
    p_web.add_argument("--host", help="Binding host IP (default 0.0.0.0)")
    p_web.add_argument("--port", type=int, help="Binding port (default 8000)")
    p_web.set_defaults(func=cmd_web)

    # netcare
    p_netcare = subparsers.add_parser(
        "netcare", help="Run the TelkomCare Netcare branch graph scraper daemon"
    )
    p_netcare.add_argument(
        "--interval",
        "-i",
        type=int,
        help="Seconds between scrape rounds (default: NETCARE_POLL_INTERVAL_SECONDS)",
    )
    p_netcare.add_argument(
        "--iterations", "-n", type=int, help="Maximum scrape rounds (for tests)"
    )
    p_netcare.set_defaults(func=cmd_netcare)

    # all
    p_all = subparsers.add_parser("all", help="Run both collector and web server concurrently")
    p_all.add_argument("--host", help="Binding host IP")
    p_all.add_argument("--port", type=int, help="Binding port")
    p_all.add_argument("--interval", "-i", type=int, help="Polling interval in seconds")
    p_all.add_argument(
        "--with-netcare",
        action="store_true",
        help="Also run the TelkomCare Netcare scraper daemon in a background thread",
    )
    p_all.set_defaults(func=cmd_all)

    # orbit-sync
    p_orbit = subparsers.add_parser(
        "orbit-sync", help="Synchronize Telkomsel Orbit modem quotas with MyOrbit portal"
    )
    p_orbit.add_argument("--catalog", "-c", help="Path to Orbit catalog file (CSV or XLSX)")
    p_orbit.add_argument("--cache-file", help="Path to Orbit JSON cache file")
    p_orbit.add_argument(
        "--no-headless", action="store_true", help="Launch visible browser window"
    )
    p_orbit.set_defaults(func=cmd_orbit_sync)

    # orbit
    p_orbit_daemon = subparsers.add_parser(
        "orbit", help="Run the Telkomsel Orbit modem scraper daemon (24/7 background)"
    )
    p_orbit_daemon.add_argument(
        "--interval",
        "-i",
        type=int,
        help="Seconds between scrape rounds (default: ORBIT_SYNC_INTERVAL_SECONDS)",
    )
    p_orbit_daemon.add_argument(
        "--iterations", "-n", type=int, help="Maximum scrape rounds (for tests)"
    )
    p_orbit_daemon.set_defaults(func=cmd_orbit)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Main execution entrypoint for CLI.

    Logging is configured here, before the subcommand runs, so every entrypoint
    (``init-db``, ``create-user``, ``collect``, ``web``, ``netcare``, ``all``)
    writes the same timestamped lines to ``logs/mrtg-cmp.log`` (FR-16.1).
    """
    configure_logging(settings.log_file, settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
