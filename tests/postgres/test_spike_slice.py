"""Run 0v's module slice, driven through ``dispatch()`` and nothing else.

Seams under test: the spike's registration through the real ``rheo.modules`` entry
point; ``spike.note.add`` / ``spike.note.list`` through ``dispatch()`` (input
validation, the read/write split, the audit sink writing exactly one row); the sealed
handler view's refusal of ``commit``; and the one-transaction rule in both directions
— a handler that raises, and a sink that raises after its own write.

**No test here drives an HTTP route, and that is a rule rather than an oversight.**
``POST /internal/v1/operations/{name}`` is C3's and does not exist at this chunk's
green checkpoint, so a route-driven test here would POST to a 404 and the cheap way
out would be to stub the route. Every route-driven test in this run lives in
``tests/postgres/test_internal_operations.py``, written by C3: AC 9's three check-order
refusals, AC 5 and AC 6's boundary cases, AC 13, and the route's 401s.

**Two refusals are deliberately not tested here** (FR 2): ``operation_not_permitted``
is unreachable from a session and already covered by
``tests/postgres/test_api_surface.py::test_operation_not_permitted_403``, and
``context_required`` is unreachable through the route by design and already pinned by
``tests/test_boundary.py``. Reaching either would mean hand-building a
``WorkspaceContext``, which ``tests/test_boundary.py``'s AST scan refuses in every file
outside ``rheo_core/boundary/`` — tests included, and a subclass counts.
"""

from uuid import UUID

import pytest
from conftest import ClusterSession, InstallSpike, MakeWorkspace
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import install_sink, reset_sinks
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.context import WORKSPACE_UNSELECTED, Refusal
from rheo_core.boundary.factories import context_from_session
from rheo_core.operations import OperationRegistry, dispatch
from rheo_core.operations.refusals import FAILED, INPUT_INVALID
from rheo_core.storage.backend import HANDLER_MAY_NOT_COMMIT, UnitOfWork
from rheo_core.storage.repositories import list_module_states
from rheo_spike import devtools
from rheo_spike.audit import SINK
from rheo_spike.operations import (
    MODULE_ID,
    NOTE_ADD,
    NOTE_COMMIT_EARLY,
    NOTE_LIST,
    NoteAdded,
    NoteAddInput,
)
from rheo_spike.records import (
    AuditProbeRow,
    NoteRow,
    list_audit_probes,
    list_notes,
    write_note,
)
from sqlalchemy import Connection

pytestmark = pytest.mark.postgres


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture
def spike_workspace(workspace: UUID, install_spike: InstallSpike) -> UUID:
    """One provisioned workspace with ``modules/spike`` installed and enabled."""
    install_spike(workspace)
    return workspace


