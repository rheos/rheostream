"""The settings registry, the identity check, precedence, and the deployment layer.

Seams: ``rheo_core.settings.resolve()`` (three-source precedence; undeclared keys
refused at every source; env coercion for the four value types, the uppercased
variant and the ``RHEO_PROFILE`` special case), the registry/``defaults.toml``
identity check, and the harness registration gate. The floor comparators and the
write path are in ``test_settings_floor.py``.

No test here builds a ``WorkspaceContext``: ``resolve`` takes ids, not a context.
"""

import logging
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from harness.settings_keys import (
    HARNESS_EXPLICIT,
    HARNESS_FLOOR_AND,
    HARNESS_FLOOR_MIN,
    HARNESS_FLOOR_UNION,
    HARNESS_MEMBER,
    register_harness_keys,
)
from rheo_core.settings import (
    CORE_ORIGIN,
    PACKAGE_DEFAULTS,
    REGISTRY,
    TEST_HARNESS_ORIGIN,
    Floor,
    KeySpec,
    Scope,
    SettingOriginRefused,
    SettingRedeclared,
    SettingsDeclarationMismatch,
    SettingTypeMismatch,
    SettingUndeclared,
    ValueType,
    assert_registry_matches,
    deployment_toml_path,
    env_variable_names,
    load_package_defaults,
    register,
    resolve,
    spec_for,
)

WORKSPACE = UUID("018f0000-0000-7000-8000-000000000001")
ACCOUNT = UUID("018f0000-0000-7000-8000-000000000002")

# The third copy of the production key set, and the reason a key lands in three files
# at once: ``schema.py`` declares it, ``config/defaults.toml`` carries its value, and
# this literal is what proves those two are not merely consistent with each other.
# Adding a key to only the first two reds the tests below rather than the chunk that
# forgot this one, so move all three together.
PRODUCTION_KEYS = {
    "storage.cluster_dsn_ref": "secret://env/RHEO_CLUSTER_DSN",
    "storage.control_database": "rheo_control",
    "storage.template_database": "template1",
    "storage.pool_cache_size": 16,
    "storage.pool_max_connections": 5,
    "storage.pool_idle_close_seconds": 300,
    "profile": "development",
    "identity.token_max_days.cli": 90,
    "identity.token_max_days.mcp": 30,
    "routing.mode": "path",
    "routing.scheme": "https",
    "routing.base_host": "localhost",
    "routing.shell.host": "circuit",
    "routing.shell.path": "/",
    "routing.identity.host": "auth",
    "routing.identity.path": "/auth",
    "routing.api.host": "api",
    "routing.api.path": "/api",
    "routing.mcp.host": "mcp",
    "routing.mcp.path": "/mcp",
    "routing.docs.host": "docs",
    "routing.docs.external": True,
    "routing.integration.host": "tuttle",
    "routing.integration.external": True,
    "routing.integration.reserved": True,
    "identity.allow_signup": False,
    "identity.allow_workspace_create": False,
    "identity.session_idle_days": 14,
    "identity.session_max_days": 30,
    "identity.providers.github.enabled": False,
    "identity.providers.github.client_id": "",
    "identity.providers.github.client_secret_ref": "",
    "internal.secret_ref": "",
    "work.due_reconcile_seconds": 900,
    "work.max_attempts": 8,
    "approvals.max_payload_bytes": 65536,
    "approvals.default_window_seconds": 900,
    "approvals.max_window_seconds": 86400,
}

FLOORED_KEYS = frozenset(
    {
        "identity.token_max_days.cli",
        "identity.token_max_days.mcp",
        "approvals.max_window_seconds",
    }
)
"""The only production keys a workspace may override, each with a ``min`` floor.

Named as a literal rather than matched by prefix. The loop below asserts that every
*other* declared key is deployment-scope and unfloored, so this set is the exhaustive
answer to "who can a workspace tighten"; a prefix test (`identity.`) answered that
question by accident and stopped being true the moment run 0c3 floored a key in
another namespace."""


class Rows:
    """An in-memory ``OverrideSource``: the rows a workspace/member would hold."""

    def __init__(
        self,
        workspace: dict[str, str] | None = None,
        member: dict[str, str] | None = None,
    ) -> None:
        self.workspace = workspace or {}
        self.member = member or {}

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]:
        assert workspace_id == WORKSPACE
        return self.workspace

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]:
        assert (workspace_id, account_id) == (WORKSPACE, ACCOUNT)
        return self.member


@pytest.fixture
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    isolate_rheo_environment(monkeypatch, root)
    register_harness_keys()
    return root


def write_deployment_toml(root: Path, text: str) -> None:
    path = deployment_toml_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ignored_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "setting_override_ignored"]


