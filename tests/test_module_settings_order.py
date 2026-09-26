"""#108: an allowed module's own deployment-scoped setting resolves from the first
``resolve()`` on, and every stray ``RHEO__*`` variable is still refused.

Seams under test: ``read_deployment_value()`` (one key, nothing else read);
``allowed_module_ids()``'s read through it; ``register_module_settings()`` (only the
allowlisted modules, settings only); and the early hook at the composition roots —
``run_startup()`` through the real FastAPI lifespan, and the ``rheo`` CLI's
``run_command``, including the two subcommands it must skip, its refusal path (a
malformed ``modules.installed``, a module that cannot load), and the one side effect
it has on ``workspace create``; and the worker's ``main()``.

**Recallatron is the real module in every positive case**, through its real
``rheo.modules`` entry point and its real ``recallatron.embedding.provider`` key. The
not-allowlisted case needs a module that is discoverable and never named, so it is
fabricated here (``absent_probe``, ``_probe``-suffixed per ``tests/harness/modules.py``)
and published beside the real one.

**Process state is restored by every test.** ``settings.schema.REGISTRY`` publishes no
unregister, so the autouse fixture swaps copies of its two tables in through
``monkeypatch`` (``tests/postgres/test_memory_retention.py``'s technique) and the
originals come back afterwards; environment variables go through ``monkeypatch`` too.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install, manifest, publish
from rheo_app_cli import main as cli_main_module
from rheo_app_core import startup as startup_module
from rheo_app_core.main import app, lifespan
from rheo_app_worker import main as worker_main_module
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    ManifestInvalid,
    allowed_module_ids,
    discovered,
    register_module_settings,
    reset_surfaces,
)
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.redaction.policy import exclude_types_key
from rheo_core.refs import uuid7
from rheo_core.settings import (
    PROFILE_KEY,
    KeySpec,
    Scope,
    ValueType,
    env_variable_names,
    resolve,
)
from rheo_core.settings.deployment import read_deployment_value
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.settings.schema import SettingTypeMismatch, SettingUndeclared
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import list_workspaces
from rheo_core.storage.provisioning import database_name_for
from rheo_core.storage.repositories import workspace_settings
from rheo_recallatron.configuration import (
    EMBEDDING_PROVIDER_KEY,
    RETRIEVAL_STRATEGY_KEY,
    STRATEGY_HYBRID,
)

RECALLATRON = "recallatron"
PROVIDER_VARIABLE = env_variable_names(EMBEDDING_PROVIDER_KEY)[0]
STRAY_VARIABLE = "RHEO__recallatron__no_such_key"
EXCLUDE_TYPES_KEY = exclude_types_key(RECALLATRON)
EXCLUDE_TYPES_VARIABLE = env_variable_names(EXCLUDE_TYPES_KEY)[0]

ABSENT_ID = "absent_probe"
ABSENT_KEY = f"{ABSENT_ID}.flag"
ABSENT_VARIABLE = env_variable_names(ABSENT_KEY)[0]
ABSENT_MANIFEST = manifest(
    ABSENT_ID,
    configuration_schema=(
        KeySpec(
            key=ABSENT_KEY,
            type=ValueType.BOOL,
            scope=Scope.DEPLOYMENT,
            floor=None,
            explicit_per_workspace=False,
            default=False,
        ),
    ),
)
ABSENT_ENTRY_POINT = EntryPoint(
    name=ABSENT_ID, value=f"{__name__}:ABSENT_MANIFEST", group=ENTRY_POINT_GROUP
)

# Allowlisted but unloadable: a contract major this core does not publish, and an
# entry point whose module does not exist.
WRONG_CONTRACT_ID = "wrongcontract_probe"
WRONG_CONTRACT_MANIFEST = manifest(WRONG_CONTRACT_ID, core_contract_versions=(99,))
WRONG_CONTRACT_ENTRY_POINT = EntryPoint(
    name=WRONG_CONTRACT_ID,
    value=f"{__name__}:WRONG_CONTRACT_MANIFEST",
    group=ENTRY_POINT_GROUP,
)

# Allowlisted and loadable, with a key of its own: the earlier half of the
# all-or-nothing case, published ahead of the wrong-contract module.
ATOMIC_ID = "atomic_probe"
ATOMIC_KEY = f"{ATOMIC_ID}.flag"
ATOMIC_MANIFEST = manifest(
    ATOMIC_ID,
    configuration_schema=(
        KeySpec(
            key=ATOMIC_KEY,
            type=ValueType.BOOL,
            scope=Scope.DEPLOYMENT,
            floor=None,
            explicit_per_workspace=False,
            default=False,
        ),
    ),
)
ATOMIC_ENTRY_POINT = EntryPoint(
    name=ATOMIC_ID, value=f"{__name__}:ATOMIC_MANIFEST", group=ENTRY_POINT_GROUP
)
UNIMPORTABLE_ID = "unimportable_probe"
UNIMPORTABLE_ENTRY_POINT = EntryPoint(
    name=UNIMPORTABLE_ID,
    value="rheo_no_such_distribution_probe:MANIFEST",
    group=ENTRY_POINT_GROUP,
)


def _recallatron_entry_point() -> EntryPoint:
    (real,) = [entry for entry in discovered() if entry.name == RECALLATRON]
    return real


@pytest.fixture(autouse=True)
def restored_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A private copy of the settings tables, and both modules discoverable."""
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    publish(monkeypatch, _recallatron_entry_point(), ABSENT_ENTRY_POINT)
    reset_surfaces()
    yield
    reset_surfaces()


