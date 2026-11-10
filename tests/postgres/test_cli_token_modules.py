"""Issue #117: ``rheo token issue`` loads the allowed modules, so a CLI-issued token
can carry a module operation.

Before the fix the command registered the core operations and nothing else, so
``cli_full`` and ``read_only`` never named a Recallatron operation and an ``mcp``
token's ``agent_default`` named no tool at all (``register_core_tools()`` was never
called either). The command now runs the same two calls ``apps/core``'s startup makes
after ``register_core_operations()``.

**Driven through ``main(argv)`` with ``modules.installed`` naming Recallatron**, the
way an operator runs it, and checked against the operations stored on the issued
token's own rows.

**The process-wide tables are isolated for the length of each test.** The command
writes into the global operation, tool and resolver registries (``dispatch`` reads
those, so they cannot be local), and the loader writes settings keys, renderings,
owned-delete declarations, audit sinks and its loaded table into process-global
tables that publish no unregister. Left behind, Recallatron's operations would widen
``read_only`` for ``tests/postgres/test_tokens.py``'s exact-set assertions, and its
``explicit_per_workspace`` key would be written into every workspace provisioned later
in the session. Each table is swapped for a copy (or a fresh instance) through
``monkeypatch``, which puts the original back on teardown.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from typing import Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install, manifest, publish
from pydantic import BaseModel, ConfigDict
from rheo_app_cli.main import main
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.audit import sink as sink_module
from rheo_core.deletion.registry import OwnedDeletionRegistry
from rheo_core.modules import ENTRY_POINT_GROUP, discovered
from rheo_core.modules import loader as loader_module
from rheo_core.operations.registry import REGISTRY
from rheo_core.redaction.registry import RenderingRegistry
from rheo_core.refs.resolver import RESOLVERS
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.settings.schema import SettingsRegistry
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    get_access_token,
    list_access_token_operations,
)
from rheo_core.tokens.issue import connector_operations
from rheo_core.tokens.sets import PACKAGE_SETS, TOOL_REGISTRY

pytestmark = pytest.mark.postgres

RECALLATRON = "recallatron"
CURRENT = "current"


@pytest.fixture
def isolated_process_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Copies of every process-global table the command and the loader write."""
    monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
    monkeypatch.setattr(TOOL_REGISTRY, "_tools", dict(TOOL_REGISTRY._tools))
    monkeypatch.setattr(RESOLVERS, "_resolvers", dict(RESOLVERS._resolvers))
    monkeypatch.setattr(sink_module, "_SINKS", dict(sink_module._SINKS))
    settings = SettingsRegistry()
    for key in SETTINGS_REGISTRY.keys():
        settings.register(
            SETTINGS_REGISTRY.get(key), origin=SETTINGS_REGISTRY.origin_of(key)
        )
    monkeypatch.setattr(loader_module, "SETTINGS_REGISTRY", settings)
    monkeypatch.setattr(loader_module, "RENDERINGS", RenderingRegistry())
    monkeypatch.setattr(loader_module, "OWNED_DELETIONS", OwnedDeletionRegistry())
    monkeypatch.setattr(loader_module, "_LOADED", {})
    yield


def _issue(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    account: UUID,
    *,
    set_name: str,
    kind: str,
) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(
        [
            "token",
            "issue",
            "--account",
            str(account),
            "--workspace",
            str(workspace),
            "--set",
            set_name,
            "--kind",
            kind,
        ]
    )
    out, err = capsys.readouterr()
    return code, out, err


def _token_id(err: str) -> UUID:
    """The issued token's id, from the line the command prints on stderr."""
    first = err.strip().splitlines()[-1].split()
    assert first[0] == "token" and first[2] == "issued,", err
    return UUID(first[1])


def _stored_operations(cluster: ClusterSession, err: str) -> frozenset[str]:
    """The issued token's operations, read back off its rows by the id on stderr."""
    with cluster.backend.control_engine.connect() as connection:
        return list_access_token_operations(connection, _token_id(err))


@pytest.mark.usefixtures("isolated_process_tables")
def test_a_cli_token_carries_the_allowed_modules_operations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    install(monkeypatch, RECALLATRON)

    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name="read_only", kind="cli"
    )

    assert code == 0, err
    assert out.startswith("rheo_cli_")
    stored = _stored_operations(cluster, err)
    # The issue's own reproduction names this one.
    assert "recallatron.entity.list" in stored
    assert "recallatron.memory.recall" in stored
    assert "core.workspace.status" in stored
    # ``read_only`` still means read: a module's mutate operation stays out.
    assert "recallatron.memory.remember" not in stored


@pytest.mark.usefixtures("isolated_process_tables")
def test_an_mcp_token_holds_the_core_and_module_tools_operations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """``agent_default`` reads the tool registry, which the command never populated:
    before #117 this set was empty even for the core's own tools."""
    install(monkeypatch, RECALLATRON)

    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name="agent_default", kind="mcp"
    )

    assert code == 0, err
    assert out.startswith("rheo_mcp_")
    stored = _stored_operations(cluster, err)
    assert "recallatron.memory.recall" in stored
    assert "core.workspace.status" in stored


@pytest.mark.usefixtures("isolated_process_tables")
def test_an_alert_writer_cli_token_holds_exactly_two_operations_for_90_days_at_most(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """The issuance an external alert writer's token comes from, run as the operator
    runs it: ``--set alert_writer --kind cli`` with Current installed."""
    install(monkeypatch, CURRENT)
    before = datetime.now(UTC)

    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name="alert_writer", kind="cli"
    )

    assert code == 0, err
    assert out.strip()
    stored = _stored_operations(cluster, err)
    assert stored == {"current.item.create", "current.item.close_by_source"}
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token(connection, _token_id(err))
    assert row is not None
    assert before < row.expires_at <= datetime.now(UTC) + timedelta(days=90)


