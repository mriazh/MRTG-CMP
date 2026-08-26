"""Unit tests for the TelkomCare Netcare target catalog and multi-key configuration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mrtg_cmp.config import Settings
from mrtg_cmp.netcare.targets import (
    CATALOG_HEADER,
    DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH,
    DEFAULT_CATALOG_RELATIVE_PATH,
    DEFAULT_TARGETS,
    EXPECTED_REGION_COUNTS,
    EXPECTED_SERVICE_COUNTS,
    REGION_ORDER,
    SERVICE_BADGE_CLASSES,
    SERVICE_DESCRIPTIONS,
    SERVICE_ORDER,
    UNCLASSIFIED_SERVICE,
    NetcareTarget,
    NetcareTargetType,
    default_catalog_path,
    find_target,
    load_catalog,
    parse_service_type,
    region_counts,
    region_label,
    resolve_targets,
    service_badge_class,
    service_counts,
    service_description,
    service_label,
    targets_by_region,
    targets_by_service,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed, anonymous catalog template. A deployment's own
#: ``config/netcare_targets.csv`` is untracked, so this is the only catalog the
#: public repository can assert against.
EXAMPLE_CATALOG = REPO_ROOT / DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH

#: The per-circuit product split (6 Astinet, 5 Metro-E, 7 VPN IP), keyed by the
#: anonymous circuit ids so that neither this test nor the catalog has to name a
#: portal identifier to pin it.
ANONYMOUS_SERVICE_ASSIGNMENT: dict[str, str] = {
    "target-001": "Astinet",
    "target-002": "Astinet",
    "target-003": "Metro-E",
    "target-004": "Metro-E",
    "target-005": "Metro-E",
    "target-006": "VPN IP",
    "target-007": "Astinet",
    "target-008": "Astinet",
    "target-009": "VPN IP",
    "target-010": "VPN IP",
    "target-011": "Astinet",
    "target-012": "Astinet",
    "target-013": "VPN IP",
    "target-014": "Metro-E",
    "target-015": "Metro-E",
    "target-016": "VPN IP",
    "target-017": "VPN IP",
    "target-018": "VPN IP",
}

# --- Catalog shape ---------------------------------------------------------


def test_catalog_holds_exactly_eighteen_active_targets() -> None:
    assert len(DEFAULT_TARGETS) == 18
    assert all(t.ocr_enabled for t in DEFAULT_TARGETS)


def test_catalog_target_ids_are_unique() -> None:
    ids = [t.target for t in DEFAULT_TARGETS]
    assert len(set(ids)) == len(ids)


def test_catalog_only_uses_supported_target_types() -> None:
    assert {t.type for t in DEFAULT_TARGETS} <= {
        NetcareTargetType.SID,
        NetcareTargetType.GRAPH_TITLE,
    }


def test_catalog_includes_both_sid_and_graph_title_modes() -> None:
    types = {t.type for t in DEFAULT_TARGETS}
    assert NetcareTargetType.SID in types
    assert NetcareTargetType.GRAPH_TITLE in types


def test_every_target_has_name_address_and_region_metadata() -> None:
    for target in DEFAULT_TARGETS:
        assert target.name.strip()
        assert target.address.strip()
        assert target.region in REGION_ORDER


def test_every_target_target_id_is_non_empty() -> None:
    for target in DEFAULT_TARGETS:
        assert target.target.strip()


# --- Region grouping -------------------------------------------------------


def test_region_distribution_matches_specification() -> None:
    assert region_counts() == EXPECTED_REGION_COUNTS


def test_region_counts_match_documented_totals() -> None:
    assert EXPECTED_REGION_COUNTS == {
        "CGK": 7,
        "SUB": 2,
        "UPG": 4,
        "DPS": 2,
        "BPN": 1,
        "SENTUL": 2,
    }
    assert sum(EXPECTED_REGION_COUNTS.values()) == 18


def test_targets_by_region_covers_every_target_exactly_once() -> None:
    grouped = targets_by_region()
    assert [code for code, _ in grouped] == list(REGION_ORDER)
    flattened = [t.target for _, targets in grouped for t in targets]
    assert sorted(flattened) == sorted(t.target for t in DEFAULT_TARGETS)


def test_region_labels_are_human_readable() -> None:
    assert region_label("CGK") == "CGK Area"
    assert region_label("SUB") == "Surabaya"
    assert region_label("UPG") == "Makassar"
    assert region_label("DPS") == "Denpasar"
    assert region_label("BPN") == "Balikpapan"
    assert region_label("SENTUL") == "Sentul VPN"


def test_unknown_region_label_falls_back_to_code() -> None:
    assert region_label("ZZZ") == "ZZZ"


# --- Lookup ----------------------------------------------------------------


def test_find_target_resolves_known_target() -> None:
    found = find_target(DEFAULT_TARGETS[0].target)
    assert found is not None
    assert found.target == DEFAULT_TARGETS[0].target


def test_find_target_returns_none_for_unknown_target() -> None:
    assert find_target("does-not-exist") is None


# --- CSV catalog override --------------------------------------------------


def _write_catalog(path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> Path:
    lines = ["type,target,name,address,region,ocr_enabled"]
    for row in rows:
        lines.append(",".join(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_catalog_reads_all_rows(tmp_path: Path) -> None:
    catalog = _write_catalog(
        tmp_path / "catalog.csv",
        [
            ("SID", "111-1", "Branch One", "Addr One", "CGK", "true"),
            ("Graph-title", "222", "Branch Two", "Addr Two", "SUB", "true"),
            ("SID", "333-1", "Branch Three", "Addr Three", "UPG", "false"),
        ],
    )
    loaded = load_catalog(catalog)
    assert len(loaded) == 3
    assert loaded[0].type is NetcareTargetType.SID
    assert loaded[1].type is NetcareTargetType.GRAPH_TITLE
    assert loaded[0].name == "Branch One"
    assert loaded[2].region == "UPG"


def test_load_catalog_filters_ocr_disabled_rows(tmp_path: Path) -> None:
    catalog = _write_catalog(
        tmp_path / "catalog.csv",
        [
            ("SID", "111-1", "Branch One", "Addr One", "CGK", "true"),
            ("SID", "333-1", "Branch Three", "Addr Three", "UPG", "false"),
        ],
    )
    active = resolve_targets(catalog)
    assert [t.target for t in active] == ["111-1"]


def test_load_catalog_skips_blank_and_malformed_rows(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "type,target,name,address,region,ocr_enabled\n"
        "\n"
        "SID,,Missing Target,,CGK,true\n"
        "NotAType,999,Bad Type,Addr,CGK,true\n"
        "SID,111-1,Good,Addr,CGK,true\n",
        encoding="utf-8",
    )
    loaded = load_catalog(catalog)
    assert [t.target for t in loaded] == ["111-1"]


def test_load_catalog_raises_when_file_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "nope.csv")


def test_resolve_targets_falls_back_to_builtin_catalog(tmp_path: Path) -> None:
    """A catalog that cannot be read must land on the built-in list, not raise."""

    missing = tmp_path / "config" / "netcare_targets.csv"

    assert [t.target for t in resolve_targets(missing)] == [t.target for t in DEFAULT_TARGETS]


def test_resolve_targets_prefers_the_deployment_catalog(tmp_path: Path) -> None:
    """A deployment's own catalog wins, which is how the gitignored file is used."""

    catalog = _write_catalog(
        tmp_path / "netcare_targets.csv",
        [("SID", "own-circuit-1", "Own Branch", "Own Address", "CGK", "true")],
    )

    assert [t.target for t in resolve_targets(catalog)] == ["own-circuit-1"]


