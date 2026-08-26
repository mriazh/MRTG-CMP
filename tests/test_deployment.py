"""Tests for the deployment contract: systemd units, deploy.sh, and packaging."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "systemd"
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"
EXAMPLE_CATALOG = REPO_ROOT / "config" / "netcare_targets.csv.example"
LIVE_CATALOG_RELATIVE = "config/netcare_targets.csv"
EXAMPLE_CATALOG_RELATIVE = "config/netcare_targets.csv.example"

EXPECTED_UNITS = (
    "mrtg-cmp-web.service",
    "mrtg-cmp-collector.service",
    "mrtg-cmp-netcare.service",
)

LEGACY_UNITS = (
    "mrtg-poncab.service",
    "mrtg-poncab-web.service",
    "mrtg-poncab-collector.service",
)

#: Corporate entity and facility identifiers that must not survive in a file the
#: public repository tracks. Assembled from fragments so this file does not
#: become the leak it is looking for.
FORBIDDEN_TERMS = tuple(
    "".join(parts)
    for parts in (
        ("gm", "f"),
        ("aer", "oasia"),
        ("gar", "uda"),
        ("pon", "dok cabe"),
        ("sep", "ingan"),
        ("soe", "tta"),
        ("ngu", "rah rai"),
        ("sukarno", " hatta"),
        ("juan", "da"),
    )
)

#: TelkomCare portal identifiers follow two shapes: a 7-digit prefix, a hyphen,
#: and 10 digits. Transcribing the digits literally would publish them here.
FORBIDDEN_PORTAL_ID = re.compile(r"\b\d{7}-\d{10}\b")


def _tracked_files() -> set[str]:
    """Return the repository's tracked paths, or skip when git is unavailable."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git ls-files is unavailable here: {exc}")

    return {name for name in result.stdout.decode("utf-8", "replace").split("\0") if name}


def _unit_basenames() -> tuple[str, ...]:
    """Unit names without the .service suffix, as deploy.sh declares them."""

    return tuple(u.removesuffix(".service") for u in EXPECTED_UNITS)


def _legacy_basenames() -> tuple[str, ...]:
    """Legacy unit names without the .service suffix."""

    return tuple(u.removesuffix(".service") for u in LEGACY_UNITS)


def _unit(name: str) -> Path:
    return SYSTEMD_DIR / name


# --- Systemd units ---------------------------------------------------------


@pytest.mark.parametrize("unit", EXPECTED_UNITS)
def test_expected_service_units_exist(unit: str) -> None:
    """FR-14.1: the mrtg-cmp-* units are shipped."""
    assert _unit(unit).is_file()


def test_legacy_units_are_removed() -> None:
    """FR-14.2: the old mrtg-poncab-* units no longer ship."""
    for unit in LEGACY_UNITS:
        assert not _unit(unit).exists(), f"legacy unit still present: {unit}"


@pytest.mark.parametrize("unit", EXPECTED_UNITS)
def test_unit_is_valid_systemd_syntax(unit: str) -> None:
    """Every unit declares the sections systemd requires."""
    text = _unit(unit).read_text(encoding="utf-8")
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, f"{unit} is missing {section}"
    assert "ExecStart=" in text
    assert "WantedBy=multi-user.target" in text


@pytest.mark.parametrize("unit", EXPECTED_UNITS)
def test_unit_uses_a_rendered_app_dir_placeholder(unit: str) -> None:
    """Units carry @APP_DIR@ so deploy.sh can install them at any path."""
    text = _unit(unit).read_text(encoding="utf-8")
    assert "@APP_DIR@" in text
    # No operator's home directory may be baked into a shipped unit, and the
    # check is generic: naming one here to assert its absence would publish it.
    assert not re.search(r"/home/[A-Za-z]", text)


@pytest.mark.parametrize("unit", EXPECTED_UNITS)
def test_unit_is_sandboxed(unit: str) -> None:
    """Units keep the existing security hardening."""
    text = _unit(unit).read_text(encoding="utf-8")
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text


def test_collector_unit_runs_the_collect_command() -> None:
    """The collector unit starts the MikroTik polling daemon."""
    text = _unit("mrtg-cmp-collector.service").read_text(encoding="utf-8")
    assert "-m mrtg_cmp collect" in text


def test_web_unit_runs_the_web_command() -> None:
    """The web unit starts the FastAPI dashboard."""
    text = _unit("mrtg-cmp-web.service").read_text(encoding="utf-8")
    assert "-m mrtg_cmp web" in text


def test_netcare_unit_runs_the_netcare_command() -> None:
    """FR-14.1: the Netcare scraper has its own unit."""
    text = _unit("mrtg-cmp-netcare.service").read_text(encoding="utf-8")
    assert "-m mrtg_cmp netcare" in text


