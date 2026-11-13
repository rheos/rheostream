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

import dataclasses
import hashlib
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
from rheo_app_mcp.tools import call_tool, list_tools
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.approvals.guards import _permission_detail
from rheo_core.audit import sink as sink_module
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import (
    context_from_operation,
    context_from_session,
    context_from_token,
)
from rheo_core.deletion.registry import OwnedDeletionRegistry
from rheo_core.modules import ENTRY_POINT_GROUP, discovered
from rheo_core.modules import loader as loader_module
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.refusals import OPERATION_NOT_PERMITTED
from rheo_core.operations.registry import REGISTRY, Authorized
from rheo_core.redaction.registry import RenderingRegistry
from rheo_core.refs.resolver import RESOLVERS
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.settings.schema import SettingsRegistry
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    get_access_token,
    insert_access_token,
    insert_access_token_operations,
    list_access_token_operations,
)
from rheo_core.tokens.format import mint
from rheo_core.tokens.issue import connector_operations, issue_connector_token
from rheo_core.tokens.sets import (
    PACKAGE_SETS,
    TOOL_REGISTRY,
    explicit_only_operations,
    package_set,
)

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
    discover: bool = False,
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
            *(["--discover"] if discover else []),
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


VAULT_DEFAULT_MANIFEST: Final = VAULT_MANIFEST.model_copy(
    update={"package_sets": "default"}
)
"""The same module before it opted out: every named set takes its operations."""


def _publish_vault(monkeypatch: pytest.MonkeyPatch, attribute: str) -> None:
    current = [entry for entry in discovered() if entry.name == CURRENT]
    assert current
    publish(
        monkeypatch,
        *current,
        EntryPoint(
            name=VAULT, value=f"{__name__}:{attribute}", group=ENTRY_POINT_GROUP
        ),
    )
    install(monkeypatch, CURRENT, VAULT)


@pytest.fixture
def vault_beside_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery sees the real Current and the fabricated opt-in module, and the
    deployment allows both, so the command loads both."""
    _publish_vault(monkeypatch, "VAULT_MANIFEST")


@pytest.fixture
def vault_default_beside_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """As :func:`vault_beside_current`, with the vault still a default module."""
    _publish_vault(monkeypatch, "VAULT_DEFAULT_MANIFEST")


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
def test_the_opt_out_travels_with_the_registration_not_the_loaders_table(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Forgetting the loaded manifests (``reset_surfaces``, or a load that stopped
    part way) leaves the operations registered. They must still count as opt-in:
    the sets read the flag the registration carries, so this fails closed."""
    code, _, err = _issue(
        capsys, workspace, owner_account_id, set_name="read_only", kind="cli"
    )
    assert code == 0, err
    loader_module.reset_surfaces()
    assert VAULT not in loader_module.loaded_manifests()
    assert {VAULT_GET, VAULT_LIST, VAULT_ADD} <= REGISTRY.names()

    for name, evaluator in PACKAGE_SETS.items():
        assert not {op for op in evaluator() if op.startswith(f"{VAULT}.")}, name
    assert not {
        op for op in connector_operations(Role.OWNER) if op.startswith(f"{VAULT}.")
    }
    opt_in = package_set(f"module_read:{VAULT}")
    assert opt_in is not None and opt_in() == {VAULT_GET, VAULT_LIST}


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


# --- a default module switched to explicit_only: narrowed at presentation (#537) ---

VAULT_OPERATIONS: Final = frozenset({VAULT_GET, VAULT_LIST, VAULT_ADD})
VAULT_GET_TOOL: Final = f"{VAULT}_get"
SESSION_HOST: Final = "example.test"
_FAR_FUTURE: Final = datetime(2999, 1, 1, tzinfo=UTC)