# --- the registry and the identity check --------------------------------------------


def test_the_registry_declares_every_production_key_and_its_shape() -> None:
    assert REGISTRY.keys(origin=CORE_ORIGIN) == frozenset(PRODUCTION_KEYS)
    assert dict(PACKAGE_DEFAULTS) == PRODUCTION_KEYS
    for key, value in PRODUCTION_KEYS.items():
        assert spec_for(key).default == value
    cli = spec_for("identity.token_max_days.cli")
    assert (cli.scope, cli.floor, cli.type) == (
        Scope.WORKSPACE,
        Floor.MIN,
        ValueType.INT,
    )
    assert spec_for("identity.token_max_days.mcp").floor is Floor.MIN
    window = spec_for("approvals.max_window_seconds")
    assert (window.scope, window.floor, window.type) == (
        Scope.WORKSPACE,
        Floor.MIN,
        ValueType.INT,
    )
    for key in PRODUCTION_KEYS:
        if key not in FLOORED_KEYS:
            assert spec_for(key).scope is Scope.DEPLOYMENT
            assert spec_for(key).floor is None
    assert spec_for("profile").choices == ("production", "development", "test")


def test_defaults_toml_loads_from_the_installed_package() -> None:
    assert load_package_defaults() == PRODUCTION_KEYS


def test_the_reconcile_floor_exceeds_the_pool_idle_window() -> None:
    """AC 18, the packaged-defaults half.

    A reconcile pass that came round faster than ``storage.pool_idle_close_seconds``
    would re-touch every cached engine inside its idle window, so nothing would ever
    be reclaimed by idleness and the count cap would be the only bound left — the
    thrash issue #12 removed. The relation is a property of the shipped pair, so it is
    asserted on the values, not on a hard-coded 900 and 300.

    This is only half of AC 18: both keys are deployment-scope, so an operator can
    break the relation through ``RHEO__work__…`` or ``RHEO__storage__…`` without this
    test noticing. ``_check_reconcile_interval`` in ``rheo doctor`` is the half that
    reads the *resolved* settings.
    """
    reconcile = PRODUCTION_KEYS["work.due_reconcile_seconds"]
    idle_close = PRODUCTION_KEYS["storage.pool_idle_close_seconds"]
    assert isinstance(reconcile, int) and isinstance(idle_close, int)
    assert reconcile > idle_close, (reconcile, idle_close)


def test_identity_check_names_an_undeclared_toml_key() -> None:
    with pytest.raises(SettingsDeclarationMismatch, match="storage.extra"):
        assert_registry_matches({**PACKAGE_DEFAULTS, "storage.extra": 1})


def test_identity_check_names_a_key_missing_from_the_toml() -> None:
    trimmed = dict(PACKAGE_DEFAULTS)
    del trimmed["profile"]
    with pytest.raises(SettingsDeclarationMismatch, match="profile"):
        assert_registry_matches(trimmed)


def test_identity_check_names_a_value_drift() -> None:
    drifted = {**PACKAGE_DEFAULTS, "storage.pool_cache_size": 64}
    with pytest.raises(SettingsDeclarationMismatch, match="storage.pool_cache_size"):
        assert_registry_matches(drifted)


def test_identity_check_names_a_type_drift() -> None:
    drifted = {**PACKAGE_DEFAULTS, "storage.pool_cache_size": "32"}
    with pytest.raises(SettingTypeMismatch, match="storage.pool_cache_size"):
        assert_registry_matches(drifted)


def test_harness_origin_is_refused_outside_the_test_profile(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "production")
    probe = KeySpec(
        key="harness.refused_probe",
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=1,
    )
    with pytest.raises(SettingOriginRefused, match="harness.refused_probe"):
        register(probe, origin=TEST_HARNESS_ORIGIN)
    assert "harness.refused_probe" not in REGISTRY


def test_harness_origin_is_accepted_under_the_test_profile(data_root: Path) -> None:
    register_harness_keys()  # idempotent: a second registration is a no-op
    assert REGISTRY.origin_of(HARNESS_FLOOR_MIN) == TEST_HARNESS_ORIGIN
    assert REGISTRY.keys(origin=CORE_ORIGIN) == frozenset(PRODUCTION_KEYS)


def test_redeclaring_a_key_with_a_different_spec_raises(data_root: Path) -> None:
    changed = KeySpec(
        key=HARNESS_FLOOR_MIN,
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=Floor.MIN,
        explicit_per_workspace=False,
        default=101,
    )
    with pytest.raises(SettingRedeclared, match=HARNESS_FLOOR_MIN):
        register(changed, origin=TEST_HARNESS_ORIGIN)
    with pytest.raises(SettingRedeclared, match=HARNESS_FLOOR_MIN):
        register(spec_for(HARNESS_FLOOR_MIN), origin=CORE_ORIGIN)
    assert spec_for(HARNESS_FLOOR_MIN).default == 100