def test_netcare_cache_is_not_committed() -> None:
    """NFR-4: the graph cache, status manifest, and session cookies stay untracked."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/netcare_cache/" in gitignore


def test_netcare_unit_writes_the_cache_directory() -> None:
    """The scraper needs write access to its cache and browser profile."""
    text = _unit("mrtg-cmp-netcare.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=" in text
    assert "netcare_cache" in text


def test_netcare_unit_supports_browser_automation() -> None:
    """Headless Chrome needs no sandbox tweaks beyond the shared hardening."""
    text = _unit("mrtg-cmp-netcare.service").read_text(encoding="utf-8")
    assert "PrivateTmp=" in text


# --- deploy.sh -------------------------------------------------------------


def test_deploy_script_exists() -> None:
    assert DEPLOY_SCRIPT.is_file()


def test_deploy_script_disables_legacy_units() -> None:
    """FR-14.2: the deploy script retires the legacy mrtg-poncab-* units."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    for unit in _legacy_basenames():
        assert unit in text, f"deploy.sh does not handle legacy unit {unit}"
    assert "disable" in text
    assert "systemctl" in text


def test_deploy_script_starts_every_new_unit() -> None:
    """FR-14.2: the deploy script enables and restarts all three mrtg-cmp-* units."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    for unit in _unit_basenames():
        assert unit in text, f"deploy.sh does not handle new unit {unit}"


def test_deploy_script_renders_app_dir_placeholder() -> None:
    """Units ship with @APP_DIR@; the script substitutes the real path on install."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "@APP_DIR@" in text
    assert "sed" in text


def test_deploy_script_reloads_systemd_daemon() -> None:
    """A daemon-reload is required before the new units are picked up."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "daemon-reload" in text


def test_deploy_script_installs_units_from_systemd_dir() -> None:
    """Units are copied out of the repository's systemd/ directory."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "systemd" in text


# --- Packaging -------------------------------------------------------------


def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_dependencies_declare_the_scraper_runtime_needs() -> None:
    """The scraper needs Selenium, TOTP, and Pillow declared as dependencies."""
    project = _pyproject()
    dependencies = project["project"]["dependencies"]
    assert isinstance(dependencies, list)
    joined = " ".join(str(d) for d in dependencies)
    assert "selenium" in joined
    assert "pyotp" in joined
    assert "pillow" in joined.lower()


def test_netcare_package_is_included_in_the_distribution() -> None:
    """setuptools package discovery must pick up the netcare subpackage."""
    project = _pyproject()
    packages = project["tool"]["setuptools"]["packages"]["find"]
    assert packages["where"] == ["src"]


def test_env_example_documents_the_netcare_settings() -> None:
    """Operators need the Netcare variables documented in .env.example."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "NETCARE_ENABLED",
        "NETCARE_BASE_URL",
        "NETCARE_POLL_INTERVAL_SECONDS",
        "NETCARE_CACHE_DIR",
        "GEMINI_API_KEYS",
        "GEMINI_MODELS",
        "TELKOM_USER",
        "TELKOM_PASSWORD",
        "TOTP_SECRET",
        "DASHBOARD_REFRESH_SECONDS",
    ):
        assert re.search(rf"^{key}=", text, re.MULTILINE), f"missing {key} in .env.example"


def test_env_example_documents_the_logging_settings() -> None:
    """FR-16.1: the operator has to be able to find and change the log path."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("LOG_FILE", "LOG_LEVEL"):
        assert re.search(rf"^{key}=", text, re.MULTILINE), f"missing {key} in .env.example"


def test_env_example_ships_only_valid_gemini_models() -> None:
    """FR-16.3: an invented model name stalls the login until it is blacklisted."""

    from mrtg_cmp.netcare.captcha import VALID_GEMINI_VISION_MODELS

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r'^GEMINI_MODELS="([^"]*)"', text, re.MULTILINE)
    assert match, "GEMINI_MODELS must be documented in .env.example"
    configured = [item.strip() for item in match.group(1).split(",") if item.strip()]

    assert configured
    unknown = [model for model in configured if model not in VALID_GEMINI_VISION_MODELS]
    assert not unknown, f".env.example ships unknown Gemini models: {unknown}"


def test_env_example_orders_gemini_models_by_quota_high_first() -> None:
    """A fresh clone must not open on a metered model and stall on its rate limit.

    The two 500 RPD / 15 RPM ``-flash-lite`` models have to lead the shipped
    chain, and a metered full-size model has to sit behind both of them.
    """

    high_quota = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
    metered = ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash")

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r'^GEMINI_MODELS="([^"]*)"', text, re.MULTILINE)
    assert match, "GEMINI_MODELS must be documented in .env.example"
    configured = [item.strip() for item in match.group(1).split(",") if item.strip()]

    for model in (*high_quota, *metered):
        assert model in configured, f".env.example must offer {model}"
    assert configured.index(metered[0]) > max(configured.index(m) for m in high_quota)


def test_shipped_env_orders_gemini_models_by_quota_high_first() -> None:
    """The deployed .env carries the same high-quota-first order as the example."""

    high_quota = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
    metered = ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash")

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():  # a fresh clone ships the example only
        pytest.skip(".env is not present in this checkout")

    text = env_file.read_text(encoding="utf-8")
    match = re.search(r'^GEMINI_MODELS="([^"]*)"', text, re.MULTILINE)
    assert match, "GEMINI_MODELS must be set in .env"
    configured = [item.strip() for item in match.group(1).split(",") if item.strip()]

    for model in (*high_quota, *metered):
        assert model in configured, f".env must offer {model}"
    assert configured.index(metered[0]) > max(configured.index(m) for m in high_quota)