@pytest.mark.usefixtures("isolated_process_tables")
def test_a_module_the_allowlist_does_not_name_stays_out(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Loading honours ``modules.installed``: an installed distribution the
    deployment did not allow contributes nothing to a token."""
    install(monkeypatch)

    code, _, err = _issue(
        capsys, workspace, owner_account_id, set_name="cli_full", kind="cli"
    )

    assert code == 0, err
    stored = _stored_operations(cluster, err)
    assert "core.workspace.status" in stored
    assert not {name for name in stored if name.startswith(f"{RECALLATRON}.")}


# --- explicit_only modules: no named package set reaches them ---------------------

VAULT: Final = "vault_probe"
VAULT_GET: Final = f"{VAULT}.entry.get"
VAULT_LIST: Final = f"{VAULT}.entry.list"
VAULT_ADD: Final = f"{VAULT}.entry.add"


class _VaultInput(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _VaultOutput(BaseModel):
    model_config = ConfigDict(frozen=True)


def _vault_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: _VaultInput
) -> _VaultOutput:
    raise AssertionError("token issuance never dispatches a module operation")


class _VaultSink:
    def record(self, *args: object, **kwargs: object) -> None:
        return None


def _vault_operation(
    name: str, safety_class: SafetyClass
) -> tuple[OperationDeclaration, object]:
    return (
        OperationDeclaration(
            name=name,
            safety_class=safety_class,
            roles=frozenset({Role.OWNER}),
            input_model=_VaultInput,
            output=_VaultOutput,
            idempotency=Idempotency.NONE,
            audit=None
            if safety_class is SafetyClass.READ
            else AuditSpec(subject_field=None),
        ),
        _vault_handler,
    )


def _vault_tool(
    name: str, operation: str, safety_class: SafetyClass
) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        safety_class=safety_class,
        operation=operation,
        input_model=_VaultInput,
    )


VAULT_MANIFEST: Final = manifest(
    VAULT,
    package_sets="explicit_only",
    operations=(
        _vault_operation(VAULT_GET, SafetyClass.READ),
        _vault_operation(VAULT_LIST, SafetyClass.READ),
        _vault_operation(VAULT_ADD, SafetyClass.MUTATE),
    ),
    tools=(
        _vault_tool(f"{VAULT}_get", VAULT_GET, SafetyClass.READ),
        _vault_tool(f"{VAULT}_add", VAULT_ADD, SafetyClass.MUTATE),
    ),
    audit_sink=_VaultSink(),
)
"""A fabricated opt-in module: two reads (one with a tool), one mutate with a tool.
Owner-only, so an owner's role reaches every operation and only the set decides."""


@pytest.fixture
def vault_beside_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery sees the real Current and the fabricated opt-in module, and the
    deployment allows both, so the command loads both."""
    current = [entry for entry in discovered() if entry.name == CURRENT]
    assert current
    publish(
        monkeypatch,
        *current,
        EntryPoint(
            name=VAULT, value=f"{__name__}:VAULT_MANIFEST", group=ENTRY_POINT_GROUP
        ),
    )
    install(monkeypatch, CURRENT, VAULT)


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
@pytest.mark.parametrize(
    ("set_name", "kind"),
    [("read_only", "cli"), ("cli_full", "cli"), ("agent_default", "mcp")],
)
def test_no_named_package_set_reaches_an_explicit_only_module(
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    set_name: str,
    kind: str,
) -> None:
    code, _, err = _issue(
        capsys, workspace, owner_account_id, set_name=set_name, kind=kind
    )

    assert code == 0, err
    stored = _stored_operations(cluster, err)
    assert "current.item.get" in stored  # the default module beside it still counts
    assert not {name for name in stored if name.startswith(f"{VAULT}.")}


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
def test_every_set_evaluator_and_the_connector_grant_skip_it(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Every evaluator, ``alert_writer`` included, and the snapshot an OAuth connector
    grant takes, which is ``agent_default`` bounded by the member's role."""
    code, _, err = _issue(
        capsys, workspace, owner_account_id, set_name="read_only", kind="cli"
    )
    assert code == 0, err  # the command loaded both modules into this process
    assert {VAULT_GET, VAULT_LIST, VAULT_ADD} <= REGISTRY.names()

    for name, evaluator in PACKAGE_SETS.items():
        assert not {op for op in evaluator() if op.startswith(f"{VAULT}.")}, name
    granted = connector_operations(Role.OWNER)
    assert "current.item.get" in granted
    assert not {op for op in granted if op.startswith(f"{VAULT}.")}


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
def test_module_read_issues_exactly_the_opt_in_modules_reads(
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """The operator command takes only a named set, so the opt-in is one: its reads,
    tool or not, and nothing else, not even the core's own reads."""
    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name=f"module_read:{VAULT}", kind="mcp"
    )

    assert code == 0, err
    assert out.startswith("rheo_mcp_")
    assert _stored_operations(cluster, err) == {VAULT_GET, VAULT_LIST}


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
@pytest.mark.parametrize("module_id", [CURRENT, "absent_probe", ""])
def test_module_read_refuses_a_default_or_unknown_module(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
    module_id: str,
) -> None:
    """``module_read`` exists for opt-in modules only: a default module is already
    reached by the rule sets, and an unknown one names nothing."""
    code, out, err = _issue(
        capsys,
        workspace,
        owner_account_id,
        set_name=f"module_read:{module_id}",
        kind="mcp",
    )

    assert code == 1
    assert out == ""
    assert err.startswith("set_not_issuable"), err