def _switch_vault_to_explicit_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next deploy loads the vault ``explicit_only``: its registrations carry the
    flag, and no stored token row is touched."""
    for name in VAULT_OPERATIONS:
        monkeypatch.setitem(
            REGISTRY._operations,
            name,
            dataclasses.replace(REGISTRY._operations[name], explicit_only=True),
        )


def _present(value: str, surface: str) -> WorkspaceContext:
    ctx = context_from_token(value, surface)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _scope(value: str, surface: str) -> frozenset[str]:
    """The operation set the token presents with: always a closed list."""
    operation_set = _present(value, surface).operation_set
    assert isinstance(operation_set, frozenset), operation_set
    return operation_set


def _vault_enabled(ctx: WorkspaceContext) -> WorkspaceContext:
    """``ctx`` with the vault enabled, so module enablement is not what decides."""
    return ctx.model_copy(update={"enabled_modules": ctx.enabled_modules | {VAULT}})


def _issued(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    account: UUID,
    *,
    set_name: str,
    kind: str,
    discover: bool = False,
) -> tuple[str, UUID]:
    """The value and id of a token the operator command issued."""
    code, out, err = _issue(
        capsys, workspace, account, set_name=set_name, kind=kind, discover=discover
    )
    assert code == 0, err
    return out.strip(), _token_id(err)


def _stored(cluster: ClusterSession, token_id: UUID) -> frozenset[str]:
    with cluster.backend.control_engine.connect() as connection:
        return list_access_token_operations(connection, token_id)


def _session_ctx(workspace: UUID, account: UUID) -> WorkspaceContext:
    session_row = create_session(account)
    secret = mint_host_secret(session_row.id, SESSION_HOST)
    assert switch_workspace(session_row.id, workspace) is None
    ctx = context_from_session(secret, SESSION_HOST)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _inserted(
    cluster: ClusterSession,
    workspace: UUID,
    account: UUID,
    *,
    set_name: str | None,
    operations: frozenset[str],
) -> str:
    """A ``cli`` token row written outside ``core.token.issue``, with ``set_name``."""
    value, raw = mint("cli")
    with cluster.backend.control_engine.begin() as connection:
        row = insert_access_token(
            connection,
            account_id=account,
            workspace_id=workspace,
            kind="cli",
            issued_from="operator",
            token_hash=hashlib.sha256(raw).digest(),
            set_name=set_name,
            purpose=None,
            expires_at=_FAR_FUTURE,
        )
        insert_access_token_operations(
            connection, token_id=row.id, operation_names=sorted(operations)
        )
    return value


def _error_text(outcome: OperationOutcome) -> str | None:
    return None if outcome.error is None else outcome.error.error_text


@pytest.mark.usefixtures("isolated_process_tables", "vault_default_beside_current")
def test_routine_sets_lose_a_module_switched_to_explicit_only_at_presentation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Issued while the vault was default, each routine token holds vault
    operations; after the switch none presents with one, the stored rows are
    unchanged, and dispatch refuses the vault read on the set, not on enablement."""
    tokens = [
        (
            *_issued(capsys, workspace, owner_account_id, set_name=name, kind=kind),
            surface,
        )
        for name, kind, surface in (
            ("cli_full", "cli", "api"),
            ("read_only", "cli", "api"),
            ("agent_default", "mcp", "mcp"),
        )
    ]
    stored = {token_id: _stored(cluster, token_id) for _, token_id, _ in tokens}
    for value, token_id, surface in tokens:
        assert VAULT_GET in stored[token_id]
        assert "current.item.get" in stored[token_id]
        before = _present(value, surface)
        assert before.operation_set == stored[token_id]
        assert isinstance(
            REGISTRY.authorize(_vault_enabled(before), VAULT_GET), Authorized
        )

    _switch_vault_to_explicit_only(monkeypatch)

    for value, token_id, surface in tokens:
        after = _scope(value, surface)
        assert after == stored[token_id] - VAULT_OPERATIONS
        assert "current.item.get" in after
        assert _stored(cluster, token_id) == stored[token_id]  # no row rewrite
        refused = REGISTRY.authorize(
            _vault_enabled(_present(value, surface)), VAULT_GET
        )
        assert isinstance(refused, Refusal) and refused.state == OPERATION_NOT_PERMITTED


