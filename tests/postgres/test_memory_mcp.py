"""AC 9 and § A14: the seven Recallatron tools, over the real tool-origin facade.

Seams under test: ``apps/mcp``'s ``list_tools``/``call_tool`` pair — not the core
facade underneath it, so the surface an agent actually reaches is the one asserted —
driven against the registries ``loaded_probe_modules`` built, exactly as
``tests/postgres/test_memory_lifecycle.py`` drives ``dispatch`` against them.

**Four things this file is here to pin, each of which the façade got wrong before.**

1. **Origin, not the operation's module.** ``recallatron_forget`` names the core
   deletion operation, whose module is always enabled, so the listing filter that
   derived a module id from the *operation name* kept advertising it in a workspace
   where Recallatron was disabled — and a call to it reached the coordinator. The
   disabled-module cases below are the ones that catch that, and they are written as
   a pair (listing *and* call) because the listing alone would go green again the
   moment someone re-added a filter and forgot the call.
2. **A listing is not authority.** The scope and role cases list first, change the
   one thing, and call the name the listing returned.
3. **The tool's own input model restricts the call.** A ``recallatron_forget`` whose
   reference is unparseable, or parses and names another module's record, is
   ``input_invalid`` with **no approval row** — asserted against the table, because
   the outcome state alone cannot tell "refused before the hold" from "refused
   inside it".
4. **Every description names its own material failure modes.** Driven from
   :data:`_MATERIAL_REFUSALS` below, so an operation that gains a refusal state
   reddens here instead of leaving a stale sentence in the declaration.

**The core's registrations go into the loaded registries, not beside them.** A
deployment has one operation table and one tool table, each holding the core's own
and every loaded module's; ``loaded_probe_modules`` hands out fresh local ones
holding only the module's. ``register_core_operations(surfaces.operations)`` and
``register_core_tools(surfaces.tools)`` put the pairs back together. The first is
what lets ``recallatron_forget`` resolve its delegate at all — and the fixture
asserts it, because a facade that could not find the operation would hide the tool
for the *right* reason by accident and every absence assertion below would pass for
the wrong one. The second is what gives every absence assertion a positive control:
``workspace_status`` stays listed while the seven are gone, so "none of the seven"
cannot be satisfied by a listing that had stopped returning anything.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_app_mcp.session import resolve_context
from rheo_app_mcp.tools import call_tool, list_tools
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import tables as approval_tables
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.deletion.operations import RECORD_DELETE
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE, WORKSPACE_STATUS
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage import control_tables as control
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.tokens.sets import register_core_tools
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.entities import ENTITY_GET, ENTITY_LIST
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_DERIVE,
    MEMORY_READ,
    MEMORY_RECALL,
    MEMORY_REMEMBER,
    MEMORY_SUPERSEDE,
)
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.tools import TOOLS
from sqlalchemy import delete, func, select

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_NOT_FOUND: Final = "not_found"
_INPUT_INVALID: Final = "input_invalid"

_SEVEN: Final[tuple[str, ...]] = (
    "recallatron_correct",
    "recallatron_derive",
    "recallatron_forget",
    "recallatron_read",
    "recallatron_recall",
    "recallatron_remember",
    "recallatron_supersede",
)
"""The exact set § A10's table gives, sorted, written out rather than derived from
``MANIFEST.tools``. Deriving it would make "exactly seven, and these seven" a
tautology over whatever the manifest happens to declare, which is the claim under
test rather than the premise of it."""

_MATERIAL_REFUSALS: Final[dict[str, frozenset[str]]] = {
    "recallatron_recall": frozenset(
        {"input_invalid", "purpose_mismatch", "reference_scan_limit"}
    ),
    "recallatron_read": frozenset(
        {
            "input_invalid",
            "purpose_mismatch",
            "not_found",
            "container_membership_required",
            "window_scan_limit",
            "reference_scan_limit",
        }
    ),
    "recallatron_remember": frozenset(
        {
            "input_invalid",
            "audience_unavailable",
            "purposes_empty",
            "purpose_mismatch",
            "use_derive",
            "not_found",
            "reference_scan_limit",
        }
    ),
    "recallatron_derive": frozenset(
        {
            "input_invalid",
            "audience_unavailable",
            "audience_empty",
            "purposes_empty",
            "purpose_mismatch",
            "use_derive",
            "not_found",
            "reference_scan_limit",
        }
    ),
    "recallatron_correct": frozenset(
        {"input_invalid", "not_found", "record_stale", "reference_scan_limit"}
    ),
    "recallatron_supersede": frozenset(
        {"input_invalid", "not_found", "record_stale", "reference_scan_limit"}
    ),
    "recallatron_forget": frozenset(
        {"input_invalid", "not_found", "approval_required"}
    ),
}
"""Per tool, the refusal states its own operation can actually answer with, from the
applicable set § A10 names.

