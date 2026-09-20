"""``core.record.delete`` end to end against a synthetic owned type.

Seams: the pre-mint authorization (a reference core will not delete never creates an
approval); the coordinator's own re-authorization under the reconstructed original
caller and again under the workspace lifecycle lock; the atomic commit of the owner's
delete, the participants, the content-free ledger row, the ``core.record.deleted``
event and the gated operation's success audit row; and the rollback that takes all five
when any one of them fails.

**Nothing here drives the coordinator directly.** Every deletion below goes through
``dispatch(ctx, "core.record.delete", ...)`` and then ``core.approval.approve``,
because the properties under test are properties of that path: the hold, the guards,
the rebuilt caller, and one transaction around the lot. A test that called
``delete_owned`` itself would prove the function works and nothing about the operation.

**The counts are read off the ledger row and the surviving rows, never off the
handler's return.** The operation answers ``{deletion_ref}`` and nothing else — that is
the disclosure rule, not an omission — so "did the closure actually go" is answered by
looking for the rows, and "what does the ledger claim" by reading the row. A test that
trusted a returned count would pass against a coordinator that deleted nothing and
reported confidently.
"""

import json
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.deletion import (
    MODE_PARTICIPANT_RAISES,
    PARTICIPANT_FAILURE,
    PROBE_UNAUTHORIZED,
    ProbeRow,
    ensure_probe_tables,
    probe_exists,
    probe_ref,
    register_deletion_harness,
    surviving_memory_ids,
    write_probe,
)
from harness.registry import (
    NOTE_WRITE,
    add_member,
    enable_harness_module,
    register_harness,
)
from rheo_contracts import EventEnvelope, RecordRef, Role, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE
from rheo_core.audit import CORE_AUDIT_SINK
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import (
    RECORD_NOT_DELETABLE,
    USER_ERASURE,
    DeletionRecordRow,
    RemovedMemories,
    deletion_record,
    get_deletion_record,
)
from rheo_core.deletion.operations import (
    RECORD_DELETE,
    RECORD_DELETED,
    RecordDeleted,
)
from rheo_core.events import ConsumerRegistry, ConsumerSubscription
from rheo_core.operations import (
    HARNESS_MODULE_ID,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.refs import uuid7
from rheo_core.storage import work_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from sqlalchemy import Engine, func, select

pytestmark = pytest.mark.postgres

DELETED_CONSUMER_ID = "harness.on_record_deleted"


def _never_delivered(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """A subscriber that nothing in this file ever runs.

    The fan-out writes a ``core.event_delivery`` row inside the publishing transaction
    and the worker is what invokes a handler later, so a subscription is enough to
    prove the publish reached the registry the composition root built. Raising here
    rather than passing makes that explicit: if any test ever drains a delivery, it
    will say so loudly instead of quietly proving nothing.
    """
    raise AssertionError("no test in this file drains a delivery")


def _consumers() -> ConsumerRegistry:
    """The composition root's registry, as this file's own composition root.

    ``apps/core``'s ``startup.py`` builds one ``CONSUMERS`` and both HTTP dispatch
    call sites pass it; a test driving ``dispatch()`` directly is the composition root
    and has to do the same. Built per call rather than once at import, so no test can
    observe another's subscriptions.
    """
    registry = ConsumerRegistry()
    registry.register(
        ConsumerSubscription(
            consumer_id=DELETED_CONSUMER_ID,
            event_type=RECORD_DELETED,
            module_id=HARNESS_MODULE_ID,
            replay_safe=True,
            handler=_never_delivered,
        )
    )
    return registry


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    """The shipped core operations and the suite's two harness registrars, all
    idempotent."""
    register_core_operations()
    register_harness()
    register_deletion_harness()


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    return cluster.backend.pools.engine_for(database)


@pytest.fixture
def owner(
    workspace: UUID, owner_account_id: UUID, engine: Engine, database: str
) -> WorkspaceContext:
    """An owner context over a workspace whose ``harness`` module is enabled.

    The module row is what puts ``harness`` in ``enabled_modules``, and the owned-delete
    authorizer refuses a reference whose owning module is not there — so without it
    every deletion below would refuse ``record_not_deletable`` for the wrong reason.
    """
    with UnitOfWork(engine, database) as uow:
        enable_harness_module(uow.connection)
        ensure_probe_tables(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


@pytest.fixture
def probe(owner: WorkspaceContext, engine: Engine, database: str) -> ProbeRow:
    """A probe with two of its own memory rows and two derived ones."""
    with UnitOfWork(engine, database) as uow:
        row = write_probe(uow.connection)
        uow.commit()
    return row


def _delete_payload(record: RecordRef) -> dict[str, object]:
    """``core.record.delete``'s payload as a caller sends it: the canonical string."""
    return {"ref": record.format()}


def _held(ctx: WorkspaceContext, record: RecordRef) -> UUID:
    """Dispatch a delete, assert it was held, and answer the approval id."""
    outcome = dispatch(ctx, RECORD_DELETE, _delete_payload(record))
    assert outcome.state == "approval_required", outcome
    assert outcome.approval_id is not None, outcome
    return outcome.approval_id


def _approve(ctx: WorkspaceContext, approval_id: UUID) -> OperationOutcome:
    """Approve, carrying a registry the way a real composition root does.

    The registry reaches the *gated* handler through ``execute_approved``, which builds
    the deletion coordinator's ``HandlerUnitOfWork`` from the one
    ``core.approval.approve``'s own dispatch was given.
    """
    return dispatch(
        ctx,
        APPROVAL_APPROVE,
        {"approval_id": str(approval_id)},
        consumers=_consumers(),
    )


def _approve_unwired(ctx: WorkspaceContext, approval_id: UUID) -> OperationOutcome:
    """Approve from a composition root that wired no ``ConsumerRegistry`` at all."""
    return dispatch(ctx, APPROVAL_APPROVE, {"approval_id": str(approval_id)})


def _approval_count(engine: Engine, database: str) -> int:
    """Rows in ``core.approval``, read directly.

    A direct count rather than a supported read, because what this file needs to know
    is that **nothing was minted** and release one publishes no "list approvals"
    operation to ask that of.
    """
    with UnitOfWork(engine, database) as uow:
        from rheo_core.approvals.tables import approval

        return int(
            uow.connection.execute(
                select(func.count()).select_from(approval)
            ).scalar_one()
        )


def _ledger_rows(engine: Engine, database: str) -> int:
    with UnitOfWork(engine, database) as uow:
        return int(
            uow.connection.execute(
                select(func.count()).select_from(deletion_record)
            ).scalar_one()
        )


def _deleted_events(engine: Engine, database: str) -> tuple[dict[str, Any], ...]:
    """Every ``core.record.deleted`` outbox row, oldest first, as ``(source, data)``."""
    with UnitOfWork(engine, database) as uow:
        rows = uow.connection.execute(
            select(
                work_tables.outbox_event.c.source,
                work_tables.outbox_event.c.subject_ref,
                work_tables.outbox_event.c.subject_revision,
                work_tables.outbox_event.c.schema_version,
                work_tables.outbox_event.c.correlation_id,
                work_tables.outbox_event.c.data,
            )
            .where(work_tables.outbox_event.c.type == RECORD_DELETED)
            .order_by(work_tables.outbox_event.c.position)
        )
        return tuple(
            {
                "source": str(row.source),
                "subject_ref": str(row.subject_ref),
                "subject_revision": int(row.subject_revision),
                "schema_version": int(row.schema_version),
                "correlation_id": row.correlation_id,
                "data": dict(row.data),
            }
            for row in rows
        )


def _deliveries(engine: Engine, database: str) -> int:
    """``core.event_delivery`` rows for this file's subscriber.

    What proves the publish went through the registry the caller supplied rather than
    through some registry the coordinator built for itself: a throwaway one would have
    no subscriber and would write no delivery row while still writing the outbox row.
    """
    with UnitOfWork(engine, database) as uow:
        return int(
            uow.connection.execute(
                select(func.count())
                .select_from(work_tables.event_delivery)
                .where(work_tables.event_delivery.c.consumer_id == DELETED_CONSUMER_ID)
            ).scalar_one()
        )


def _ledger(engine: Engine, database: str, reference: str) -> DeletionRecordRow:
    """The ledger row the deletion reference names."""
    with UnitOfWork(engine, database) as uow:
        row = get_deletion_record(
            uow.connection, deletion_id=RecordRef.parse(reference).id
        )
    assert row is not None, f"no core.deletion_record row for {reference}"
    return row


def _survivors(engine: Engine, database: str, row: ProbeRow) -> tuple[bool, int]:
    with UnitOfWork(engine, database) as uow:
        return (
            probe_exists(uow.connection, row.id),
            len(surviving_memory_ids(uow.connection, row.id)),
        )


# --- the happy path: one commit, one ledger row, one event ----------------------------


def test_an_approved_delete_removes_the_closure_and_leaves_one_ledger_row(
    owner: WorkspaceContext, engine: Engine, database: str, probe: ProbeRow
) -> None:
    """The whole coordinator in one pass.

    The owner's two rows and the participant's two go together, which is what makes the
    count four rather than two: a coordinator that ran ``delete_owned`` and skipped the
    participants would leave the derived rows behind and claim two. The three
    phase-three counters are asserted to be exactly zero, because this tree implements
    none of that cleanup and a non-zero would be a claim about work nobody did.
    """
    approval_id = _held(owner, probe_ref(probe.id))

    outcome = _approve(owner, approval_id)

    assert outcome.ok, outcome
    assert _survivors(engine, database, probe) == (False, 0)
    assert _ledger_rows(engine, database) == 1
    (event,) = _deleted_events(engine, database)
    row = _ledger(engine, database, str(event["data"]["deletion_record_ref"]))
    assert row.record_type == "harness.probe"
    assert row.record_id == probe.id
    assert row.participants == ("harness",)
    assert (
        row.cancelled_job_count,
        row.cancelled_action_count,
        row.removed_export_count,
        row.invalidated_memory_count,
    ) == (0, 0, 0, len(probe.memory_ids))
    assert row.cause == USER_ERASURE
    assert row.retained_successor_ref is None
    assert row.approval_id == approval_id
    assert row.actor_kind == owner.actor.kind.value
    assert row.actor_id == owner.actor.id


def test_the_published_event_names_the_record_and_the_ledger_row_and_nothing_else(
    owner: WorkspaceContext, engine: Engine, database: str, probe: ProbeRow
) -> None:
    """``core.record.deleted`` carries ``(ref, deletion_record_ref)`` — exactly the two
    fields the ratified event table gives it — published once, from the core segment.

    Its ``data`` is checked key by key rather than for "contains a ref", because the
    rule that makes an event safe to fan out is that it carries references and no
    content; a third field would be the first place content could arrive.
    """
    reference = probe_ref(probe.id)

    _approve(owner, _held(owner, reference))

    (event,) = _deleted_events(engine, database)
    assert event["source"] == "urn:rheo:module:core"
    assert event["subject_ref"] == reference.format()
    assert event["subject_revision"] == 1
    assert event["schema_version"] == 1
    assert set(event["data"]) == {"ref", "deletion_record_ref"}
    assert event["data"]["ref"] == reference.format()
    assert str(event["data"]["deletion_record_ref"]).startswith("core.deletion_record:")
    # The event's own body is JSON-serialisable and holds no probe label: the record's
    # only appearance in it is as an identifier.
    assert probe.label not in json.dumps(event["data"])
    # Fanned out through the caller's registry, not a throwaway one.
    assert _deliveries(engine, database) == 1


def test_a_deployment_with_no_consumer_registry_refuses_and_deletes_nothing(
    owner: WorkspaceContext, engine: Engine, database: str, probe: ProbeRow
) -> None:
    """An absent registry is a deployment bug, refused — never silently degraded.

    The alternative the ratified rule rules out is a per-call ``ConsumerRegistry()``,
    which would let the deletion commit while its event fanned out to nobody: the
    record would be gone, the ledger would say so, and every subscriber would be
    unaware. So the refusal has to come *before* the effect, which is what the
    surviving rows here assert.
    """
    approval_id = _held(owner, probe_ref(probe.id))

    outcome = _approve_unwired(owner, approval_id)

    assert outcome.state == "consumers_missing", outcome
    assert _survivors(engine, database, probe) == (True, len(probe.memory_ids))
    assert _ledger_rows(engine, database) == 0
    assert _deleted_events(engine, database) == ()


def test_the_operations_output_carries_the_deletion_reference_and_no_count() -> None:
    """Every caller, owner included, receives ``{deletion_ref}`` only.

    Asserted on the declared output model rather than on one response, because the rule
    is about what the operation *can* return: a count added to this model would leak the
    closure's size to every caller of every deletion at once, and no single response
    would look wrong.
    """
    assert set(RecordDeleted.model_fields) == {"deletion_ref"}


# --- the pre-mint authorization: nothing is minted for a reference core will not
# --- delete ---------------------------------------------------------------------------


def test_an_unowned_record_type_refuses_before_an_approval_is_minted(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """``harness.note`` resolves and has an operation of its own, and no owned-delete
    declaration — so deleting one is refused generically, before anything is written.

    The count of ``core.approval`` rows is the assertion that matters. A coordinator
    that refused at *execution* instead would also answer a refusal to the caller, and
    would leave an approval sitting in the workspace for somebody to release against a
    record type core can never delete.
    """
    written = dispatch(owner, NOTE_WRITE, {"body": "not a deletable type"})
    assert written.ok, written
    note_reference = RecordRef.parse(str(written.result.ref))  # type: ignore[union-attr]
    before = _approval_count(engine, database)

    outcome = dispatch(owner, RECORD_DELETE, _delete_payload(note_reference))

    assert outcome.state == RECORD_NOT_DELETABLE, outcome
    assert outcome.approval_id is None and outcome.operation_id is None
    assert _approval_count(engine, database) == before


def test_an_unauthorized_target_of_an_owned_type_refuses_before_a_mint(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """The owner's *own* authorizer refuses, and its refusal reaches the caller
    unchanged.

    The distinct state is the point: ``record_not_deletable`` would mean core never
    found an owner, and this case is the opposite — the owner was found, ran, and said
    no. Nothing is minted either way.
    """
    before = _approval_count(engine, database)

    outcome = dispatch(owner, RECORD_DELETE, _delete_payload(probe_ref(uuid7())))

    assert outcome.state == PROBE_UNAUTHORIZED, outcome
    assert outcome.approval_id is None and outcome.operation_id is None
    assert _approval_count(engine, database) == before


def test_a_workspace_without_the_owning_module_enabled_refuses_generically(
    cluster: ClusterSession, make_workspace: Any, owner_account_id: UUID
) -> None:
    """The enabled-module gate, in the one workspace that has no ``harness`` row.

    Its refusal is the *generic* one and carries no hint that the type exists
    elsewhere, which is what stops ``core.record.delete`` being a probe for which
    modules a deployment runs.
    """
    elsewhere = make_workspace()
    ctx = context_for_harness(elsewhere, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx

    outcome = dispatch(ctx, RECORD_DELETE, _delete_payload(probe_ref(uuid7())))

    assert outcome.state == RECORD_NOT_DELETABLE, outcome
    assert outcome.approval_id is None


def test_a_malformed_reference_is_refused_as_an_input_before_authorization(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """A string that is not a canonical reference never reaches the registry.

    ``RecordDeleteInput`` parses the wire form in a validator, so a malformed one is
    ``input_invalid`` from pydantic rather than a refusal invented further down — and
    the payload's own bytes never reach a database.
    """
    before = _approval_count(engine, database)

    outcome = dispatch(owner, RECORD_DELETE, {"ref": "not-a-reference"})

    assert outcome.state == "input_invalid", outcome
    assert _approval_count(engine, database) == before


# --- execution runs as the held caller ------------------------------------------------


def test_the_ledger_names_the_held_caller_not_the_approver(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    probe: ProbeRow,
) -> None:
    """A member holds the deletion and the owner releases it; the ledger names the
    member.

    ``core.deletion_record.actor_*`` is the record of who erased the data, and the
    coordinator reads it off the context it runs under — so this is the ledger's half of
    "the approver decides, the original caller acts". Before 1a1 that context was the
    approver's and this row would have named the owner.
    """
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="held-caller"
    )
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    assert member_id != owner_account_id

    approval_id = _held(member, probe_ref(probe.id))
    assert _approve(owner, approval_id).ok

    (event,) = _deleted_events(engine, database)
    row = _ledger(engine, database, str(event["data"]["deletion_record_ref"]))
    assert row.actor_id == member_id


# --- rollback: any failure takes the effect, the ledger and the event with it ---------


def test_a_participant_that_raises_rolls_back_the_effect_the_ledger_and_the_event(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """The record survives untouched and the operation reports the participant's
    failure.

    All four assertions are needed and none implies another: the probe row is what the
    owner already deleted before the participant ran, the memory rows are the owner's
    own closure, the ledger row is written after both, and the event is written last.
    A coordinator with a second commit boundary anywhere in that sequence would leave
    one of the four behind.
    """
    with UnitOfWork(engine, database) as uow:
        row = write_probe(uow.connection, mode=MODE_PARTICIPANT_RAISES)
        uow.commit()
    approval_id = _held(owner, probe_ref(row.id))

    outcome = _approve(owner, approval_id)

    assert not outcome.ok, outcome
    assert outcome.error is not None
    assert PARTICIPANT_FAILURE not in outcome.error.error_text
    assert _survivors(engine, database, row) == (True, len(row.memory_ids))
    assert _ledger_rows(engine, database) == 0
    assert _deleted_events(engine, database) == ()


def test_an_audit_failure_rolls_back_the_effect_the_ledger_and_the_event(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    probe: ProbeRow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success audit row is in the same transaction as everything else.

    The injection is on the sink's own ``record``, narrowed to this one operation so
    that ``core.approval.approve``'s own audit row still writes and the failure is
    unambiguously the gated operation's. AC 23's property is that a mutation and its
    audit record commit together or neither does; this is that property for a deletion,
    where "neither" has to include the ledger row and the published event.
    """
    original = type(CORE_AUDIT_SINK).record

    def failing(self: Any, ctx: Any, uow: Any, **keywords: Any) -> None:
        if keywords["operation"] == RECORD_DELETE:
            raise RuntimeError("the audit row could not be written")
        original(self, ctx, uow, **keywords)

    approval_id = _held(owner, probe_ref(probe.id))
    monkeypatch.setattr(type(CORE_AUDIT_SINK), "record", failing)

    outcome = _approve(owner, approval_id)

    assert not outcome.ok, outcome
    assert _survivors(engine, database, probe) == (True, len(probe.memory_ids))
    assert _ledger_rows(engine, database) == 0
    assert _deleted_events(engine, database) == ()


def test_a_second_approval_of_the_same_deletion_runs_nothing_twice(
    owner: WorkspaceContext, engine: Engine, database: str, probe: ProbeRow
) -> None:
    """One approval, one deletion, one ledger row, one event.

    The second approve refuses on the approval's own state, so the coordinator is never
    re-entered — which is what keeps the ledger from growing a second row for a record
    that was deleted once.
    """
    approval_id = _held(owner, probe_ref(probe.id))
    assert _approve(owner, approval_id).ok

    second = _approve(owner, approval_id)

    assert not second.ok, second
    assert _ledger_rows(engine, database) == 1
    assert len(_deleted_events(engine, database)) == 1


# --- the union that the ledger's count is ---------------------------------------------


def test_removed_memories_union_counts_a_shared_row_once() -> None:
    """The distinctness rule, at the seam that implements it.

    ``invalidated_memory_count`` is the size of the union across the owner and every
    participant, so two participants that removed rows from one overlapping set count
    the overlap once. Unit-level rather than end to end because the shipped closure has
    no overlap to produce — the harness owner takes the undeled rows and the harness
    participant the derived ones — and a harness rigged to report a row it did not
    remove would be testing a lie the contract forbids anyone from telling.
    """
    shared = uuid7()
    first = RemovedMemories(frozenset({shared, uuid7()}))
    second = RemovedMemories(frozenset({shared, uuid7()}))

    assert len(first.union(second).memory_ids) == 3
    assert first.union(second) == second.union(first)
