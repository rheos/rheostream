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
    HARNESS_RETENTION_DAYS,
    HARNESS_RETENTION_DEFAULT,
    register_harness_keys,
)
from rheo_core.settings import (
    CORE_ORIGIN,
    PACKAGE_DEFAULTS,
    REGISTRY,
    TEST_HARNESS_ORIGIN,
    Floor,
    FrozenValue,
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
from rheo_core.settings.schema import check_value, decode_text

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
    "routing.public_host": "",
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
    "runtime.claude_cli.executable": "",
    "runtime.claude_cli.credential_kind": "login",
    "runtime.claude_cli.login_seed_dir": "",
    "runtime.claude_cli.credential_account_id": "",
    "runtime.claude_cli.credential_ref": "",
    "runtime.session_ttl_hours": 72,
    "runtime.max_deadline_seconds": 600,
    "runtime.max_context_bytes": 200000,
    "runtime.transcript_retention_days": 90,
    "runtime.allowed_runtimes": ("claude_cli",),
    "runtime.allowed_models": ("sonnet",),
    "modules.installed": (),
    "telemetry.tool_retention_days": 7,
    "telemetry.tool_max_rows": 10000,
}

FLOORED_KEYS = frozenset(
    {
        "identity.token_max_days.cli",
        "identity.token_max_days.mcp",
        "approvals.max_window_seconds",
        "runtime.max_deadline_seconds",
        "runtime.max_context_bytes",
        "runtime.transcript_retention_days",
        "runtime.allowed_runtimes",
        "runtime.allowed_models",
        "telemetry.tool_retention_days",
        "telemetry.tool_max_rows",
    }
)
"""The production keys a workspace may override, each with a floor.

Named as a literal rather than matched by prefix. The loop below asserts that every
*other* declared key is deployment-scope and unfloored, so this set is the exhaustive
answer to "who can a workspace tighten". ``min`` floors cap numeric ceilings;
``subset`` floors (``runtime.allowed_runtimes``, ``runtime.allowed_models``) are the
first production subset keys."""


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
    deadline = spec_for("runtime.max_deadline_seconds")
    assert (deadline.scope, deadline.floor, deadline.type) == (
        Scope.WORKSPACE,
        Floor.MIN,
        ValueType.INT,
    )
    assert spec_for("runtime.max_context_bytes").floor is Floor.MIN
    assert spec_for("runtime.transcript_retention_days").floor is Floor.MIN
    runtimes = spec_for("runtime.allowed_runtimes")
    assert (runtimes.scope, runtimes.floor, runtimes.type) == (
        Scope.WORKSPACE,
        Floor.SUBSET,
        ValueType.STR_LIST,
    )
    models = spec_for("runtime.allowed_models")
    assert (models.scope, models.floor, models.type) == (
        Scope.WORKSPACE,
        Floor.SUBSET,
        ValueType.STR_LIST,
    )
    kind = spec_for("runtime.claude_cli.credential_kind")
    assert (kind.scope, kind.floor, kind.choices) == (
        Scope.DEPLOYMENT,
        None,
        ("login", "api_key"),
    )
    for key in FLOORED_KEYS:
        assert spec_for(key).scope is Scope.WORKSPACE
        assert spec_for(key).floor is not None
    for key in PRODUCTION_KEYS:
        if key not in FLOORED_KEYS:
            assert spec_for(key).scope is Scope.DEPLOYMENT
            assert spec_for(key).floor is None
    assert spec_for("profile").choices == ("production", "development", "test")


def test_defaults_toml_loads_from_the_installed_package() -> None:
    loaded = load_package_defaults()
    frozen = {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in loaded.items()
    }
    assert frozen == PRODUCTION_KEYS


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


# --- numeric bounds -------------------------------------------------------------------
# 1a1 adds ``minimum``/``maximum`` to ``KeySpec``. The claim under test is that a
# bounded key is held to its range at **every** layer, because a bound one layer skips
# is not a bound: declaration (the spec and its own default), a natively typed value
# (``check_value``: the TOML, a write), a text value (``decode_text``: an environment
# variable, an override row), and — in ``test_settings_floor.py``, where the write path
# lives — a caller's write.


