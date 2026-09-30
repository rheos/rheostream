"""Issue #262: discover-then-call mode over the real tool-origin facade.

Seams under test: ``apps/mcp``'s ``list_tools``/``call_tool`` pair, driven with a
context the boundary resolved from a real bearer, against the registries
``loaded_probe_modules`` built with Recallatron in them (the same recipe as
``test_memory_mcp.py``).

**The claim is that ``operations_call`` is not a second way in.** So almost every
test below makes the same call twice, once directly under a direct-mode token and
once through ``operations_call`` under a discover-then-call token holding the same
grants, and asserts the two outcomes are the same outcome: state, error text,
approval hold, audit row. A test that only checked the generic call succeeded would
stay green against a generic call that skipped a check.

**Tokens are written straight into the control plane, then resolved through the
boundary.** ``core.token.issue`` expands against the process-wide registries, which
do not hold the loaded module's operations here (they live in the local registries
the fixture hands out), so an operator issuance could not grant ``memory.*`` at all.
Writing the snapshot rows directly is how ``test_mcp_transport.py`` builds its
expired and revoked tokens too; the context itself still comes only from
``resolve_context``, so nothing below constructs a context by hand.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import (
    NOTE_GET,
    add_member,
    enable_harness_module,
    register_harness,
)
from rheo_app_mcp.session import resolve_context
from rheo_app_mcp.tools import call_tool, list_tools
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import tables as approval_tables
from rheo_core.audit import AUDIT_LIST
from rheo_core.audit.telemetry_tables import tool_telemetry
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.deletion.operations import RECORD_DELETE
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import (
    OPERATION_GET,
    OPERATION_LIST,
    OperationRegistry,
    register_core_operations,
)
from rheo_core.operations.core_ops import TOOL_CALL, WORKSPACE_STATUS
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    insert_access_token,
    insert_access_token_operations,
)
from rheo_core.tokens.format import mint
from rheo_core.tokens.sets import (
    DISCOVER_THEN_CALL_TOOL_NAMES,
    ToolRegistry,
    register_core_tools,
)
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_READ,
    MEMORY_RECALL,
    MEMORY_REMEMBER,
)
from rheo_recallatron.resolvers import resolve_memory
from sqlalchemy import delete, func, select

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_NOT_FOUND: Final = "not_found"
_INPUT_INVALID: Final = "input_invalid"
_APPROVAL_REQUIRED: Final = "approval_required"
_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

_FIXED: Final[frozenset[str]] = frozenset(
    {
        "operations_call",
        "operations_catalog",
        "operations_describe",
        "operations_get",
        "operations_list",
    }
)
"""The whole discover-then-call listing, typed out rather than read from
``DISCOVER_THEN_CALL_TOOL_NAMES`` so the test states the claim instead of
restating the code (one test below holds the two equal)."""

_CORE_READS: Final[tuple[str, ...]] = (WORKSPACE_STATUS, OPERATION_GET, OPERATION_LIST)
_MEMORY_WRITES: Final[tuple[str, ...]] = (
    MEMORY_RECALL,
    MEMORY_READ,
    MEMORY_REMEMBER,
    MEMORY_CORRECT,
    RECORD_DELETE,
)


# --- the workspace ---------------------------------------------------------------


@dataclass(frozen=True)
class Discover:
    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str

    def token(
        self, operations: Iterable[str], *, account_id: UUID | None = None
    ) -> WorkspaceContext:
        """An ``mcp`` token holding exactly ``operations``, resolved by the boundary.

        Issued to the owner unless ``account_id`` names another member, whose
        membership role the boundary then reads onto the context.
        """
        value, raw = mint("mcp")
        with self.cluster.backend.control_engine.begin() as connection:
            row = insert_access_token(
                connection,
                account_id=(
                    self.owner_account_id if account_id is None else account_id
                ),
                workspace_id=self.workspace,
                kind="mcp",
                issued_from="operator",
                token_hash=hashlib.sha256(raw).digest(),
                set_name=None,
                purpose=None,
                expires_at=_FAR_FUTURE,
            )
            insert_access_token_operations(
                connection, token_id=row.id, operation_names=sorted(set(operations))
            )
        ctx = resolve_context(value)
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def pair(
        self, operations: Iterable[str], *, account_id: UUID | None = None
    ) -> tuple[WorkspaceContext, WorkspaceContext]:
        """(direct, discover): two tokens with the same grants, one in each mode."""
        grants = tuple(operations)
        return (
            self.token(grants, account_id=account_id),
            self.token((*grants, TOOL_CALL), account_id=account_id),
        )

    def catalogue(self, ctx: WorkspaceContext) -> list[str]:
        outcome = self.call(ctx, "operations_catalog", {"limit": 500})
        assert outcome.ok, outcome
        return [entry.name for entry in outcome.result.tools]  # type: ignore[union-attr]

    def listed(
        self,
        ctx: WorkspaceContext,
        *,
        tools: ToolRegistry | None = None,
        registry: OperationRegistry | None = None,
    ) -> frozenset[str]:
        return frozenset(
            tool.name
            for tool in list_tools(
                ctx,
                tools=self.surfaces.tools if tools is None else tools,
                registry=self.surfaces.operations if registry is None else registry,
            )
        )

    def call(
        self,
        ctx: WorkspaceContext,
        name: str,
        arguments: Mapping[str, object],
        *,
        consumers: ConsumerRegistry | None = None,
    ) -> OperationOutcome:
        return call_tool(
            ctx,
            name,
            arguments,
            consumers=consumers,
            tools=self.surfaces.tools,
            registry=self.surfaces.operations,
        )

    def generic(
        self,
        ctx: WorkspaceContext,
        name: str,
        arguments: Mapping[str, object],
        *,
        consumers: ConsumerRegistry | None = None,
    ) -> OperationOutcome:
        return self.call(
            ctx,
            "operations_call",
            {"name": name, "input": dict(arguments)},
            consumers=consumers,
        )

    def unit_of_work(self) -> UnitOfWork:
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        return UnitOfWork(engine, self.database_name)

    def counts(self) -> dict[str, int]:
        tables = {
            "approval": approval_tables.approval,
            "operation": work_tables.operation,
            "audit_record": work_tables.audit_record,
        }
        with self.unit_of_work() as uow:
            return {
                name: int(
                    uow.connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for name, table in tables.items()
            }

    def audit_names(self) -> list[str]:
        with self.unit_of_work() as uow:
            return [
                str(name)
                for name in uow.connection.execute(
                    select(work_tables.audit_record.c.operation_name).order_by(
                        work_tables.audit_record.c.occurred_at
                    )
                ).scalars()
            ]

    def telemetry_names(self) -> list[str]:
        with self.unit_of_work() as uow:
            return [
                str(name)
                for name in uow.connection.execute(
                    select(tool_telemetry.c.tool_name).order_by(
                        tool_telemetry.c.occurred_at, tool_telemetry.c.id
                    )
                ).scalars()
            ]

    def clear_telemetry(self) -> None:
        with self.unit_of_work() as uow:
            uow.connection.execute(delete(tool_telemetry))
            uow.commit()


@contextmanager
def _loaded(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    *,
    enable: bool,
) -> Iterator[Discover]:
    """Recallatron loaded, and installed and enabled in ``workspace`` when
    ``enable``. Disabled is still *loaded*: its tools are registered and their
    operations resolve, so the only reason one is unreachable is the module state."""
    register_core_operations()
    register_core_tools()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(
        monkeypatch, _MEMORY_MODULE, deletions=OWNED_DELETIONS
    ) as surfaces:
        register_core_operations(surfaces.operations)
        register_core_tools(surfaces.tools)
        if enable:
            bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
            assert isinstance(bootstrap, WorkspaceContext), bootstrap
            install_and_enable_module(
                cluster.backend, bootstrap, workspace, _MEMORY_MODULE
            )
        row = cluster.registry_row(workspace)
        yield Discover(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
        )


@pytest.fixture
def discover(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[Discover]:
    with _loaded(
        monkeypatch, cluster, workspace, owner_account_id, enable=True
    ) as loaded:
        yield loaded


@pytest.fixture
def disabled(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[Discover]:
    with _loaded(
        monkeypatch, cluster, workspace, owner_account_id, enable=False
    ) as loaded:
        yield loaded


def _text(outcome: OperationOutcome) -> str | None:
    if outcome.error is None:
        return None
    text = outcome.error.error_text
    for own_id, placeholder in (
        (outcome.approval_id, "<approval_id>"),
        (outcome.operation_id, "<operation_id>"),
    ):
        if own_id is not None:
            text = text.replace(str(own_id), placeholder)
    return text


def _same(direct: OperationOutcome, generic: OperationOutcome) -> None:
    """The parts of an outcome a caller acts on, equal across the two paths.

    An approval hold names its own ids in its text, and two calls hold two
    approvals, so each outcome's own ids are replaced before the texts are compared.
    """
    assert generic.state == direct.state, (direct, generic)
    assert _text(generic) == _text(direct), (direct, generic)
    assert (generic.approval_id is None) == (direct.approval_id is None)
    assert (generic.operation_id is None) == (direct.operation_id is None)


def _remember(
    discover: Discover, ctx: WorkspaceContext, *, generic: bool
) -> OperationOutcome:
    arguments = {
        "kind": "note",
        "title": "a memory written for the discover-then-call tests",
        "body": "its body carries nothing a test needs to read back",
        "purposes": ["respond"],
    }
    if generic:
        return discover.generic(
            ctx, "recallatron_remember", arguments, consumers=ConsumerRegistry()
        )
    return discover.call(
        ctx, "recallatron_remember", arguments, consumers=ConsumerRegistry()
    )


# --- a constant-size listing ------------------------------------------------------


def test_the_fixed_names_are_the_codes_own(discover: Discover) -> None:
    assert frozenset(DISCOVER_THEN_CALL_TOOL_NAMES) == _FIXED


def test_the_listing_stays_five_however_many_tools_are_registered(
    discover: Discover,
) -> None:
    """Core only, then Recallatron's seven, then the harness operation behind
    ``harness_get_note``: the discover-then-call listing is the same five names each
    time, while a direct-mode token with the same grants grows each time (the
    positive control that proves the registrations landed)."""
    with discover.unit_of_work() as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    grants = (*_CORE_READS, *_MEMORY_WRITES, NOTE_GET)
    direct, discovering = discover.pair(grants)

    core_tools, core_operations = ToolRegistry(), OperationRegistry()
    register_core_operations(core_operations)
    register_core_tools(core_tools)
    core_direct = discover.listed(direct, tools=core_tools, registry=core_operations)
    core_discover = discover.listed(
        discovering, tools=core_tools, registry=core_operations
    )

    module_direct = discover.listed(direct)
    module_discover = discover.listed(discovering)

    register_harness(registry=discover.surfaces.operations)
    harness_direct = discover.listed(direct)
    harness_discover = discover.listed(discovering)

    assert core_discover == module_discover == harness_discover == _FIXED
    assert core_direct < module_direct < harness_direct
    assert "recallatron_remember" in module_direct - core_direct
    assert harness_direct - module_direct == {"harness_get_note"}
    assert (_FIXED - {"operations_get", "operations_list"}).isdisjoint(harness_direct)


def test_a_direct_token_never_sees_the_three_discover_tools(discover: Discover) -> None:
    direct = discover.token((*_CORE_READS, *_MEMORY_WRITES))
    listed = discover.listed(direct)
    assert {"operations_call", "operations_catalog", "operations_describe"}.isdisjoint(
        listed
    )
    outcome = discover.call(direct, "operations_call", {"name": "workspace_status"})
    assert outcome.state == _NOT_FOUND, outcome


def test_a_hidden_tool_called_directly_is_not_found_to_a_discover_token(
    discover: Discover,
) -> None:
    """ "Callable directly" still means "listed": the grant is held, the tool is
    hidden, and a direct call by name is the same ``not_found`` any unlisted tool
    gets. The same tool through ``operations_call`` runs."""
    _, discovering = discover.pair(_CORE_READS)
    hidden = discover.call(discovering, "workspace_status", {})
    assert hidden.state == _NOT_FOUND, hidden
    assert hidden.error is not None
    assert hidden.error.error_text == "'workspace_status' is not a tool available here"
    reached = discover.generic(discovering, "workspace_status", {})
    assert reached.ok, reached


# --- the catalogue and the description -------------------------------------------


def test_the_catalogue_lists_exactly_what_the_token_may_call(
    discover: Discover,
) -> None:
    """Only granted tools, and none of the three discover tools themselves. The
    role and module-origin exclusions are asserted on the catalogue in the two
    tests below that disable each."""
    _, discovering = discover.pair((WORKSPACE_STATUS, MEMORY_RECALL, OPERATION_LIST))
    outcome = discover.call(discovering, "operations_catalog", {})
    assert outcome.ok, outcome
    names = [entry.name for entry in outcome.result.tools]  # type: ignore[union-attr]
    assert names == sorted(
        ["operations_list", "recallatron_recall", "workspace_status"]
    )

    filtered = discover.call(discovering, "operations_catalog", {"query": "RECALL"})
    assert [e.name for e in filtered.result.tools] == ["recallatron_recall"]  # type: ignore[union-attr]


def test_describe_answers_the_direct_listings_schema_or_not_found(
    discover: Discover,
) -> None:
    direct, discovering = discover.pair((MEMORY_RECALL,))
    listed = {
        tool.name: tool
        for tool in list_tools(
            direct, tools=discover.surfaces.tools, registry=discover.surfaces.operations
        )
    }
    described = discover.call(
        discovering, "operations_describe", {"name": "recallatron_recall"}
    )
    assert described.ok, described
    result = described.result
    assert result is not None
    assert result.input_schema == (  # type: ignore[attr-defined]
        listed["recallatron_recall"].input_model.model_json_schema()
    )

    refused = discover.call(
        discovering, "operations_describe", {"name": "recallatron_remember"}
    )
    called = discover.generic(discovering, "recallatron_remember", {})
    _same(called, refused)
    assert refused.state == _NOT_FOUND


# --- operations_call is the direct path ------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["recallatron_remember", "audit_list", "no_such_tool", "operations_call"],
)
def test_a_call_outside_the_grants_gets_the_direct_refusal(
    discover: Discover, tool: str
) -> None:
    """Ungranted, unregistered, or the generic tool naming itself: each is the same
    ``not_found`` with the same text a direct-mode token gets calling the tool by
    name, and none writes anything."""
    direct, discovering = discover.pair((MEMORY_RECALL, WORKSPACE_STATUS))
    before = discover.counts()
    by_name = discover.call(direct, tool, {})
    generic = discover.generic(discovering, tool, {})
    assert by_name.state == _NOT_FOUND, by_name
    _same(by_name, generic)
    assert discover.counts() == before


def test_a_read_token_cannot_write_through_the_generic_call(
    discover: Discover,
) -> None:
    """The READ-class boundary holds: a token granted only reads, in discover mode,
    cannot reach a mutate or destructive tool through ``operations_call``."""
    _, discovering = discover.pair((MEMORY_RECALL, MEMORY_READ, *_CORE_READS))
    before = discover.counts()
    written = _remember(discover, discovering, generic=True)
    assert written.state == _NOT_FOUND, written
    forgotten = discover.generic(
        discovering, "recallatron_forget", {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}"}
    )
    assert forgotten.state == _NOT_FOUND, forgotten
    assert discover.counts() == before


def test_a_write_carrying_a_mask_token_is_refused_through_the_generic_call(
    discover: Discover,
) -> None:
    direct, discovering = discover.pair(_MEMORY_WRITES)
    arguments: dict[str, Any] = {
        "ref": f"{_MEMORY_MODULE}.memory:{uuid4()}",
        "expected_revision": 1,
        "title": "a correction",
        "body": "reach me at [email withheld] instead",
    }
    by_name = discover.call(direct, "recallatron_correct", arguments)
    generic = discover.generic(discovering, "recallatron_correct", arguments)
    assert by_name.state == _INPUT_INVALID, by_name
    _same(by_name, generic)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"kind": "note", "title": "t", "body": "b", "purposes": ["respond"], "x": 1},
        {"kind": "note", "title": "t", "body": "b", "purposes": "respond"},
        {"workspace_id": str(uuid4()), "kind": "note", "title": "t", "body": "b"},
    ],
    ids=["missing-fields", "undeclared-argument", "wrong-type", "reserved-name"],
)
def test_input_validation_errors_match_the_direct_call(
    discover: Discover, arguments: dict[str, Any]
) -> None:
    direct, discovering = discover.pair(_MEMORY_WRITES)
    before = discover.counts()
    by_name = discover.call(
        direct, "recallatron_remember", arguments, consumers=ConsumerRegistry()
    )
    generic = discover.generic(
        discovering, "recallatron_remember", arguments, consumers=ConsumerRegistry()
    )
    assert by_name.state == _INPUT_INVALID, by_name
    _same(by_name, generic)
    assert discover.counts() == before


def test_a_malformed_envelope_is_input_invalid_and_reaches_nothing(
    discover: Discover,
) -> None:
    _, discovering = discover.pair(_MEMORY_WRITES)
    before = discover.counts()
    for arguments in (
        {},
        {"name": "recallatron_remember", "input": "not an object"},
        {"name": "recallatron_remember", "input": {}, "as_role": "owner"},
    ):
        outcome = discover.call(discovering, "operations_call", arguments)
        assert outcome.state == _INPUT_INVALID, (arguments, outcome)
    assert discover.counts() == before


def test_a_destructive_call_is_held_for_approval_through_the_generic_call(
    discover: Discover,
) -> None:
    """``approval_required`` with both ids, one approval row, nothing erased: the
    same hold a direct call gets, and never a success wrapping it."""
    direct, discovering = discover.pair(_MEMORY_WRITES)
    written = _remember(discover, direct, generic=False)
    assert written.ok, written
    reference = str(written.result.ref)  # type: ignore[union-attr]

    before = discover.counts()
    by_name = discover.call(
        direct, "recallatron_forget", {"ref": reference}, consumers=ConsumerRegistry()
    )
    middle = discover.counts()
    generic = discover.generic(
        discovering,
        "recallatron_forget",
        {"ref": reference},
        consumers=ConsumerRegistry(),
    )
    after = discover.counts()

    assert by_name.state == _APPROVAL_REQUIRED, by_name
    _same(by_name, generic)
    assert generic.approval_id is not None and generic.operation_id is not None
    assert middle["approval"] - before["approval"] == 1
    assert after["approval"] - middle["approval"] == 1


def test_audit_and_telemetry_name_the_tool_that_ran(discover: Discover) -> None:
    """A mutate through ``operations_call`` writes one audit row naming the memory
    operation and one telemetry row naming the memory tool; nothing anywhere names
    ``core.tool.call`` or ``operations_call``."""
    _, discovering = discover.pair(_MEMORY_WRITES)
    discover.clear_telemetry()
    audit_before = discover.audit_names()

    written = _remember(discover, discovering, generic=True)
    assert written.ok, written

    new_audit = discover.audit_names()[len(audit_before) :]
    assert new_audit == [MEMORY_REMEMBER]
    assert discover.telemetry_names() == ["recallatron_remember"]
    assert TOOL_CALL not in discover.audit_names()


def test_a_scope_narrowed_after_the_catalogue_still_refuses(
    discover: Discover,
) -> None:
    """The catalogue is not authority: a grant removed after it was read is refused
    at the call, exactly as a direct call is after a listing."""
    _, discovering = discover.pair((MEMORY_RECALL, MEMORY_REMEMBER))
    catalogue = discover.call(discovering, "operations_catalog", {})
    assert "recallatron_remember" in [
        entry.name
        for entry in catalogue.result.tools  # type: ignore[union-attr]
    ]
    narrowed = discover.token((MEMORY_RECALL, TOOL_CALL))
    outcome = _remember(discover, narrowed, generic=True)
    assert outcome.state == _NOT_FOUND, outcome


# --- the origin and role checks, through the generic path -------------------------


def test_a_tool_whose_own_module_is_disabled_is_refused_like_a_direct_call(
    disabled: Discover,
) -> None:
    """``recallatron_forget`` names the core's ``core.record.delete``, whose module
    is always enabled; its *own* module is loaded but not enabled here. The origin
    check is the only thing refusing it, and the generic call must refuse it exactly
    as a direct call does, before any approval work."""
    direct, discovering = disabled.pair((RECORD_DELETE, WORKSPACE_STATUS))
    assert "workspace_status" in disabled.listed(direct), "positive control"
    before = disabled.counts()
    arguments = {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}"}
    by_name = disabled.call(
        direct, "recallatron_forget", arguments, consumers=ConsumerRegistry()
    )
    generic = disabled.generic(
        discovering, "recallatron_forget", arguments, consumers=ConsumerRegistry()
    )
    assert by_name.state == _NOT_FOUND, by_name
    _same(by_name, generic)
    assert disabled.counts() == before
    assert "recallatron_forget" not in disabled.catalogue(discovering)
    assert "workspace_status" in disabled.catalogue(discovering)


def test_a_role_outside_the_operations_roles_is_refused_like_a_direct_call(
    discover: Discover,
) -> None:
    """``core.audit.list`` is owner and operator only. A member's token that holds
    the grant anyway is refused by the role check, through the generic call exactly
    as directly, and the catalogue does not offer the tool."""
    member = add_member(
        discover.cluster.backend, discover.workspace, Role.MEMBER, display_name="m"
    )
    direct, discovering = discover.pair(
        (AUDIT_LIST, WORKSPACE_STATUS), account_id=member
    )
    assert discovering.role is Role.MEMBER
    by_name = discover.call(direct, "audit_list", {})
    generic = discover.generic(discovering, "audit_list", {})
    assert by_name.state == _NOT_FOUND, by_name
    _same(by_name, generic)
    assert "audit_list" not in discover.catalogue(discovering)
    assert "workspace_status" in discover.catalogue(discovering)

    owner_direct, owner_discovering = discover.pair((AUDIT_LIST,))
    assert discover.call(owner_direct, "audit_list", {}).ok, "positive control"
    assert discover.generic(owner_discovering, "audit_list", {}).ok