def _env_site_name(path: Path) -> str:
    match = re.search(r'^SITE_NAME="([^"]*)"', path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"SITE_NAME must be set in {path.name}"
    return match.group(1)


# --- OpSec sanitization gate (Task 32) ---------------------------------------


def test_env_example_brands_the_site_neutrally() -> None:
    """The shipped example is public, so it must not name a single customer site."""

    assert _env_site_name(REPO_ROOT / ".env.example") == "Enterprise Gateway"


def test_env_example_ships_no_secret_values() -> None:
    """Every credential in the public example has to be empty, not illustrative."""

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "ROUTEROS_PASSWORD",
        "ADMIN_PASSWORD",
        "TUNNEL_WEB_EMAIL",
        "TUNNEL_WEB_PASSWORD",
        "TUNNEL_WEB_SERVICE_ID",
        "WA_DEVICE_ID",
        "WA_TARGET_JID",
        "GEMINI_API_KEYS",
        "TELKOM_USER",
        "TELKOM_PASSWORD",
        "TOTP_SECRET",
    ):
        match = re.search(rf'^{key}="([^"]*)"', text, re.MULTILINE)
        assert match, f"{key} must be documented in .env.example"
        assert not match.group(1).strip(), f"{key} ships a value in .env.example"

    # The session key cannot be empty, or every fresh install shares one signing
    # key, so the example carries a placeholder the operator has to replace.
    secret = re.search(r'^SECRET_KEY="([^"]*)"', text, re.MULTILINE)
    assert secret, "SECRET_KEY must be documented in .env.example"
    assert "change-this" in secret.group(1), "SECRET_KEY must ship as an obvious placeholder"


def test_shipped_env_sets_its_own_site_name() -> None:
    """Branding belongs to the deployment's private .env, not to the public example.

    The value is deliberately not asserted: an operator is free to brand the
    dashboard however they like, and that file is gitignored so it is never part
    of the published tree. What must hold is that they set one at all.
    """

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():  # a fresh clone ships the example only
        pytest.skip(".env is not present in this checkout")

    assert _env_site_name(env_file).strip()


def test_live_netcare_catalog_is_gitignored() -> None:
    """The deployment's own circuit ids and addresses must never be committed."""

    ignored = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert f"/{LIVE_CATALOG_RELATIVE}" in ignored
    # Anchored, so the tracked template beside it is still committable.
    assert LIVE_CATALOG_RELATIVE not in ignored
    assert EXAMPLE_CATALOG_RELATIVE not in ignored


def test_live_netcare_catalog_is_not_tracked() -> None:
    """Git must not be holding the catalog, which the ignore rule cannot undo alone."""

    assert LIVE_CATALOG_RELATIVE not in _tracked_files()


def test_example_netcare_catalog_is_tracked() -> None:
    """A clone needs the template, so it has to be a tracked file."""

    assert EXAMPLE_CATALOG.is_file()
    assert EXAMPLE_CATALOG_RELATIVE in _tracked_files()


def test_example_netcare_catalog_holds_eighteen_anonymous_circuits() -> None:
    """The template must load, and it must name nothing but anonymous circuits."""

    from mrtg_cmp.netcare.targets import CATALOG_HEADER, load_catalog

    assert EXAMPLE_CATALOG.read_text(encoding="utf-8-sig").splitlines()[0] == ",".join(
        CATALOG_HEADER
    )

    loaded = load_catalog(EXAMPLE_CATALOG)
    assert [t.target for t in loaded] == [f"target-{index:03d}" for index in range(1, 19)]
    assert all(t.ocr_enabled for t in loaded)


def test_deploy_script_seeds_a_missing_catalog_from_the_example() -> None:
    """A fresh host has to end up with a catalog, or the dashboard renders nothing."""

    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "netcare_targets.csv.example" in text
    assert "config/netcare_targets.csv" in text or "netcare_targets.csv" in text
    assert "cp " in text or "install " in text


def test_tracked_files_leak_no_customer_identifiers() -> None:
    """FR-23.1: no corporate name, facility, or portal id in the published tree.

    This is the gate the whole phase exists for, so it is checked against git's
    view of the repository rather than against a hand-picked file list: a leak
    anywhere in a tracked file fails, including one added after this test was
    written.
    """

    tracked = _tracked_files()
    assert tracked, "git reported no tracked files"

    for name in sorted(tracked):
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue  # a binary or non-UTF-8 asset cannot carry these strings

        for term in FORBIDDEN_TERMS:
            assert term not in text, f"{name} leaks {term!r}"
        assert not FORBIDDEN_PORTAL_ID.search(text), f"{name} leaks a portal circuit id"


def test_log_file_is_not_committed() -> None:
    """Runtime logs stay out of git; the file is regenerated on every start."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip() in {"*.log", "logs/"} for line in ignored)