def _bounded(
    *,
    key: str = "harness.bounded_probe",
    value_type: ValueType = ValueType.INT,
    default: FrozenValue = 5,
    minimum: int | None = 1,
    maximum: int | None = 10,
) -> KeySpec:
    """A bounded spec, built but never registered.

    ``check_value`` and ``decode_text`` take a spec rather than a key, so both parsing
    entry points are provable without adding an eighth key to the process-wide
    registry — and a spec that is never registered cannot leak into another test's
    ``resolve()``.
    """
    return KeySpec(
        key=key,
        type=value_type,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )


def test_keyspec_rejects_bounds_on_a_key_that_is_not_an_int() -> None:
    with pytest.raises(TypeError, match="minimum/maximum apply to int keys only"):
        _bounded(value_type=ValueType.STR, default="x")


def test_keyspec_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError, match="minimum 10 is above maximum 1"):
        _bounded(minimum=10, maximum=1)


def test_keyspec_rejects_a_default_outside_its_own_bounds() -> None:
    """Declaration is the first layer, and the package default is what it holds.

    Without this, a key could ship a default its own range forbids and every layer
    below would be enforcing a bound the key itself had already broken.
    """
    with pytest.raises(
        ValueError, match="default 0 is outside the declared range 1-10"
    ):
        _bounded(default=0)
    with pytest.raises(ValueError, match="default 11 is outside"):
        _bounded(default=11)


def test_check_value_accepts_the_endpoints_and_refuses_either_side() -> None:
    """Inclusive both ends, and the refusal names the key, the value and the bound."""
    spec = _bounded()
    assert check_value(spec, 1, source="the TOML") == 1
    assert check_value(spec, 10, source="the TOML") == 10
    for value in (0, 11):
        with pytest.raises(SettingTypeMismatch) as excinfo:
            check_value(spec, value, source="the TOML")
        assert excinfo.value.key == "harness.bounded_probe"
        assert str(value) in str(excinfo.value)
        assert "the declared range 1-10" in str(excinfo.value)


def test_decode_text_refuses_an_out_of_range_text_value() -> None:
    spec = _bounded()
    assert decode_text(spec, " 3 ", source="workspace row") == 3
    with pytest.raises(SettingTypeMismatch, match="the declared range 1-10"):
        decode_text(spec, "0", source="workspace row")


def test_a_one_sided_bound_names_only_the_bound_it_has() -> None:
    lower = _bounded(minimum=1, maximum=None)
    assert check_value(lower, 10_000, source="the TOML") == 10_000
    with pytest.raises(SettingTypeMismatch, match="the declared minimum 1"):
        check_value(lower, 0, source="the TOML")
    upper = _bounded(minimum=None, maximum=10)
    assert check_value(upper, -5, source="the TOML") == -5
    with pytest.raises(SettingTypeMismatch, match="the declared maximum 10"):
        check_value(upper, 11, source="the TOML")


def test_an_unbounded_key_keeps_exactly_its_old_behaviour() -> None:
    """Both bounds default to ``None``: every key declared before 1a1 is untouched."""
    spec = spec_for("storage.pool_cache_size")
    assert (spec.minimum, spec.maximum) == (None, None)
    assert check_value(spec, 10_000, source="the TOML") == 10_000
    assert decode_text(spec, "-1", source="an environment variable") == -1


def test_an_out_of_range_row_is_ignored_and_logged_rather_than_raised(
    data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The bound reaches the row layer, and the resolver's own rule is unchanged.

    ``_apply_rows`` catches ``SettingTypeMismatch`` and drops the row — "a stale or
    hostile row can neither crash a request nor take effect" — so the observable
    effect of an out-of-range row is that the key keeps the value below it, with one
    ``setting_type`` line in the log. A bound that raised out of ``resolve()`` would
    hand any writer of one row a way to fail every request in the workspace.
    """
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    resolved = resolve(
        workspace_id=WORKSPACE, source=Rows(workspace={HARNESS_RETENTION_DAYS: "0"})
    )
    assert resolved[HARNESS_RETENTION_DAYS] == HARNESS_RETENTION_DEFAULT
    assert (HARNESS_RETENTION_DAYS, "setting_type") in {
        (r.setting_key, r.state)  # type: ignore[attr-defined]
        for r in ignored_records(caplog)
    }
    # An in-range row still applies, so the ignore above is the bound and not the key.
    assert (
        resolve(
            workspace_id=WORKSPACE, source=Rows(workspace={HARNESS_RETENTION_DAYS: "2"})
        )[HARNESS_RETENTION_DAYS]
        == 2
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