def test_resolve_targets_ignores_the_builtin_list_when_an_override_exists(
    tmp_path: Path,
) -> None:
    """The override must be the catalog in effect, not merely readable."""

    catalog = _write_catalog(
        tmp_path / "netcare_targets.csv",
        [("SID", "own-circuit-1", "Own Branch", "Own Address", "CGK", "true")],
    )

    assert [t.target for t in resolve_targets(catalog)] != [t.target for t in DEFAULT_TARGETS]


# --- Catalog discovery and the committed example ---------------------------


def test_default_catalog_path_finds_a_catalog_in_an_ancestor_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-discovery walks up from the cwd, so a subdirectory run still finds it."""

    catalog = tmp_path / "config" / "netcare_targets.csv"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("type,target,name,address,region,ocr_enabled\n", encoding="utf-8")
    nested = tmp_path / "service" / "sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert default_catalog_path() == catalog.resolve()


def test_discovered_catalog_is_never_the_committed_example() -> None:
    """The example documents the format; it must not stand in for a real catalog.

    Returning it from discovery would silently run a deployment against the
    anonymous circuits instead of its own, so a checkout that has both files has
    to resolve to the gitignored one.
    """

    discovered = default_catalog_path()
    if discovered is None:  # a fresh clone ships the example only
        return

    assert discovered.name == DEFAULT_CATALOG_RELATIVE_PATH.name
    assert discovered.name != DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH.name


def test_example_catalog_is_the_only_committed_catalog() -> None:
    """A clone ships the anonymous template; the real one is gitignored."""

    assert EXAMPLE_CATALOG.is_file()
    assert DEFAULT_CATALOG_RELATIVE_PATH != DEFAULT_CATALOG_EXAMPLE_RELATIVE_PATH


def test_example_catalog_declares_the_documented_header() -> None:
    header = EXAMPLE_CATALOG.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == ",".join(CATALOG_HEADER)


def test_example_catalog_holds_eighteen_anonymous_circuits() -> None:
    loaded = load_catalog(EXAMPLE_CATALOG)
    assert [t.target for t in loaded] == [f"target-{index:03d}" for index in range(1, 19)]
    assert all(t.ocr_enabled for t in loaded)


def test_example_catalog_uses_generic_branch_metadata() -> None:
    """The template documents the format, so it names no branch and no address."""

    for target in load_catalog(EXAMPLE_CATALOG):
        assert target.name != target.target
        assert re.fullmatch(rf"Branch Office {target.region} \d+", target.name), target.name
        assert target.address.strip()
        assert "Link" not in target.name


def test_example_catalog_matches_the_builtin_catalog() -> None:
    """Both anonymous catalogs must agree on lookup mode, region, and product.

    They differ only in presentation (the template spells out its rows, the
    built-in list derives names from the region buckets); anything that changes a
    link's type, region, or service has to be made in both places.
    """

    def classify(targets: list[NetcareTarget]) -> dict[str, tuple[NetcareTargetType, str, str]]:
        return {t.target: (t.type, t.region, t.service_type) for t in targets}

    assert classify(load_catalog(EXAMPLE_CATALOG)) == classify(list(DEFAULT_TARGETS))


def test_example_catalog_preserves_the_specified_region_distribution() -> None:
    assert region_counts(load_catalog(EXAMPLE_CATALOG)) == EXPECTED_REGION_COUNTS


def test_example_catalog_respects_type_modes() -> None:
    loaded = load_catalog(EXAMPLE_CATALOG)
    assert {t.type for t in loaded[:11]} == {NetcareTargetType.SID}
    assert {t.type for t in loaded[11:]} == {NetcareTargetType.GRAPH_TITLE}


def test_resolve_targets_skips_unreadable_override(tmp_path: Path) -> None:
    broken = tmp_path / "broken.csv"
    broken.write_text("not,a,valid,header,row,here\n", encoding="utf-8")
    assert len(resolve_targets(broken)) == 18


# --- Service type classification (Task 30.1) --------------------------------


def test_catalog_header_declares_a_service_type_column() -> None:
    assert CATALOG_HEADER[-1] == "service_type"
    assert "service_type" in CATALOG_HEADER


def test_every_target_carries_a_known_service_type() -> None:
    for target in DEFAULT_TARGETS:
        assert target.service_type in SERVICE_ORDER, target.target


def test_service_assignment_matches_the_anonymous_catalog() -> None:
    assert {t.target: t.service_type for t in DEFAULT_TARGETS} == ANONYMOUS_SERVICE_ASSIGNMENT


def test_example_catalog_service_assignment_matches_the_anonymous_catalog() -> None:
    assert {t.target: t.service_type for t in load_catalog(EXAMPLE_CATALOG)} == (
        ANONYMOUS_SERVICE_ASSIGNMENT
    )


def test_builtin_catalog_agrees_with_the_example_catalog() -> None:
    builtin = {t.target: t.service_type for t in DEFAULT_TARGETS}
    example = {t.target: t.service_type for t in load_catalog(EXAMPLE_CATALOG)}
    assert builtin == example


def test_service_distribution_matches_specification() -> None:
    assert service_counts(resolve_targets()) == EXPECTED_SERVICE_COUNTS
    assert EXPECTED_SERVICE_COUNTS == {"Astinet": 6, "Metro-E": 5, "VPN IP": 7}
    assert sum(EXPECTED_SERVICE_COUNTS.values()) == 18


def test_service_counts_cover_every_active_target_exactly_once() -> None:
    counts = service_counts(resolve_targets())
    assert sum(counts.values()) == len(resolve_targets())
    assert UNCLASSIFIED_SERVICE not in counts


def test_service_counts_ignore_ocr_disabled_targets() -> None:
    targets = [
        NetcareTarget("a", NetcareTargetType.SID, "A", "Addr", "CGK", True, "Astinet"),
        NetcareTarget("b", NetcareTargetType.SID, "B", "Addr", "CGK", False, "Astinet"),
    ]
    assert service_counts(targets) == {"Astinet": 1, "Metro-E": 0, "VPN IP": 0}


def test_targets_by_service_preserves_service_order() -> None:
    grouped = targets_by_service(resolve_targets())
    assert [code for code, _ in grouped] == list(SERVICE_ORDER)
    # Every target lands in exactly one bucket: grouping must not drop or
    # duplicate a link, which is what would make a pill count disagree with the
    # cards behind it.
    flattened = [t.target for _, members in grouped for t in members]
    assert sorted(flattened) == sorted(t.target for t in resolve_targets())


def test_targets_by_service_appends_unclassified_targets_last() -> None:
    targets = [
        NetcareTarget("a", NetcareTargetType.SID, "A", "Addr", "CGK", True, UNCLASSIFIED_SERVICE),
        NetcareTarget("b", NetcareTargetType.SID, "B", "Addr", "CGK", True, "Astinet"),
    ]
    grouped = dict(targets_by_service(targets))
    assert [t.target for t in grouped["Astinet"]] == ["b"]
    assert [t.target for t in grouped[UNCLASSIFIED_SERVICE]] == ["a"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Astinet", "Astinet"),
        ("astinet", "Astinet"),
        ("  ASTINET  ", "Astinet"),
        ("Metro-E", "Metro-E"),
        ("metro e", "Metro-E"),
        ("metro_e", "Metro-E"),
        ("METROE", "Metro-E"),
        ("VPN IP", "VPN IP"),
        ("vpnip", "VPN IP"),
        ("vpn-ip", "VPN IP"),
    ],
)
def test_parse_service_type_normalizes_case_and_separators(raw: str, expected: str) -> None:
    assert parse_service_type(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "Leased Line", "unknown", "Astinet X"])
def test_parse_service_type_rejects_unknown_values(raw: str) -> None:
    assert parse_service_type(raw) is None


def test_service_label_and_badge_class_cover_every_service() -> None:
    for code in SERVICE_ORDER:
        assert service_label(code) == code
        assert service_badge_class(code) == SERVICE_BADGE_CLASSES[code]
    # The three products must be told apart by colour, or the legend is noise.
    per_service = {SERVICE_BADGE_CLASSES[code] for code in SERVICE_ORDER}
    assert len(per_service) == len(SERVICE_ORDER)


def test_service_label_and_badge_class_fall_back_for_unclassified() -> None:
    assert service_label(UNCLASSIFIED_SERVICE) == "Unclassified"
    assert service_badge_class(UNCLASSIFIED_SERVICE) == SERVICE_BADGE_CLASSES[UNCLASSIFIED_SERVICE]
    assert service_badge_class("not-a-service") == SERVICE_BADGE_CLASSES[UNCLASSIFIED_SERVICE]


def test_every_service_explains_itself_in_the_legend() -> None:
    """The filter legend is only useful if each service says what it carries."""

    assert SERVICE_DESCRIPTIONS["Astinet"] == "Internet Dedicated"
    assert SERVICE_DESCRIPTIONS["Metro-E"] == "Ethernet L2"
    assert SERVICE_DESCRIPTIONS["VPN IP"] == "Intranet"
    for code in SERVICE_ORDER:
        assert service_description(code) == SERVICE_DESCRIPTIONS[code]
    assert service_description("not-a-service") == ""


def test_load_catalog_reads_the_service_type_column(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "type,target,name,address,region,ocr_enabled,service_type\n"
        "SID,111-1,Branch One,Addr One,CGK,true,metro e\n"
        "Graph-title,222,Branch Two,Addr Two,SUB,true,VPNIP\n"
        "SID,333-1,Branch Three,Addr Three,UPG,true,\n",
        encoding="utf-8",
    )

    loaded = load_catalog(catalog)

    assert [t.service_type for t in loaded] == ["Metro-E", "VPN IP", UNCLASSIFIED_SERVICE]


def test_load_catalog_treats_an_unknown_service_as_unclassified(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "type,target,name,address,region,ocr_enabled,service_type\n"
        "SID,111-1,Branch One,Addr One,CGK,true,Leased Line\n",
        encoding="utf-8",
    )

    assert load_catalog(catalog)[0].service_type == UNCLASSIFIED_SERVICE


def test_load_catalog_without_a_service_column_still_loads_every_row(tmp_path: Path) -> None:
    """A pre-Task-30 export must keep working rather than being rejected."""

    catalog = _write_catalog(
        tmp_path / "catalog.csv",
        [("SID", "111-1", "Branch One", "Addr One", "CGK", "true")],
    )

    loaded = load_catalog(catalog)

    assert len(loaded) == 1
    assert loaded[0].service_type == UNCLASSIFIED_SERVICE


def test_netcare_target_as_dict_exposes_service_metadata() -> None:
    payload = NetcareTarget(
        "target-001",
        NetcareTargetType.SID,
        "Branch",
        "Addr",
        "CGK",
        True,
        "Astinet",
    ).as_dict()

    assert payload["service_type"] == "Astinet"
    assert payload["service_label"] == "Astinet"
    assert payload["service_description"] == "Internet Dedicated"
    assert payload["service_badge_class"] == SERVICE_BADGE_CLASSES["Astinet"]


def test_netcare_target_service_defaults_to_unclassified() -> None:
    target = NetcareTarget("111-1", NetcareTargetType.SID, "Branch", "Addr", "CGK")
    assert target.service_type == UNCLASSIFIED_SERVICE
    assert target.as_dict()["service_type"] == UNCLASSIFIED_SERVICE


# --- Settings parsing ------------------------------------------------------


def test_settings_parse_comma_separated_gemini_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "key-one, key-two ,key-three")
    settings = Settings(_env_file=None)
    assert settings.gemini_api_keys == ["key-one", "key-two", "key-three"]


def test_settings_parse_comma_separated_gemini_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-1.5-flash")
    settings = Settings(_env_file=None)
    assert settings.gemini_models == ["gemini-2.5-flash", "gemini-1.5-flash"]


def test_settings_default_gemini_model_chain() -> None:
    settings = Settings(_env_file=None)
    assert settings.gemini_models == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ]


def test_settings_empty_gemini_keys_become_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", " , ,")
    settings = Settings(_env_file=None)
    assert settings.gemini_api_keys == []


def test_settings_netcare_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.netcare_enabled is True
    assert settings.netcare_poll_interval_seconds == 300
    assert settings.netcare_cache_dir == Path("data/netcare_cache")
    assert settings.netcare_base_url.startswith("https://")


def test_settings_dashboard_refresh_is_three_minutes() -> None:
    settings = Settings(_env_file=None)
    assert settings.dashboard_refresh_seconds == 180


def test_settings_credentials_default_to_empty() -> None:
    settings = Settings(_env_file=None)
    assert settings.telkom_user == ""
    assert settings.telkom_password == ""
    assert settings.totp_secret == ""


def test_settings_credentials_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELKOM_USER", "user1")
    monkeypatch.setenv("TELKOM_PASSWORD", "secret1")
    monkeypatch.setenv("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    settings = Settings(_env_file=None)
    assert settings.telkom_user == "user1"
    assert settings.telkom_password == "secret1"
    assert settings.totp_secret == "JBSWY3DPEHPK3PXP"


def test_settings_can_disable_netcare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETCARE_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.netcare_enabled is False


def test_netcare_target_is_hashable_and_frozen() -> None:
    target = NetcareTarget(
        target="1-1",
        type=NetcareTargetType.SID,
        name="A",
        address="B",
        region="CGK",
    )
    assert {target}
    with pytest.raises(AttributeError):
        target.name = "changed"  # type: ignore[misc]