def test_explicit_per_workspace_keys_carry_their_default(data_root: Path) -> None:
    specs = REGISTRY.explicit_per_workspace()
    assert {s.key for s in specs} == {HARNESS_EXPLICIT}
    assert specs[0].default == "harness-package-default"


def test_keyspec_rejects_a_floor_on_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="floor"):
        KeySpec(
            key="harness.bad_floor",
            type=ValueType.STR,
            scope=Scope.WORKSPACE,
            floor=Floor.MIN,
            explicit_per_workspace=False,
            default="x",
        )
    with pytest.raises(TypeError, match="default"):
        KeySpec(
            key="harness.bad_default",
            type=ValueType.INT,
            scope=Scope.WORKSPACE,
            floor=None,
            explicit_per_workspace=False,
            default="1",
        )


# --- precedence ----------------------------------------------------------------------


def test_precedence_default_then_deployment_then_override(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve()[HARNESS_EXPLICIT] == "harness-package-default"

    write_deployment_toml(
        data_root, '[harness]\nexplicit_per_workspace = "from-toml"\n'
    )
    assert resolve()[HARNESS_EXPLICIT] == "from-toml"

    monkeypatch.setenv("RHEO__harness__explicit_per_workspace", "from-env")
    assert resolve()[HARNESS_EXPLICIT] == "from-env"

    rows = Rows(workspace={HARNESS_EXPLICIT: "from-row"})
    assert resolve(workspace_id=WORKSPACE, source=rows)[HARNESS_EXPLICIT] == "from-row"
    # The row source is consulted only for a workspace.
    assert resolve(source=rows)[HARNESS_EXPLICIT] == "from-env"


def test_member_rows_apply_only_to_member_scope_keys(
    data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    rows = Rows(
        workspace={HARNESS_MEMBER: "workspace-row"},
        member={HARNESS_MEMBER: "member-row", HARNESS_EXPLICIT: "member-row"},
    )
    resolved = resolve(workspace_id=WORKSPACE, account_id=ACCOUNT, source=rows)
    assert resolved[HARNESS_MEMBER] == "member-row"
    assert resolved[HARNESS_EXPLICIT] == "harness-package-default"
    ignored = {
        (r.setting_key, r.state, r.override_scope) for r in ignored_records(caplog)
    }  # type: ignore[attr-defined]
    assert ignored == {
        (HARNESS_MEMBER, "setting_scope", "workspace"),
        (HARNESS_EXPLICIT, "setting_scope", "member"),
    }
    # Without an account there are no member rows at all.
    assert (
        resolve(workspace_id=WORKSPACE, source=rows)[HARNESS_MEMBER] == "member-default"
    )


def test_deployment_scope_row_is_ignored_and_logged(
    data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    rows = Rows(workspace={"storage.pool_cache_size": "1"})
    assert resolve(workspace_id=WORKSPACE, source=rows)["storage.pool_cache_size"] == 16
    [record] = ignored_records(caplog)
    assert record.setting_key == "storage.pool_cache_size"  # type: ignore[attr-defined]
    assert record.state == "setting_scope"  # type: ignore[attr-defined]
    assert "1" not in record.getMessage()


def test_row_that_will_not_coerce_is_ignored_and_logged(
    data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    rows = Rows(workspace={HARNESS_FLOOR_MIN: "lots"})
    assert resolve(workspace_id=WORKSPACE, source=rows)[HARNESS_FLOOR_MIN] == 100
    [record] = ignored_records(caplog)
    assert record.state == "setting_type"  # type: ignore[attr-defined]


# --- undeclared keys, refused at every source ----------------------------------------


def test_undeclared_key_is_refused_in_deployment_toml(data_root: Path) -> None:
    write_deployment_toml(data_root, "[nope]\nkey = 1\n")
    with pytest.raises(SettingUndeclared, match="nope.key") as excinfo:
        resolve()
    assert excinfo.value.state == "setting_undeclared"


def test_undeclared_key_is_refused_in_the_environment(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__nope__key", "1")
    with pytest.raises(SettingUndeclared, match="RHEO__nope__key") as excinfo:
        resolve()
    assert excinfo.value.key == "nope.key"


def test_undeclared_key_in_override_rows_is_ignored_and_logged(
    data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    resolved = resolve(workspace_id=WORKSPACE, source=Rows(workspace={"nope.key": "1"}))
    assert "nope.key" not in resolved
    with pytest.raises(SettingUndeclared, match="nope.key"):
        resolved["nope.key"]
    [record] = ignored_records(caplog)
    assert (record.setting_key, record.state) == ("nope.key", "setting_undeclared")  # type: ignore[attr-defined]


# --- the environment: coercion, the uppercased variant, RHEO_PROFILE -----------------


def test_env_variable_names_follow_the_double_underscore_mapping() -> None:
    assert env_variable_names("identity.token_max_days.cli") == (
        "RHEO__identity__token_max_days__cli",
        "RHEO__IDENTITY__TOKEN_MAX_DAYS__CLI",
    )


@pytest.mark.parametrize(
    ("variable", "raw", "key", "expected"),
    [
        ("RHEO__storage__pool_cache_size", "64", "storage.pool_cache_size", 64),
        ("RHEO__STORAGE__POOL_MAX_CONNECTIONS", "7", "storage.pool_max_connections", 7),
        (
            "RHEO__storage__control_database",
            "rheo_control_alt",
            "storage.control_database",
            "rheo_control_alt",
        ),
        ("RHEO__harness__floor_and", "true", HARNESS_FLOOR_AND, True),
        ("RHEO__HARNESS__FLOOR_AND", "0", HARNESS_FLOOR_AND, False),
        (
            "RHEO__harness__floor_union",
            "harness.confirm, extra ,",
            HARNESS_FLOOR_UNION,
            ["harness.confirm", "extra"],
        ),
        ("RHEO__harness__floor_union", "", HARNESS_FLOOR_UNION, []),
    ],
)
def test_environment_values_are_coerced_by_declared_type(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    raw: str,
    key: str,
    expected: object,
) -> None:
    monkeypatch.setenv(variable, raw)
    assert resolve()[key] == expected


def test_exact_env_form_wins_over_the_uppercased_variant(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__storage__pool_cache_size", "11")
    monkeypatch.setenv("RHEO__STORAGE__POOL_CACHE_SIZE", "22")
    assert resolve()["storage.pool_cache_size"] == 11


def test_rheo_profile_wins_over_the_double_underscore_form(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__profile", "development")
    assert resolve()["profile"] == "test"  # RHEO_PROFILE=test from the fixture
    monkeypatch.delenv("RHEO_PROFILE")
    assert resolve()["profile"] == "development"


def test_environment_beats_deployment_toml(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_deployment_toml(data_root, "[storage]\npool_cache_size = 8\n")
    assert resolve()["storage.pool_cache_size"] == 8
    monkeypatch.setenv("RHEO__storage__pool_cache_size", "9")
    assert resolve()["storage.pool_cache_size"] == 9


def test_environment_value_that_will_not_coerce_names_key_and_variable(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__storage__pool_cache_size", "lots")
    with pytest.raises(SettingTypeMismatch) as excinfo:
        resolve()
    message = str(excinfo.value)
    assert "storage.pool_cache_size" in message
    assert "RHEO__storage__pool_cache_size" in message
    assert "lots" not in message  # the text is never echoed


def test_profile_outside_the_three_values_is_refused(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "staging")
    with pytest.raises(SettingTypeMismatch, match="profile"):
        resolve()


def test_deployment_toml_type_mismatch_names_the_key(data_root: Path) -> None:
    write_deployment_toml(data_root, '[storage]\npool_cache_size = "32"\n')
    with pytest.raises(SettingTypeMismatch, match="storage.pool_cache_size"):
        resolve()


def test_deployment_toml_list_and_nested_keys(data_root: Path) -> None:
    write_deployment_toml(
        data_root,
        '[harness]\nfloor_union = ["a", "b"]\n\n[identity.token_max_days]\ncli = 45\n',
    )
    resolved = resolve()
    assert resolved[HARNESS_FLOOR_UNION] == ["a", "b"]
    assert resolved["identity.token_max_days.cli"] == 45


# --- the resolved mapping ------------------------------------------------------------


def test_resolved_settings_is_a_frozen_typed_mapping(data_root: Path) -> None:
    resolved = resolve()
    with pytest.raises(TypeError):
        resolved["profile"] = "x"  # type: ignore[index]
    with pytest.raises(AttributeError):
        resolved._values = {}  # type: ignore[attr-defined]
    handed_out = resolved[HARNESS_FLOOR_UNION]
    assert isinstance(handed_out, list)
    handed_out.append("mutated")
    assert resolved[HARNESS_FLOOR_UNION] == ["harness.confirm"]

    assert resolved.get_int("storage.pool_cache_size") == 16
    assert resolved.get_str("profile") == "test"
    assert resolved.get_bool(HARNESS_FLOOR_AND) is False
    assert resolved.get_list(HARNESS_FLOOR_UNION) == ["harness.confirm"]
    with pytest.raises(SettingTypeMismatch, match="profile"):
        resolved.get_int("profile")
    assert set(resolved) == REGISTRY.keys()
