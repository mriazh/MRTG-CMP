"""Unit tests for command-line interface (CLI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mrtg_cmp.auth import verify_password
from mrtg_cmp.cli import build_parser, main
from mrtg_cmp.config import settings
from mrtg_cmp.db import Database


def test_cli_parser_commands() -> None:
    """Ensure argument parser recognizes subcommands and options."""
    parser = build_parser()

    # init-db
    args_init = parser.parse_args(["init-db"])
    assert args_init.command == "init-db"

    # create-user
    args_user = parser.parse_args(["create-user", "-u", "engineer", "-p", "secret123"])
    assert args_user.command == "create-user"
    assert args_user.username == "engineer"
    assert args_user.password == "secret123"

    # collect
    args_col = parser.parse_args(["collect", "-i", "60", "-n", "5"])
    assert args_col.command == "collect"
    assert args_col.interval == 60
    assert args_col.iterations == 5

    # web
    args_web = parser.parse_args(["web", "--host", "127.0.0.1", "--port", "9000"])
    assert args_web.command == "web"
    assert args_web.host == "127.0.0.1"
    assert args_web.port == 9000


def test_cli_parser_netcare_command() -> None:
    """Ensure the netcare subcommand exposes interval and iteration options."""
    parser = build_parser()

    args = parser.parse_args(["netcare", "-i", "120", "-n", "2"])
    assert args.command == "netcare"
    assert args.interval == 120
    assert args.iterations == 2

    default_args = parser.parse_args(["netcare"])
    assert default_args.interval is None
    assert default_args.iterations is None


def test_cli_parser_all_supports_netcare_flag() -> None:
    """Ensure `all` can optionally manage the Netcare scraper too."""
    parser = build_parser()

    args = parser.parse_args(["all", "--with-netcare"])
    assert args.with_netcare is True

    assert parser.parse_args(["all"]).with_netcare is False


def test_cli_netcare_runs_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`netcare` builds a daemon from settings and runs one bounded round."""
    from mrtg_cmp import cli as cli_module

    cache_dir = tmp_path / "netcare_cache"
    monkeypatch.setattr(cli_module.settings, "netcare_cache_dir", cache_dir)
    monkeypatch.setattr(cli_module.settings, "netcare_enabled", True)

    rounds: list[int] = []
    observed: dict[str, int] = {}

    class StubService:
        targets = ("t1", "t2")

    class StubDaemon:
        def __init__(self, interval_seconds: int = 300) -> None:
            self.interval_seconds = interval_seconds
            self.service = StubService()

        def install_signal_handlers(self, stop_event: object) -> None:
            return None

        def run(self, stop_event: object = None, max_rounds: int | None = None) -> int:
            observed["interval"] = self.interval_seconds
            observed["max_rounds"] = max_rounds
            rounds.append(1)
            return 1

    monkeypatch.setattr(
        cli_module, "build_daemon_from_settings", lambda cfg=None: StubDaemon(300)
    )

    assert cli_module.main(["netcare", "-i", "120", "-n", "1"]) == 0
    assert len(rounds) == 1
    assert observed["interval"] == 120
    assert observed["max_rounds"] == 1


def test_cli_netcare_reports_disabled_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`netcare` refuses to start when the scraper is disabled by configuration."""
    from mrtg_cmp import cli as cli_module

    monkeypatch.setattr(cli_module.settings, "netcare_enabled", False)
    assert cli_module.main(["netcare"]) == 1


def test_cli_all_starts_netcare_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`all --with-netcare` launches the scraper daemon in a background thread."""
    from mrtg_cmp import cli as cli_module

    started: list[str] = []

    class StubDaemon:
        def install_signal_handlers(self, stop_event: object) -> None:
            return None

        def run(self, stop_event: object = None, max_rounds: int | None = None) -> int:
            started.append("netcare")
            return 0

    class StubCollector:
        def run(self, **kwargs: object) -> None:
            started.append("collector")
            return None

    monkeypatch.setattr(cli_module, "build_daemon_from_settings", lambda cfg=None: StubDaemon())
    monkeypatch.setattr(cli_module, "TrafficCollector", StubCollector)

    def fake_uvicorn_run(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli_module.uvicorn, "run", fake_uvicorn_run)

    assert cli_module.main(["all", "--with-netcare"]) == 0
    assert "netcare" in started
    assert "collector" in started


def test_cli_init_db_and_create_user(tmp_path: Path) -> None:
    """Test CLI execution of init-db and create-user commands."""
    db_path = tmp_path / "cli_test.db"
    settings.database_path = db_path

    # Run init-db
    ret_init = main(["init-db"])
    assert ret_init == 0

    db = Database(db_path)
    admin = db.get_user("admin")
    assert admin is not None

    # Create new operator user
    ret_user = main(["create-user", "-u", "operator", "-p", "OperatorPass1!"])
    assert ret_user == 0
    op = db.get_user("operator")
    assert op is not None
    assert verify_password("OperatorPass1!", op["password_hash"]) is True

    # Update operator password
    ret_update = main(["create-user", "-u", "operator", "-p", "NewPass2!"])
    assert ret_update == 0
    op_updated = db.get_user("operator")
    assert op_updated is not None
    assert verify_password("NewPass2!", op_updated["password_hash"]) is True