@pytest.fixture
def provider_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(PROVIDER_VARIABLE, "local")


# --- read_deployment_value and the allowlist ----------------------------------------


def test_read_deployment_value_ignores_an_unrelated_stray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(STRAY_VARIABLE, "anything")
    assert read_deployment_value(ALLOWLIST_KEY) == (RECALLATRON,)
    assert allowed_module_ids() == frozenset({RECALLATRON})


def test_the_allowlist_falls_back_to_its_empty_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    assert read_deployment_value(ALLOWLIST_KEY) is None
    assert allowed_module_ids() == frozenset()


# --- register_module_settings ---------------------------------------------------------


@pytest.mark.usefixtures("provider_in_environment")
def test_an_allowed_modules_environment_setting_resolves() -> None:
    # The pre-fix failure: the first full resolve refuses the module's own key.
    with pytest.raises(SettingUndeclared):
        resolve()
    assert register_module_settings() == (RECALLATRON,)
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"
    # Idempotent.
    assert register_module_settings() == (RECALLATRON,)


_COLD_EXCLUDE_TYPES = """
from rheo_core.redaction.policy import exclude_types_key
from rheo_core.settings import resolve
from rheo_core.settings.schema import SettingUndeclared
key = exclude_types_key("recallatron")
try:
    resolve()
except SettingUndeclared:
    print("refused")
else:
    print("resolved unexpectedly")
from rheo_core.modules import register_module_settings
register_module_settings()
print(",".join(resolve().get_list(key)))
"""


def test_an_allowed_modules_redaction_exclude_types_resolves_after_register(
    tmp_path: Path,
) -> None:
    """#130: ``_register_settings()`` declares ``<module_id>.redaction.exclude_types``
    for every module that owns a record type, Recallatron included -- the same early
    hook and the same before/after shape as any other module settings key.

    Run in a genuinely cold interpreter, like the #108 cases further down: the
    in-process form left ``exclude_types`` registered on the shared process-wide
    ``SETTINGS_REGISTRY`` for every later test in this file, so its "before
    register" refusal passed only when this test ran before the others that
    register Recallatron -- a test-order dependency, not a real assertion.
    """
    result = _cold_run(
        tmp_path,
        {
            env_variable_names(ALLOWLIST_KEY)[0]: RECALLATRON,
            EXCLUDE_TYPES_VARIABLE: "memory,duplicate",
        },
        _COLD_EXCLUDE_TYPES,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused\nmemory,duplicate"


def test_an_allowed_modules_deployment_toml_setting_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment.toml").write_text(
        '[modules]\ninstalled = ["recallatron"]\n\n'
        '[recallatron.embedding]\nprovider = "local"\n',
        encoding="utf-8",
    )
    assert register_module_settings() == (RECALLATRON,)
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"


def test_a_stray_module_variable_is_refused_before_and_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(STRAY_VARIABLE, "anything")
    with pytest.raises(SettingUndeclared) as before:
        resolve()
    assert STRAY_VARIABLE in str(before.value)
    assert register_module_settings() == (RECALLATRON,)
    with pytest.raises(SettingUndeclared) as after:
        resolve()
    assert STRAY_VARIABLE in str(after.value)