Each entry is a claim about the handler, not about the sentence: ``use_derive`` is on
both writers because ``writes.py`` raises it from ``resolve_stated_links`` and from
``resolve_sources``' ``about_refs`` loop; ``audience_empty`` is on ``derive`` alone
because only a source meet can be empty; ``record_stale`` is on the two lifecycle
changes alone because only they carry a compare-and-set; and ``approval_required`` is
on ``forget`` alone because only its operation is destructive. ``retention_unavailable``
is reachable from all six module operations and is deliberately absent — it is not in
the set § A10 enumerates, and a table that quietly grew past its own source would stop
being checkable against it.
"""

_TOOL_OPERATIONS: Final[dict[str, str]] = {
    "recallatron_recall": MEMORY_RECALL,
    "recallatron_read": MEMORY_READ,
    "recallatron_remember": MEMORY_REMEMBER,
    "recallatron_derive": MEMORY_DERIVE,
    "recallatron_correct": MEMORY_CORRECT,
    "recallatron_supersede": MEMORY_SUPERSEDE,
    "recallatron_forget": RECORD_DELETE,
}
"""§ A10's operation column, against the constants each side declares. ``forget``'s
entry is the core's own ``RECORD_DELETE``, which is the check that the assembled name
in ``rheo_recallatron.tools`` still equals it: that module may not write a
``<reserved-segment>.<name>`` literal, so this is where the two are held together."""


# --- the workspace ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolWorkspace:
    """Recallatron loaded, with the core operations in the same registry."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str

    def context(
        self, *, account_id: UUID | None = None, role: Role = Role.OWNER
    ) -> WorkspaceContext:
        ctx = context_for_harness(
            self.workspace,
            self.owner_account_id if account_id is None else account_id,
            role,
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def listed(self, ctx: WorkspaceContext) -> tuple[str, ...]:
        return tuple(
            sorted(
                tool.name
                for tool in list_tools(
                    ctx, tools=self.surfaces.tools, registry=self.surfaces.operations
                )
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

    def counts(self) -> dict[str, int]:
        """The three tables a held destructive call would write into."""
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        tables = {
            "approval": approval_tables.approval,
            "operation": work_tables.operation,
            "audit_record": work_tables.audit_record,
        }
        with UnitOfWork(engine, self.database_name) as uow:
            return {
                name: int(
                    uow.connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for name, table in tables.items()
            }


@contextmanager
def _recallatron(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    *,
    enable: bool,
) -> Iterator[ToolWorkspace]:
    """Recallatron loaded, and optionally installed and enabled in ``workspace``.

    ``enable=False`` is the disabled case the origin check exists for, and it is a
    *loaded* module rather than an absent one on purpose: the tools are registered
    and their operations resolve, so the only reason any of the seven is invisible is
    that the workspace has not enabled the module that registered them.
    """
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
        assert surfaces.operations.lookup(RECORD_DELETE) is not None, (
            "the loaded registry must hold the core deletion operation, or every "
            "absence assertion below passes for the wrong reason"
        )
        if enable:
            bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
            assert isinstance(bootstrap, WorkspaceContext), bootstrap
            install_and_enable_module(
                cluster.backend, bootstrap, workspace, _MEMORY_MODULE
            )
        row = cluster.registry_row(workspace)
        yield ToolWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
        )


@pytest.fixture
def memory_tools(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[ToolWorkspace]:
    with _recallatron(
        monkeypatch, cluster, workspace, owner_account_id, enable=True
    ) as loaded:
        yield loaded


@pytest.fixture
def disabled_tools(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[ToolWorkspace]:
    with _recallatron(
        monkeypatch, cluster, workspace, owner_account_id, enable=False
    ) as loaded:
        yield loaded


# --- exactly seven, and which seven ----------------------------------------------


def test_exactly_the_seven_declared_tools_are_discoverable(
    memory_tools: ToolWorkspace,
) -> None:
    """An enabled module and a fully permitted owner see seven, and these seven.

    No eighth, and neither entity read: § A10 gives ``recallatron.entity.list`` and
    ``.get`` "none" in the MCP column, and both operations are registered in the same
    registry this listing is filtered against — so their absence is the façade's
    doing rather than an artefact of nothing having registered them.

    The whole listing is asserted, not only this module's slice, because "no eighth"
    has to be a claim about what the surface returns. ``workspace_status`` is the
    core's own and is expected beside the seven; ``harness_get_note`` is registered
    as a declaration here and is absent because its operation is not, which is the
    delegate check doing its job on a tool nobody disabled.
    """
    ctx = memory_tools.context()
    listed = memory_tools.listed(ctx)
    assert listed == tuple(sorted((*_SEVEN, "workspace_status")))
    assert tuple(name for name in listed if name.startswith("recallatron_")) == _SEVEN
    assert "harness_get_note" in memory_tools.surfaces.tools.names()
    registered = memory_tools.surfaces.operations.names()
    assert {ENTITY_LIST, ENTITY_GET} <= registered, (
        "the entity operations must be registered for their absence to mean anything"
    )


def test_each_tool_names_the_operation_the_ratified_table_gives_it() -> None:
    """The delegate column, including ``forget``'s core-owned one.

    Read off the declarations rather than a registry, because the pairing is a
    property of what this module declares and holds without a workspace. The
    ``forget`` row is the one with work to do: ``rheo_recallatron.tools`` may not
    write a ``<reserved-segment>.<name>`` literal, so this is where its assembled
    name is held against the core's own constant.
    """
    assert {tool.name: tool.operation for tool in TOOLS} == _TOOL_OPERATIONS


def test_no_declared_tool_is_absent_from_the_manifest(
    memory_tools: ToolWorkspace,
) -> None:
    """What the loader registered is what the manifest declares, and nothing else."""
    assert tuple(sorted(tool.name for tool in MANIFEST.tools)) == _SEVEN
    assert {tool.name for tool in TOOLS} == set(_SEVEN)


# --- the module's own state gates every one of the seven --------------------------


def test_a_disabled_module_hides_all_seven_including_the_core_backed_tool(
    disabled_tools: ToolWorkspace,
) -> None:
    """None of the seven is discoverable while Recallatron is disabled.

    ``recallatron_forget`` is the case with teeth. Its operation is core-owned, so a
    filter that asks "is the *operation's* module enabled" answers yes in every
    workspace; only the tool's registered origin says otherwise. The positive control
    is the core tool beside it, which must still be listed — otherwise this assertion
    would also pass against a façade that had simply stopped listing anything.
    """
    ctx = disabled_tools.context()
    listed = disabled_tools.listed(ctx)
    assert set(listed).isdisjoint(_SEVEN), listed
    assert "workspace_status" in listed


def test_a_disabled_module_refuses_the_core_backed_tool_before_any_approval(
    disabled_tools: ToolWorkspace,
) -> None:
    """And the call refuses too, writing nothing.

    ``not_found`` rather than the deletion coordinator's own refusal: reaching the
    coordinator at all would mean the tool's origin was never checked on the call,
    and the state it answers with would confirm both that the reference parsed and
    that no module here owns it.
    """
    ctx = disabled_tools.context()
    before = disabled_tools.counts()
    outcome = disabled_tools.call(
        ctx, "recallatron_forget", {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}"}
    )
    assert outcome.state == _NOT_FOUND, outcome
    assert disabled_tools.counts() == before


def test_an_unloaded_module_declares_no_tools_at_all(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """Module absent: the process-wide registry never held any of the seven.

    The positive control is the core tool set, which is registered here, so this is
    "the seven are absent" rather than "the registry is empty".
    """
    register_core_operations()
    register_core_tools()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    listed = tuple(sorted(tool.name for tool in list_tools(ctx)))
    assert set(listed).isdisjoint(_SEVEN), listed
    assert "workspace_status" in listed


# --- a cached listing is not authority --------------------------------------------


def _issue_mcp_token(
    operator_ctx: WorkspaceContext, *, account_id: UUID, operations: list[str]
) -> tuple[str, UUID]:
    outcome = dispatch(
        operator_ctx,
        TOKEN_ISSUE,
        {
            "kind": "mcp",
            "operations": operations,
            "account_id": str(account_id),
        },
    )
    assert outcome.ok, outcome
    result = outcome.result
    assert result is not None
    return result.value, result.token_id  # type: ignore[attr-defined]


def test_revoking_the_scope_between_discovery_and_the_call_still_refuses(
    memory_tools: ToolWorkspace,
) -> None:
    """The same name, listed under one scope and called under the narrowed one.

    The token's own snapshot row for ``forget``'s operation is deleted between the
    two — the way a scope actually narrows — and the context is resolved again from
    the same bearer, because a context is the boundary's to build and a test may not
    construct a narrowed one by hand. A façade that dispatched straight from the tool
    registry would answer the operation registry's ``operation_not_permitted`` here
    instead, which is a refusal that confirms the tool exists and that the caller is
    merely unscoped for it.
    """
    operator = context_for_operator(memory_tools.workspace)
    assert isinstance(operator, WorkspaceContext), operator
    value, token_id = _issue_mcp_token(
        operator,
        account_id=memory_tools.owner_account_id,
        operations=[RECORD_DELETE, WORKSPACE_STATUS],
    )

    wide = resolve_context(value)
    assert isinstance(wide, WorkspaceContext), wide
    assert "recallatron_forget" in memory_tools.listed(wide)

    with memory_tools.cluster.backend.control_engine.begin() as connection:
        connection.execute(
            delete(control.access_token_operation).where(
                control.access_token_operation.c.token_id == token_id,
                control.access_token_operation.c.operation_name == RECORD_DELETE,
            )
        )

    narrowed = resolve_context(value)
    assert isinstance(narrowed, WorkspaceContext), narrowed
    assert "recallatron_forget" not in memory_tools.listed(narrowed)
    before = memory_tools.counts()
    outcome = memory_tools.call(
        narrowed, "recallatron_forget", {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}"}
    )
    assert outcome.state == _NOT_FOUND, outcome
    assert memory_tools.counts() == before


def test_a_role_that_cannot_run_the_operation_neither_lists_nor_calls_it(
    memory_tools: ToolWorkspace,
) -> None:
    """Role is checked at discovery and again at the call (§ A10).

    An operator is the role with the widest reach over a deployment and the
    narrowest over a workspace's memories: § A10 gives every one of the seven to
    some combination of owner, member and service, and none of them to ``operator``
    — a memory is a member's record, not an operations capability. So an operator
    context carries the full operation set, has the module enabled, and must still
    see none of the seven.

    A member context would prove nothing here: a member may run all seven. The owner
    listing is taken first, and the call below names one of the tools that listing
    returned, which is what makes this a check on the *call* rather than only on the
    filter.
    """
    owner = memory_tools.context()
    assert "recallatron_correct" in memory_tools.listed(owner)

    operator = context_for_operator(memory_tools.workspace)
    assert isinstance(operator, WorkspaceContext), operator
    assert operator.role is Role.OPERATOR
    assert _MEMORY_MODULE in operator.enabled_modules, (
        "the module must be enabled for the operator, or this tests the wrong thing"
    )
    listed = memory_tools.listed(operator)
    assert set(listed).isdisjoint(_SEVEN), listed
    assert "workspace_status" in listed

    before = memory_tools.counts()
    outcome = memory_tools.call(
        operator,
        "recallatron_correct",
        {
            "ref": f"{_MEMORY_MODULE}.memory:{uuid4()}",
            "expected_revision": 1,
            "title": "a correction this role may not make",
            "body": "and which therefore writes nothing at all",
        },
    )
    assert outcome.state == _NOT_FOUND, outcome
    assert memory_tools.counts() == before


# --- the tool's own input model, before any approval work -------------------------


@pytest.mark.parametrize(
    ("reference", "why"),
    [
        ("not-a-reference", "unparseable"),
        ("recallatron.memory:not-a-uuid", "right shape, wrong id"),
        ("harness.note:11111111-1111-7111-8111-111111111111", "another module"),
        ("recallatron.memory_entity:11111111-1111-7111-8111-111111111111", "our own"),
    ],
    ids=["malformed", "bad-uuid", "foreign-module", "foreign-record-type"],
)
def test_forget_refuses_a_foreign_or_malformed_reference_before_any_approval(
    memory_tools: ToolWorkspace, reference: str, why: str
) -> None:
    """§ A8, asserted against the table rather than only against the outcome.

    The operation's own model accepts **any** canonical reference, because it is the
    coordinator for every owned record type in a deployment; the tool's model accepts
    one shape. Without the pre-dispatch validation the last two cases reach the hold
    and are refused there under a state that reports what core does and does not own,
    which is exactly the probe the generic refusal exists to prevent.
    """
    ctx = memory_tools.context()
    before = memory_tools.counts()
    outcome = memory_tools.call(ctx, "recallatron_forget", {"ref": reference})
    assert outcome.state == _INPUT_INVALID, (why, outcome)
    assert memory_tools.counts()["approval"] == before["approval"] == 0


def test_an_argument_the_tool_does_not_declare_is_refused(
    memory_tools: ToolWorkspace,
) -> None:
    """An advertised schema restricts the call, not only describes it."""
    ctx = memory_tools.context()
    outcome = memory_tools.call(
        ctx,
        "recallatron_forget",
        {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}", "cascade": True},
    )
    assert outcome.state == _INPUT_INVALID, outcome
    assert outcome.error is not None
    assert "cascade" in outcome.error.error_text


def _remember(memory_tools: ToolWorkspace, ctx: WorkspaceContext) -> str:
    """One memory, written through the ``remember`` tool, returning its reference.

    Through the tool rather than the module's repository on purpose: this is the
    only ``mutate`` path in the file, and it is what proves a publishing handler
    reached through this surface finds the registry the caller named.
    """
    outcome = memory_tools.call(
        ctx,
        "recallatron_remember",
        {
            "kind": "note",
            "title": "a memory to erase",
            "body": "written through the tool surface so the erasure has a target",
            "purposes": ["respond"],
        },
        consumers=ConsumerRegistry(),
    )
    assert outcome.ok, outcome
    assert outcome.result is not None
    return str(outcome.result.ref)  # type: ignore[attr-defined]


def test_a_mutating_tool_publishes_through_the_registry_the_caller_named(
    memory_tools: ToolWorkspace,
) -> None:
    """``consumers`` reaches the handler, and its absence is loud rather than silent.

    ``remember`` publishes inside its own transaction, so it reads
    ``uow.consumers`` and refuses ``consumers_missing`` when the dispatch carries
    none. Both halves are asserted here because the passing half alone would stay
    green against a façade that had quietly built a registry of its own per call —
    which is the fan-out bug the argument has no default in order to prevent.
    """
    ctx = memory_tools.context()
    assert _remember(memory_tools, ctx).startswith(f"{_MEMORY_MODULE}.memory:")

    unwired = memory_tools.call(
        ctx,
        "recallatron_remember",
        {
            "kind": "note",
            "title": "a memory with nowhere to publish",
            "body": "and therefore a memory that is never written",
            "purposes": ["respond"],
        },
        consumers=None,
    )
    assert unwired.state == "consumers_missing", unwired


def test_a_well_formed_forget_reaches_the_approval_gate(
    memory_tools: ToolWorkspace,
) -> None:
    """The other side of the same claim, so the refusals above are not vacuous.

    A canonical reference to one of this module's own memories passes the tool's
    model and is held: ``approval_required``, with a row to confirm. Nothing is
    erased — a destructive call never enters its handler on the request itself.
    """
    ctx = memory_tools.context()
    reference = _remember(memory_tools, ctx)
    before = memory_tools.counts()["approval"]
    outcome = memory_tools.call(
        ctx, "recallatron_forget", {"ref": reference}, consumers=ConsumerRegistry()
    )
    assert outcome.state == "approval_required", outcome
    assert outcome.approval_id is not None
    assert memory_tools.counts()["approval"] == before + 1


# --- AC 9: the descriptions ------------------------------------------------------


def test_every_description_names_its_own_material_failure_modes() -> None:
    """Driven from :data:`_MATERIAL_REFUSALS`, one row per tool.

    Read directly off :data:`~rheo_recallatron.tools.TOOLS` rather than through a
    loaded registry: this is a claim about the declarations, and it should hold
    without a database or a workspace.
    """
    assert {tool.name for tool in TOOLS} == set(_MATERIAL_REFUSALS)
    missing: dict[str, list[str]] = {}
    for tool in TOOLS:
        absent = sorted(
            state
            for state in _MATERIAL_REFUSALS[tool.name]
            if state not in tool.description
        )
        if absent:
            missing[tool.name] = absent
    assert not missing, f"tool descriptions omit a refusal they can answer: {missing}"


def test_no_description_claims_a_refusal_its_operation_cannot_answer() -> None:
    """The other direction, over the states this module and the core declare.

    A description that named ``audience_empty`` on ``correct`` would be worse than
    one that named nothing: a client would build a retry path for an answer it can
    never receive. The comparison is against the applicable set § A10 enumerates, so
    ordinary prose is not policed — only the vocabulary.
    """
    applicable = frozenset().union(*_MATERIAL_REFUSALS.values())
    overclaimed: dict[str, list[str]] = {}
    for tool in TOOLS:
        extra = sorted(
            state
            for state in applicable - _MATERIAL_REFUSALS[tool.name]
            if state in tool.description
        )
        if extra:
            overclaimed[tool.name] = extra
    assert not overclaimed, (
        f"tool descriptions claim refusals they cannot answer: {overclaimed}"
    )