@pytest.fixture
def owner(spike_workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(spike_workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _open(cluster: ClusterSession, workspace_id: UUID) -> UnitOfWork:
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    return UnitOfWork(engine, row.database_name)


def _notes(cluster: ClusterSession, workspace_id: UUID) -> tuple[NoteRow, ...]:
    with _open(cluster, workspace_id) as uow:
        return list_notes(uow.connection, limit=200)


def _probes(cluster: ClusterSession, workspace_id: UUID) -> tuple[AuditProbeRow, ...]:
    with _open(cluster, workspace_id) as uow:
        return list_audit_probes(uow.connection)


# --- FR 9 / FR 10: the two real operations, through dispatch --------------------------


def test_the_mutate_operation_writes_a_note_the_read_operation_lists(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    added = dispatch(owner, NOTE_ADD, {"body": "the first note"})
    assert added.ok, added
    assert isinstance(added.result, NoteAdded)
    assert added.result.body == "the first note"
    assert added.result.revision == 1

    listed = dispatch(owner, NOTE_LIST, {})
    assert listed.ok, listed
    assert listed.result is not None
    bodies = [note.body for note in listed.result.notes]  # type: ignore[attr-defined]
    refs = [note.ref for note in listed.result.notes]  # type: ignore[attr-defined]
    assert bodies == ["the first note"]
    assert refs == [added.result.ref]


def test_a_workspace_with_no_notes_lists_an_empty_success(
    spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """A success, not a refusal: the difference matters to the page's empty state."""
    outcome = dispatch(owner, NOTE_LIST, {})
    assert outcome.ok, outcome
    assert outcome.result is not None
    assert outcome.result.notes == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("body", ["", "x" * 4001])
def test_an_empty_or_oversize_body_refuses_input_invalid(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext, body: str
) -> None:
    """``body`` is ``min_length=1, max_length=4000``. The refusal names the field and
    never echoes the value — a 4001-character body in an error string would be the
    payload back out again."""
    outcome = dispatch(owner, NOTE_ADD, {"body": body})

    assert outcome.state == INPUT_INVALID, outcome
    assert outcome.error is not None
    assert "body" in outcome.error.error_text
    if body:
        assert body not in outcome.error.error_text
    assert _notes(cluster, spike_workspace) == ()


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_an_out_of_range_limit_refuses_input_invalid(
    spike_workspace: UUID, owner: WorkspaceContext, limit: int
) -> None:
    outcome = dispatch(owner, NOTE_LIST, {"limit": limit})
    assert outcome.state == INPUT_INVALID, outcome
    assert outcome.error is not None
    assert "limit" in outcome.error.error_text


# --- AC 9's success case: a read runs, and nothing audits it --------------------------


def test_read_class_runs_without_an_audit_spec(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """``spike.note.list`` declares ``audit = None``, so the dispatcher never reaches
    the sink for it.

    In release one "→ safety class" reduces to exactly this: nothing branches on
    ``read`` versus ``mutate`` except whether a declaration carries an ``AuditSpec``
    (finding F14). The ``add`` below is what makes the assertion non-vacuous — it
    proves the probe table is reachable and writable in this very workspace, so the
    empty result after the read is the read's own silence rather than a missing table.
    """
    assert dispatch(owner, NOTE_ADD, {"body": "audited"}).ok
    before = _probes(cluster, spike_workspace)
    assert [probe.operation for probe in before] == [NOTE_ADD]

    assert dispatch(owner, NOTE_LIST, {"limit": 10}).ok

    assert _probes(cluster, spike_workspace) == before


# --- AC 10: the handler cannot end the transaction -----------------------------------


def test_a_committing_handler_persists_nothing(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """``spike.note.commit_early``'s handler writes a note and then calls
    ``uow.commit()`` on the sealed view it was handed."""
    outcome = dispatch(owner, NOTE_COMMIT_EARLY, {"body": "committed too early"})

    assert outcome.state == HANDLER_MAY_NOT_COMMIT, outcome
    assert _notes(cluster, spike_workspace) == ()
    assert _probes(cluster, spike_workspace) == ()


# --- AC 11: the dispatcher writes the audit row, and the module does not -------------


def test_the_dispatcher_writes_the_audit_row(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """Exactly one row, naming the operation and the acting context.

    "Exactly one" is structural rather than incidental: the sink is resolved by the
    operation's owning module id, so no ``core.*`` dispatch anywhere else in this
    process can add a second. ``modules/spike/src/rheo_spike/operations.py`` names the
    sink nowhere; ``tests/test_audit_sink.py::test_only_the_dispatcher_calls_the_sink``
    is the static half of the same claim.
    """
    outcome = dispatch(owner, NOTE_ADD, {"body": "audit me"})
    assert outcome.ok, outcome

    probes = _probes(cluster, spike_workspace)
    assert len(probes) == 1
    probe = probes[0]
    assert probe.operation == NOTE_ADD
    # ``AuditSpec(subject_field=None)``: a create has no input field naming its own
    # subject, which is finding F4 rather than a gap in this module.
    assert probe.subject_ref is None
    assert probe.actor_kind == owner.actor.kind.value
    assert probe.actor_id == owner.actor.id
    assert probe.request_id == owner.request_id


def test_the_sink_writes_whatever_the_dispatcher_hands_it(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """``SpikeAuditSink.record`` carries no operation-name guard, deliberately.

    The scoping belongs to ``dispatch()``'s ``sink_for(operation.module_id)``, so this
    object is only ever handed a ``spike.*`` operation in the first place. A guard here
    would move the decision into module code — the thing FR 4 exists to disprove — and
    would make AC 11's scan a weaker claim than it reads as. Handing the sink a
    ``core.*`` name directly is the only way to observe the absence of that guard, and
    it fails the moment somebody adds one.

    This is a ``.record(...)`` call, which is legal here and nowhere in
    ``packages``/``apps``/``modules``: ``tests/`` is outside that scan's roots.
    """
    with _open(cluster, spike_workspace) as uow:
        SINK.record(owner, uow, operation="core.settings.set", subject_ref=None)
        uow.commit()

    assert [probe.operation for probe in _probes(cluster, spike_workspace)] == [
        "core.settings.set"
    ]


# --- AC 12: one transaction, both halves ---------------------------------------------


class _Exploded(RuntimeError):
    """Raised by the test-local handler below, and never by shipped code."""


def _explode(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteAddInput
) -> NoteAdded:
    """Write a note, then fail — the failure a real handler bug would be."""
    write_note(uow.connection, body=model_input.body)
    raise _Exploded("the handler failed after writing")


EXPLODE_DECLARATION = OperationDeclaration(
    name="spike.note.explode",
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=NoteAddInput,
    output=NoteAdded,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)
"""A fourth ``spike.*`` declaration that exists only inside this file.

It is registered on a **local** ``OperationRegistry``, never the process-wide one, so
the shipped module keeps three operations and ``rheo openapi``'s document (C3) never
carries this name. Registering it under ``origin = "spike"`` is the real registration
path: ``check_origin`` accepts it exactly as it accepts the manifest's own three.
"""


def test_a_failed_handler_rolls_back_its_own_write(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """AC 12(a). This proves the **domain** half only, and cannot prove the audit half:
    the sink runs after the handler returns, so a handler that raises never reaches it.
    ``test_a_failure_after_the_audit_row_rolls_it_back_too`` is the other half."""
    registry = OperationRegistry()
    registry.register(EXPLODE_DECLARATION, _explode, origin=MODULE_ID)

    outcome = dispatch(
        owner, EXPLODE_DECLARATION.name, {"body": "gone"}, registry=registry
    )

    assert outcome.state == FAILED, outcome
    assert outcome.error is not None
    # The exception's class name only: its message is for the log, never the outcome.
    assert outcome.error.error_text == _Exploded.__name__
    assert _notes(cluster, spike_workspace) == ()
    assert _probes(cluster, spike_workspace) == ()


class _WritesThenRaises:
    """A sink that writes its row through the real unit of work and then fails.

    The failure lands **between** ``sink_for(...).record(...)`` and ``uow.commit()``,
    which is the only window in which the audit row exists and the transaction is
    still open.
    """

    def __init__(self) -> None:
        self.called = 0

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        self.called += 1
        SINK.record(ctx, uow, operation=operation, subject_ref=None)
        raise _Exploded("the sink failed after writing its row")


def test_a_failure_after_the_audit_row_rolls_it_back_too(
    cluster: ClusterSession, spike_workspace: UUID, owner: WorkspaceContext
) -> None:
    """AC 12(b): the assertion that proves the audit write is **inside** the handler's
    transaction rather than merely near it.

    ``reset_sinks()`` first, because ``install_spike`` already occupies the ``spike``
    key with the module's own sink and ``install_sink`` refuses a *different* object
    under an occupied key. The real sink is put back afterwards so later tests in this
    file see the module's own writer and not this double.
    """
    sink = _WritesThenRaises()
    reset_sinks()
    install_sink(MODULE_ID, sink)
    try:
        outcome = dispatch(owner, NOTE_ADD, {"body": "neither half survives"})
    finally:
        reset_sinks()
        install_sink(MODULE_ID, SINK)

    assert sink.called == 1
    assert outcome.state == FAILED, outcome
    assert _notes(cluster, spike_workspace) == ()
    assert _probes(cluster, spike_workspace) == ()


# --- FR 7 (reduced): the install command ---------------------------------------------


def _module_states(connection: Connection) -> dict[str, str]:
    return {row.module_id: row.state for row in list_module_states(connection)}


def test_rheo_spike_install_enables_the_module_in_one_workspace(
    cluster: ClusterSession, make_workspace: MakeWorkspace, install_spike: InstallSpike
) -> None:
    """``rheo-spike install`` writes the ``core.module_state`` row, and the next
    context built over that workspace carries ``spike`` in ``enabled_modules``.

    The fixture is requested but **deliberately not called for this workspace**: the
    command has to do the whole job by itself. Requesting it still registers the
    module process-wide, which is what makes the id meaningful at all.
    """
    other = make_workspace()
    install_spike(other)
    target = make_workspace()

    before = context_for_operator(target)
    assert isinstance(before, WorkspaceContext), before
    assert MODULE_ID not in before.enabled_modules

    assert devtools.main(["install", "--workspace", str(target)]) == 0

    with _open(cluster, target) as uow:
        assert _module_states(uow.connection)[MODULE_ID] == "enabled"
    after = context_for_operator(target)
    assert isinstance(after, WorkspaceContext), after
    assert MODULE_ID in after.enabled_modules
    # Idempotent: a second install is a no-op, not an error.
    assert devtools.main(["install", "--workspace", str(target)]) == 0


def test_rheo_spike_session_prints_a_cookie_value_that_resolves(
    capsys: pytest.CaptureFixture[str], owner_account_id: UUID
) -> None:
    """``rheo-spike session`` is C5's way into a browser flow without an OAuth
    provider, so its output has to be a secret the shipped boundary really accepts.

    The assertion is ``workspace_unselected`` rather than a context, and that is the
    contract, not a shortfall: ``create_session`` always writes
    ``active_workspace_id = NULL`` and only ``switch_workspace`` ever sets it, so
    **every** browser session starts here (run 0b2's carried CF1). What this pins is
    that the printed value reaches a real session row — ``session_missing`` is the
    refusal a wrong or unstored secret would give, and it is the one that must not
    appear.
    """
    host = "spike.localhost"
    argv = ["session", "--account", str(owner_account_id), "--host", host]
    assert devtools.main(argv) == 0
    printed = capsys.readouterr().out.strip()

    refusal = context_from_session(bytes.fromhex(printed), host)
    assert isinstance(refusal, Refusal), refusal
    assert refusal.state == WORKSPACE_UNSELECTED


def test_rheo_spike_refuses_under_the_production_profile(
    monkeypatch: pytest.MonkeyPatch, workspace: UUID
) -> None:
    """Both subcommands are development-only. The refusal comes before any database
    work, so no workspace is touched on the way to it."""
    monkeypatch.setenv("RHEO_PROFILE", "production")
    assert devtools.main(["install", "--workspace", str(workspace)]) == 1