def test_a_discoverable_module_not_in_the_allowlist_stays_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(ABSENT_VARIABLE, "true")
    registered = register_module_settings()
    assert ABSENT_ID not in registered
    assert registered == (RECALLATRON,)
    assert ABSENT_KEY not in SETTINGS_REGISTRY
    with pytest.raises(SettingUndeclared) as refused:
        resolve()
    assert ABSENT_VARIABLE in str(refused.value)


def test_a_later_refused_manifest_registers_no_earlier_modules_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All or nothing: the first permitted module loads cleanly and the second is
    refused, so neither module's keys may be left behind in the registry."""
    publish(monkeypatch, ATOMIC_ENTRY_POINT, WRONG_CONTRACT_ENTRY_POINT)
    install(monkeypatch, ATOMIC_ID, WRONG_CONTRACT_ID)
    with pytest.raises(ManifestInvalid) as refused:
        register_module_settings()
    assert WRONG_CONTRACT_ID in str(refused.value)
    assert ATOMIC_KEY not in SETTINGS_REGISTRY


# --- the composition roots ------------------------------------------------------------


@pytest.mark.usefixtures("provider_in_environment")
async def test_run_startup_resolves_with_the_module_variable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real lifespan, with the full module load stubbed out.

    Stubbing ``load_modules`` keeps Recallatron's operations off the process-wide
    registry the rest of the session reads, and it makes the assertion sharper: with
    the full load gone, the early hook is the only thing left that could declare the
    key ``run_startup()``'s first ``resolve()`` reads.
    """
    monkeypatch.setattr(startup_module, "load_modules", lambda **_: ())
    async with lifespan(app):
        assert app.state.startup.profile == "test"
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"


@pytest.mark.usefixtures("provider_in_environment")
def test_the_cli_resolves_with_the_module_variable_set() -> None:
    assert cli_main_module.main(["routing", "hosts"]) == 0


@pytest.mark.parametrize(
    "argv", [["openapi", "--out", "-"], ["web", "compose", "--out", "-"]]
)
def test_openapi_and_web_never_register_module_settings(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    install(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(
        cli_main_module, "register_module_settings", lambda: calls.append(None)
    )
    assert cli_main_module.main(argv) == 0
    assert calls == []


def test_every_other_command_registers_module_settings_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(
        cli_main_module, "register_module_settings", lambda: calls.append(None)
    )
    assert cli_main_module.main(["routing", "hosts"]) == 0
    assert calls == [None]


# --- the CLI's refusal path -----------------------------------------------------------


@pytest.mark.parametrize(
    "toml_value",
    ["5", "[1, 2]", "true"],
    ids=["integer", "list-of-integers", "boolean"],
)
def test_a_malformed_allowlist_is_a_cli_refusal_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    toml_value: str,
) -> None:
    """The early hook sits inside ``run_command``'s ``try``: moving it above turns
    this into an uncaught ``SettingTypeMismatch``."""
    install(monkeypatch)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "deployment.toml").write_text(
        f"[modules]\ninstalled = {toml_value}\n", encoding="utf-8"
    )
    capsys.readouterr()
    assert cli_main_module.main(["routing", "hosts"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"{SettingTypeMismatch.state}: "), err
    assert ALLOWLIST_KEY in err
    assert err.count("\n") == 1
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("entry_point", "state"),
    [
        (WRONG_CONTRACT_ENTRY_POINT, "module_invalid"),
        (UNIMPORTABLE_ENTRY_POINT, "module_load_failed"),
    ],
    ids=["wrong-contract", "unimportable"],
)
@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["migrate"],
        ["token", "revoke", "00000000-0000-7000-8000-000000000000"],
    ],
    ids=["doctor", "migrate", "token-revoke"],
)
def test_an_allowed_module_that_cannot_load_is_a_cli_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_point: EntryPoint,
    state: str,
    argv: list[str],
) -> None:
    publish(monkeypatch, entry_point)
    install(monkeypatch, entry_point.name)
    capsys.readouterr()
    assert cli_main_module.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"{state}: "), err
    assert entry_point.name in err or "rheo_no_such_distribution_probe" in err
    assert err.count("\n") == 1


# --- a genuinely cold interpreter -----------------------------------------------------
#
# Every case above runs in a process where pytest imported ``rheo_recallatron`` long
# before ``monkeypatch.setenv``, so an import-time side effect of that package never
# sees the module's variable. A deployment's core, worker and CLI each start cold with
# the variable already set; these cases do the same in a fresh interpreter.

_COLD_REPRO = """
from rheo_core.modules import register_module_settings
register_module_settings()
from rheo_recallatron.embedding.registry import configured_provider_name
print(configured_provider_name())
"""

# What a composition root does after the early hook: import the module (as the full
# load will) and resolve in full, strictly.
_COLD_RESOLVE = """
from rheo_core.modules import register_module_settings
register_module_settings()
import rheo_recallatron.embedding.registry
from rheo_core.settings import resolve
resolve()
"""


def _cold_run(
    tmp_path: Path, extra: dict[str, str], script: str = _COLD_REPRO
) -> subprocess.CompletedProcess[str]:
    """``script`` in a fresh interpreter with every inherited RHEO_* stripped."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("RHEO_")
    }
    environ["RHEO_DATA_ROOT"] = str(tmp_path)
    environ.update(extra)
    return subprocess.run(
        [sys.executable, "-c", script],
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_cold_start_resolves_an_allowed_modules_environment_setting(
    tmp_path: Path,
) -> None:
    result = _cold_run(
        tmp_path,
        {env_variable_names(ALLOWLIST_KEY)[0]: RECALLATRON, PROVIDER_VARIABLE: "none"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "none"


def test_cold_start_still_refuses_a_stray_variable_of_an_allowed_module(
    tmp_path: Path,
) -> None:
    result = _cold_run(
        tmp_path,
        {env_variable_names(ALLOWLIST_KEY)[0]: RECALLATRON, STRAY_VARIABLE: "anything"},
        _COLD_RESOLVE,
    )
    assert result.returncode != 0
    assert "SettingUndeclared" in result.stderr
    assert STRAY_VARIABLE in result.stderr


def test_cold_start_still_refuses_a_module_variable_when_not_allowlisted(
    tmp_path: Path,
) -> None:
    result = _cold_run(tmp_path, {PROVIDER_VARIABLE: "none"}, _COLD_RESOLVE)
    assert result.returncode != 0
    assert "SettingUndeclared" in result.stderr
    assert PROVIDER_VARIABLE in result.stderr


# Importing the registry registers nothing and reads no deployment layer; the first use
# registers the built-ins exactly once, however many uses follow.
_COLD_FIRST_USE = """
import rheo_recallatron.embedding.registry as registry
assert registry.PROVIDERS == {}, sorted(registry.PROVIDERS)
calls = []
original = registry.register_builtin_providers
def counting():
    calls.append(1)
    original()
registry.register_builtin_providers = counting
fake = registry.providers()[registry.FAKE_PROVIDER]
registry.resolve_provider()
registry.register_provider("probe", fake)
assert registry.providers()[registry.FAKE_PROVIDER] is fake
assert calls == [1], calls
print("registered once")
"""

# The import itself never reads the strict deployment layer, so a variable no schema
# declares yet cannot fail it; the first use still reads it, and still refuses.
_COLD_IMPORT_THEN_USE = """
import rheo_recallatron.embedding.registry as registry
print("imported")
for attempt in (1, 2):
    try:
        registry.providers()
    except Exception as refusal:
        message = str(refusal).replace("\\n", " ")
        print(f"refused {attempt}: {type(refusal).__name__} {message}")
    else:
        print(f"returned {attempt}: {sorted(registry.PROVIDERS)}")
"""


def test_cold_registry_registers_built_ins_on_first_use_exactly_once(
    tmp_path: Path,
) -> None:
    result = _cold_run(
        tmp_path, {env_variable_names(PROFILE_KEY)[0]: "test"}, _COLD_FIRST_USE
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "registered once"


def test_cold_registry_import_reads_no_deployment_layer_but_first_use_stays_strict(
    tmp_path: Path,
) -> None:
    result = _cold_run(tmp_path, {PROVIDER_VARIABLE: "none"}, _COLD_IMPORT_THEN_USE)
    assert result.returncode == 0, result.stderr
    imported, first, second = result.stdout.strip().splitlines()
    assert imported == "imported"
    # A refused first use must not leave the registry marked done: the second use reads
    # the strict layer again and refuses again, never returning an empty registry.
    for attempt, line in ((1, first), (2, second)):
        assert line.startswith(f"refused {attempt}: SettingUndeclared "), line
        assert PROVIDER_VARIABLE in line


# The production entry point is the first use: after the early hook, a cold
# ``resolve_provider()`` with nothing else touching the registry registers the
# built-ins and returns the configured one.
_COLD_RESOLVE_FIRST = """
from rheo_core.modules import register_module_settings
register_module_settings()
import rheo_recallatron.embedding.registry as registry
assert registry.PROVIDERS == {}, sorted(registry.PROVIDERS)
print(type(registry.resolve_provider()).__name__)
"""


def test_cold_resolve_provider_as_first_use_returns_the_configured_built_in(
    tmp_path: Path,
) -> None:
    result = _cold_run(
        tmp_path,
        {
            env_variable_names(ALLOWLIST_KEY)[0]: RECALLATRON,
            env_variable_names(PROFILE_KEY)[0]: "test",
            PROVIDER_VARIABLE: "fake",
        },
        _COLD_RESOLVE_FIRST,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FakeEmbeddingProvider"


# A same-named register_provider() before any other use replaces the built-in, and the
# built-ins registering later on that same first use never overwrite it.
_COLD_OVERRIDE_FIRST = """
import rheo_recallatron.embedding.registry as registry
from rheo_recallatron.embedding.fake import FakeEmbeddingProvider
override = FakeEmbeddingProvider()
registry.register_provider(registry.FAKE_PROVIDER, override)
assert registry.providers()[registry.FAKE_PROVIDER] is override
assert registry.providers()[registry.FAKE_PROVIDER] is override
print("override kept")
"""


def test_cold_register_provider_as_first_use_overrides_the_built_in(
    tmp_path: Path,
) -> None:
    result = _cold_run(
        tmp_path, {env_variable_names(PROFILE_KEY)[0]: "test"}, _COLD_OVERRIDE_FIRST
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "override kept"


# --- the worker's composition root ----------------------------------------------------


class _ReachedModuleLoad(Exception):
    """Raised in place of the worker's full module load: main() got past its resolve."""


@pytest.mark.usefixtures("provider_in_environment")
def test_worker_main_resolves_with_the_module_variable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` up to and through ``refuse_misconfigured_login()``'s ``resolve()``.

    The full module load is replaced by a sentinel, so nothing after it runs (no
    backend, no loop) and the early hook is the only thing that could have declared
    the module's key by then. Without the hook, ``SettingUndeclared`` arrives first.
    """

    def _stop() -> None:
        raise _ReachedModuleLoad

    monkeypatch.setattr(worker_main_module, "register_modules", _stop)
    with pytest.raises(_ReachedModuleLoad):
        worker_main_module.main()
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"


# --- workspace create's side effect ---------------------------------------------------


def _workspace_ids(cluster: ClusterSession) -> set[UUID]:
    with cluster.backend.control_engine.connect() as connection:
        return {row.id for row in list_workspaces(connection)}


def _create_workspace(capsys: pytest.CaptureFixture[str], owner: UUID) -> UUID:
    capsys.readouterr()
    assert cli_main_module.main(["workspace", "create", "--owner", str(owner)]) == 0
    return UUID(capsys.readouterr().out.strip())


def _settings_rows(cluster: ClusterSession, workspace_id: UUID) -> dict[str, str]:
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        return workspace_settings(uow.connection)


@pytest.mark.postgres
def test_workspace_create_writes_an_allowed_modules_per_workspace_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
) -> None:
    """The recorded side effect of the early hook: provisioning writes every
    ``explicit_per_workspace`` key the registry holds, and the hook has put an
    allowed module's keys there before the handler runs. Inert until the module is
    installed and enabled in the workspace; insert-if-absent, so nothing twice."""
    before = _workspace_ids(cluster)
    try:
        install(monkeypatch)
        capsys.readouterr()
        assert (
            cli_main_module.main(
                [
                    "account",
                    "create",
                    "--provider",
                    "github",
                    "--subject",
                    f"subject-{uuid7().hex[:12]}",
                    "--display-name",
                    "order-owner",
                ]
            )  # fmt: skip
            == 0
        )
        owner = UUID(capsys.readouterr().out.strip())

        plain = _create_workspace(capsys, owner)
        assert RETRIEVAL_STRATEGY_KEY not in _settings_rows(cluster, plain)

        install(monkeypatch, RECALLATRON)
        with_module = _create_workspace(capsys, owner)
        rows = _settings_rows(cluster, with_module)
        assert rows[RETRIEVAL_STRATEGY_KEY] == STRATEGY_HYBRID

        # Idempotent: a repair of the same workspace changes nothing.
        capsys.readouterr()
        assert cli_main_module.main(["workspace", "repair", str(with_module)]) == 0
        assert _settings_rows(cluster, with_module) == rows
    finally:
        for workspace_id in _workspace_ids(cluster) - before:
            cluster.record(database_name_for(workspace_id))