@pytest.mark.usefixtures("isolated_process_tables", "vault_default_beside_current")
def test_the_facade_stops_listing_and_calling_a_switched_modules_tools(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Direct listing, the discover catalogue and ``operations_call`` all follow the
    narrowed scope. ``_vault_handler`` raises, so a call that got through fails."""
    direct_value, _ = _issued(
        capsys, workspace, owner_account_id, set_name="agent_default", kind="mcp"
    )
    discover_value, _ = _issued(
        capsys,
        workspace,
        owner_account_id,
        set_name="agent_default",
        kind="mcp",
        discover=True,
    )

    def catalogue(ctx: WorkspaceContext) -> list[str]:
        outcome = call_tool(ctx, "operations_catalog", {"limit": 500}, consumers=None)
        assert outcome.ok, outcome
        return [entry.name for entry in outcome.result.tools]  # type: ignore[union-attr]

    direct = _vault_enabled(_present(direct_value, "mcp"))
    discovering = _vault_enabled(_present(discover_value, "mcp"))
    assert VAULT_GET_TOOL in {tool.name for tool in list_tools(direct)}
    assert VAULT_GET_TOOL in catalogue(discovering)

    _switch_vault_to_explicit_only(monkeypatch)

    direct = _vault_enabled(_present(direct_value, "mcp"))
    discovering = _vault_enabled(_present(discover_value, "mcp"))
    assert VAULT_GET_TOOL not in {tool.name for tool in list_tools(direct)}
    assert VAULT_GET_TOOL not in catalogue(discovering)
    by_name = call_tool(direct, VAULT_GET_TOOL, {}, consumers=None)
    generic = call_tool(
        discovering,
        "operations_call",
        {"name": VAULT_GET_TOOL, "input": {}},
        consumers=None,
    )
    assert by_name.state == "not_found", by_name
    assert generic.state == by_name.state, generic
    assert _error_text(generic) == _error_text(by_name)


@pytest.mark.usefixtures("isolated_process_tables", "vault_default_beside_current")
def test_a_connector_grant_loses_a_switched_module_at_presentation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    _issued(capsys, workspace, owner_account_id, set_name="read_only", kind="cli")
    # The command above loaded the default vault into this process.
    with cluster.backend.control_engine.begin() as connection:
        token_id, value, _ = issue_connector_token(
            connection,
            account_id=owner_account_id,
            workspace_id=workspace,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    stored = _stored(cluster, token_id)
    assert {VAULT_GET, VAULT_ADD} <= stored  # owner role, the two vault tools
    assert _scope(value, "mcp") == stored

    _switch_vault_to_explicit_only(monkeypatch)

    after = _scope(value, "mcp")
    assert after == stored - VAULT_OPERATIONS
    assert "current.item.get" in after


@pytest.mark.usefixtures("isolated_process_tables", "vault_default_beside_current")
def test_an_explicit_list_keeps_what_it_was_issued_after_the_switch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """A session's explicit list is kept whole by the same switch that narrows a
    routine token beside it."""
    routine_value, routine_id = _issued(
        capsys, workspace, owner_account_id, set_name="cli_full", kind="cli"
    )
    outcome = dispatch(
        _session_ctx(workspace, owner_account_id),
        TOKEN_ISSUE,
        {"kind": "cli", "operations": [VAULT_GET, VAULT_ADD, "current.item.get"]},
    )
    assert outcome.ok, outcome
    result = outcome.result
    assert result is not None
    explicit_value: str = result.value  # type: ignore[attr-defined]
    explicit_id: UUID = result.token_id  # type: ignore[attr-defined]
    explicit = _stored(cluster, explicit_id)
    assert explicit == {VAULT_GET, VAULT_ADD, "current.item.get"}

    _switch_vault_to_explicit_only(monkeypatch)

    assert _scope(explicit_value, "api") == explicit
    routine = _stored(cluster, routine_id)
    assert VAULT_OPERATIONS <= routine
    assert _scope(routine_value, "api") == routine - VAULT_OPERATIONS


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
def test_a_module_read_token_keeps_the_opt_in_reads_at_presentation(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    value, _ = _issued(
        capsys,
        workspace,
        owner_account_id,
        set_name=f"module_read:{VAULT}",
        kind="mcp",
    )
    # A routine name would strip both: the vault is opt-in in this process.
    assert {VAULT_GET, VAULT_LIST} <= explicit_only_operations()
    assert _scope(value, "mcp") == {VAULT_GET, VAULT_LIST}


@pytest.mark.usefixtures("isolated_process_tables", "vault_beside_current")
def test_an_unknown_set_name_is_narrowed_as_routine(
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """A row naming a set nothing resolves fails closed; the same row with no set
    name, an explicit list, keeps the vault read."""
    _issued(capsys, workspace, owner_account_id, set_name="read_only", kind="cli")
    held = frozenset({VAULT_GET, "current.item.get"})
    legacy = _inserted(
        cluster, workspace, owner_account_id, set_name="legacy_probe", operations=held
    )
    unnamed = _inserted(
        cluster, workspace, owner_account_id, set_name=None, operations=held
    )

    assert _scope(legacy, "api") == {"current.item.get"}
    assert _scope(unnamed, "api") == held


@pytest.mark.usefixtures("isolated_process_tables", "vault_default_beside_current")
def test_a_stored_caller_and_the_approval_guard_follow_the_narrowing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """A run, continuation or approved execution rebuilds its token caller through
    ``context_from_operation``; an approval's actor guard reads the token's scope.
    Both narrow exactly as presentation does."""
    _, token_id = _issued(
        capsys, workspace, owner_account_id, set_name="cli_full", kind="cli"
    )

    def rebuilt() -> WorkspaceContext:
        ctx = context_from_operation(
            workspace,
            actor_kind="token",
            actor_id=token_id,
            audience_kind="token",
            audience_id=token_id,
            entry="api",
            purpose=None,
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def detail() -> str | None:
        return _permission_detail(
            workspace,
            actor_kind="token",
            actor_id=token_id,
            roles=frozenset({Role.OWNER}),
            operation_name=VAULT_ADD,
            whose="the gated actor",
            now=datetime.now(UTC),
        )

    before = rebuilt().operation_set
    assert isinstance(before, frozenset) and VAULT_ADD in before
    assert detail() is None

    _switch_vault_to_explicit_only(monkeypatch)

    narrowed = rebuilt().operation_set
    assert isinstance(narrowed, frozenset)
    assert not narrowed & VAULT_OPERATIONS
    assert "current.item.get" in narrowed
    text = detail()
    assert text is not None and f"no longer permits {VAULT_ADD}" in text
