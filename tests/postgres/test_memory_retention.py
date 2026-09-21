"""AC 8: Recallatron's retention sweep, its authority, and its catch-up.

Seams under test: the ``SELECT ... FOR UPDATE SKIP LOCKED`` claim on the one catch-up
schedule row and its **non-blocking** coexistence with ``core.retention_sweep`` in a
single visit; the coalesce/rearm cycle draining a backlog larger than one batch across
several committed transactions; and the non-forgeability of the verified expiry —
including the property the whole disposition split exists for, that a fresh
replacement survives its expired predecessor's sweep.

**Why the sweep is driven through a real worker visit and not by calling the handler.**
Three of the properties here are properties of the *transaction map*: the deletions and
the schedule rearm commit together, a batch that made no progress is requeued under
ordinary backoff rather than finishing green, and the verified capability only exists
inside a leased job. Calling ``run_memory_retention_sweep`` directly would leave all
three unobserved and every assertion below still green.

**Counts and states are read back off the tables.** The expiry answers only its
``deletion_ref``, and the sweep answers nothing at all, so "did it actually go" is
answered by looking for the rows.

This file's own ``core.retention_sweep`` registration is not incidental: it is the
control for every locking case. The claim under test is that one kind is treated
specially and the other is untouched, and a suite that registered only the kind under
test could not tell that apart from a change to the ticker's whole loop.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import MEMORY_EXPIRY_OPERATIONS, context_for_harness
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import OWNED_DELETIONS, RETENTION_EXPIRY, get_deletion_record
from rheo_core.deletion.operations import RECORD_DELETE, RECORD_DELETED
from rheo_core.deletion.tables import deletion_record
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.settings import ValueType
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.storage import core_tables, work_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.jobs import enqueue_job, request_cancellation
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_core.work.scheduled_authority import (
    CATCH_UP_JOB_KIND,
    SCHEDULED_AUTHORITY_INVALID,
    VerifiedScheduledExecution,
    dispatch_memory_expiry_in,
)
from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    RetentionSweepPayload,
    earliest_schedule_due_at,
    run_due_schedules,
    run_retention_sweep,
)
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import (
    MEMORY_RECORD_TYPE,
    RETENTION_DAYS_DEFAULT,
    RETENTION_DAYS_KEY,
    RETENTION_DAYS_SPEC,
)
from rheo_recallatron.contracts import MemorySuperseded
from rheo_recallatron.eligibility import memory_reference
from rheo_recallatron.operations import MEMORY_SUPERSEDE
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.retention import (
    MEMORY_RECORD_QUALIFIED,
    MEMORY_RETENTION_SWEEP,
    SWEEP_AUTHORITY_MISSING,
    SWEEP_BATCH_LIMIT,
    SWEEP_NO_PROGRESS,
    MemoryRetentionSweepPayload,
    run_memory_retention_sweep,
)
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryPurposeRow,
    MemoryRow,
    get_memory,
    insert_memory,
    insert_memory_link,
    insert_memory_purpose,
    list_expired_memory_roots,
)
from sqlalchemy import Engine, func, select, text, update

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_OWNER = "memory-retention-test"
_RESPOND = "respond"
_DERIVED_FROM = "derived_from"
_EXPIRED_AGE = timedelta(days=RETENTION_DAYS_DEFAULT + 30)
_FRESH_AGE = timedelta(days=1)


# --- the workspace --------------------------------------------------------------------


@dataclass(frozen=True)
class SweepWorkspace:
    """A workspace with Recallatron installed, enabled and its schedule written."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine

    def context(self, *, role: Role = Role.OWNER) -> WorkspaceContext:
        ctx = context_for_harness(self.workspace, self.owner_account_id, role)
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    @contextmanager
    def unit(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow
            uow.commit()

    @contextmanager
    def reading(self) -> Iterator[UnitOfWork]:
        with UnitOfWork(self.engine, self.database_name) as uow:
            yield uow

    def call(
        self, ctx: WorkspaceContext, name: str, payload: dict[str, object]
    ) -> OperationOutcome:
        return dispatch(
            ctx,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=ConsumerRegistry(),
        )

    def stored(self, memory_id: UUID) -> MemoryRow | None:
        with self.reading() as uow:
            return get_memory(uow.connection, memory_id)

    def memory_count(self) -> int:
        with self.reading() as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(memory_tables.memory)
                ).scalar_one()
            )

    def schedule_row(self, job_kind: str) -> Any:
        with self.reading() as uow:
            return uow.connection.execute(
                select(work_tables.schedule).where(
                    work_tables.schedule.c.job_kind == job_kind
                )
            ).one()

    def jobs_of(self, kind: str) -> list[Any]:
        with self.reading() as uow:
            return list(
                uow.connection.execute(
                    select(work_tables.job)
                    .where(work_tables.job.c.kind == kind)
                    .order_by(work_tables.job.c.created_at)
                )
            )

    def deletion_records(self) -> int:
        with self.reading() as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(deletion_record)
                ).scalar_one()
            )

    def deleted_events(self) -> list[dict[str, Any]]:
        with self.reading() as uow:
            return [
                {"subject_ref": str(row.subject_ref), "data": dict(row.data)}
                for row in uow.connection.execute(
                    select(
                        work_tables.outbox_event.c.subject_ref,
                        work_tables.outbox_event.c.data,
                    )
                    .where(work_tables.outbox_event.c.type == RECORD_DELETED)
                    .order_by(work_tables.outbox_event.c.position)
                )
            ]

    def ledger(self, reference: str) -> Any:
        with self.reading() as uow:
            row = get_deletion_record(
                uow.connection, deletion_id=RecordRef.parse(reference).id
            )
        assert row is not None, f"no deletion ledger row for {reference}"
        return row

    def set_retention(self, value: str) -> None:
        """Write the workspace's retention row directly, bounds and all.

        Through the repository rather than ``core.settings.set``: two cases below need
        a value the write path refuses on purpose (zero days, a decade), and the
        subject there is what a *reader* does with a row that should not be there.
        """
        with self.unit() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=RETENTION_DAYS_KEY,
                value=value,
                value_type=ValueType.INT,
                updated_by=None,
            )


@pytest.fixture
def sweep(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[SweepWorkspace]:
    """Recallatron loaded, installed, enabled, and its deletion hooks registered.

    ``deletions=OWNED_DELETIONS`` for the reason ``test_memory_lifecycle.py``'s own
    fixture gives: the coordinator reads that one process-wide instance, so a local
    registry would register a participant nothing ever calls — and the participant is
    exactly the half whose closure this file is about.

    **The retention key is put into the *real* settings registry, and that is not a
    convenience.** ``loaded_probe_modules`` rebinds the loader's registry to a local
    one so a fixture module's ``explicit_per_workspace`` key cannot leak into every
    workspace the session provisions afterwards. The core's own locked retention
    recheck (``scheduled_authority.dispatch_memory_expiry_in``) resolves the key
    through ``settings.resolve``, which reads the process-global table — so with the
    local registry alone the recheck raises ``setting_undeclared`` for every target
    and the sweep would be tested against a failure production never has. Production
    registers this key globally at load, so the global table is the truthful place for
    it; the swap-the-table-and-restore below is ``test_worker_job_kinds.py``'s
    ``restored_composition_root`` technique, and it keeps the leak the harness recipe
    is guarding against from happening anyway.
    """
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    SETTINGS_REGISTRY.register(RETENTION_DAYS_SPEC, origin=_MEMORY_MODULE)
    register_core_operations()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(
        monkeypatch, _MEMORY_MODULE, deletions=OWNED_DELETIONS
    ) as surfaces:
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
        row = cluster.registry_row(workspace)
        yield SweepWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
            engine=cluster.backend.pools.engine_for(row.database_name),
        )


# --- seeding and driving --------------------------------------------------------------


def _row(
    *,
    age: timedelta,
    title: str = "a memory",
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
) -> MemoryRow:
    return MemoryRow(
        id=uuid7(),
        kind="note",
        title=title,
        body="the body of a memory about apples",
        audience_kind=audience_kind,
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=datetime.now(UTC) - age,
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=1,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=None,
        invalidation_reason=None,
        source_namespace=None,
        external_source_key=None,
    )


Link = tuple[str, str, bool]


def _seed(
    sweep: SweepWorkspace,
    row: MemoryRow,
    *,
    purposes: Sequence[str] = (_RESPOND,),
    links: Sequence[Link] = (),
) -> MemoryRow:
    """One memory, through the module's own repository.

    § A3's rule for a synthetic fixture, and there is no writer for an
    already-expired row anyway: the service stamps ``recorded_at`` itself.
    """
    with sweep.unit() as uow:
        insert_memory(uow.connection, row)
        for purpose in purposes:
            insert_memory_purpose(
                uow.connection, MemoryPurposeRow(memory_id=row.id, purpose=purpose)
            )
        for ref, relation, lineage in links:
            insert_memory_link(
                uow.connection,
                MemoryLinkRow(
                    memory_id=row.id,
                    ref=ref,
                    relation=relation,
                    created_at=row.recorded_at,
                    supersession_lineage=lineage,
                ),
            )
    return row


def _age_row(sweep: SweepWorkspace, memory_id: UUID, age: timedelta) -> None:
    """Backdate one already-written row, for a replacement a real operation created."""
    with sweep.unit() as uow:
        uow.connection.execute(
            update(memory_tables.memory)
            .where(memory_tables.memory.c.id == memory_id)
            .values(recorded_at=datetime.now(UTC) - age)
        )


def _kinds() -> JobKindRegistry:
    """Both sweeps, always.

    ``core.retention_sweep`` is the control for every locking assertion in this file,
    and a registry without it would turn "the other schedule still ran" into "the
    other schedule's job failed with an unknown kind".
    """
    kinds = JobKindRegistry()
    kinds.register(
        MEMORY_RETENTION_SWEEP, MemoryRetentionSweepPayload, run_memory_retention_sweep
    )
    kinds.register(RETENTION_SWEEP, RetentionSweepPayload, run_retention_sweep)
    return kinds


def _due(sweep: SweepWorkspace, job_kind: str, *, at: datetime) -> None:
    with sweep.unit() as uow:
        uow.connection.execute(
            update(work_tables.schedule)
            .where(work_tables.schedule.c.job_kind == job_kind)
            .values(next_run_at=at)
        )


def _visit(sweep: SweepWorkspace, *, at: datetime) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=sweep.workspace, observed_due_at=None),
        kinds=_kinds(),
        consumers=ConsumerRegistry(),
        backend=sweep.cluster.backend,
        owner=_OWNER,
        clock=lambda: at,
        jitter=None,
    )


def _run_one_sweep(sweep: SweepWorkspace, *, at: datetime | None = None) -> datetime:
    """Make the sweep due and drive exactly one visit. Answers the instant used."""
    now = datetime.now(UTC) if at is None else at
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now)
    _visit(sweep, at=now)
    return now


def _supersede(sweep: SweepWorkspace, ctx: WorkspaceContext, target: MemoryRow) -> UUID:
    outcome = sweep.call(
        ctx,
        MEMORY_SUPERSEDE,
        {
            "ref": memory_reference(target.id),
            "expected_revision": target.revision,
            "kind": "note",
            "title": "the replacement",
            "body": "what the record says now",
        },
    )
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemorySuperseded), outcome
    return canonical_ref(outcome.result.replacement.ref).id


# --- the declaration itself -----------------------------------------------------------


def test_the_expiry_contexts_operation_set_is_exactly_the_core_delete_operation() -> (
    None
):
    """``MEMORY_EXPIRY_OPERATIONS`` is a copy, so it is pinned against the original.

    ``boundary/factories.py`` cannot import ``deletion.operations`` — that module
    reaches back into the operation package and closes a cycle — so it spells the one
    operation name it grants. A copy with no guard is a copy that drifts the first
    time the operation is renamed, and the sweep would then carry an operation set
    naming nothing.
    """
    assert MEMORY_EXPIRY_OPERATIONS == frozenset({RECORD_DELETE})


def test_the_module_declares_one_schedule_on_its_own_job_kind(
    sweep: SweepWorkspace,
) -> None:
    """The kind core scopes its catch-up protocol to is the kind this module ships.

    Core names :data:`CATCH_UP_JOB_KIND` as a literal, because the protocol was
    ratified for this one schedule; nothing in core imports Recallatron to check it.
    This is the check, from the side that may look.
    """
    (schedule,) = MANIFEST.schedules
    (job,) = MANIFEST.jobs
    assert schedule.job_kind == job.name == MEMORY_RETENTION_SWEEP
    assert MEMORY_RETENTION_SWEEP == CATCH_UP_JOB_KIND
    assert schedule.enabled_by_default is True
    # And it is a *second* schedule, beside the core's, not a replacement for it.
    assert sweep.schedule_row(RETENTION_SWEEP).enabled is True


def test_enabling_the_module_writes_the_default_retention_row_and_a_daily_schedule(
    sweep: SweepWorkspace,
) -> None:
    """AC 8's explicit row and its default, written by ``core.module.enable``'s step 3.

    The row has to exist for anything to read: every read, write and sweep refuses
    ``retention_unavailable`` rather than substituting the package default, so a
    module whose enable did not write it would be a module nothing could read.
    """
    row = sweep.schedule_row(MEMORY_RETENTION_SWEEP)
    assert row.module_id == _MEMORY_MODULE
    assert row.enabled is True
    assert row.next_run_at > datetime.now(UTC) + timedelta(hours=1)
    with sweep.reading() as uow:
        stored = uow.connection.execute(
            select(core_tables.workspace_setting.c.value).where(
                core_tables.workspace_setting.c.key == RETENTION_DAYS_KEY
            )
        ).scalar_one()
    assert int(str(stored)) == RETENTION_DAYS_DEFAULT


# --- the window itself ----------------------------------------------------------------


def test_the_boundary_instant_is_retained_and_anything_older_is_not(
    sweep: SweepWorkspace,
) -> None:
    """§ A9: ``recorded_at < now - days`` expires; equality is retained.

    Asked of the selection statement directly, because it is the one assertion in this
    file that cannot be made through the sweep: the sweep reads its own clock, which
    has moved on by the time any row could be compared to it, so "exactly on the
    horizon" is unreachable from the outside.
    """
    horizon = datetime.now(UTC) - _EXPIRED_AGE
    on_the_line = _seed(sweep, _row(age=_EXPIRED_AGE, title="on the horizon"))
    with sweep.unit() as uow:
        uow.connection.execute(
            update(memory_tables.memory)
            .where(memory_tables.memory.c.id == on_the_line.id)
            .values(recorded_at=horizon)
        )
    older = _seed(sweep, _row(age=_EXPIRED_AGE, title="older"))
    with sweep.unit() as uow:
        uow.connection.execute(
            update(memory_tables.memory)
            .where(memory_tables.memory.c.id == older.id)
            .values(recorded_at=horizon - timedelta(microseconds=1))
        )

    with sweep.reading() as uow:
        selected = list_expired_memory_roots(
            uow.connection, horizon=horizon, limit=SWEEP_BATCH_LIMIT
        )
    assert [root.id for root in selected] == [older.id]


def test_a_retention_row_outside_the_declared_range_expires_nothing(
    sweep: SweepWorkspace,
) -> None:
    """AC 8's fail-closed half, on the sweep rather than on a read.

    Zero days would make every memory in the workspace expired the instant it was
    written, which is exactly why the spec bounds the key and why a reader must not
    fall back to the package default when the stored row is out of range. The sweep
    refuses and the job goes back on the queue; nothing is deleted.
    """
    kept = _seed(sweep, _row(age=_EXPIRED_AGE))
    sweep.set_retention("0")

    _run_one_sweep(sweep)

    assert sweep.stored(kept.id) is not None
    (job,) = sweep.jobs_of(MEMORY_RETENTION_SWEEP)
    assert job.state == "queued"
    assert "retention_unavailable" in str(job.last_error)


def test_shortening_and_lengthening_the_window_change_the_next_sweep_only(
    sweep: SweepWorkspace,
) -> None:
    """A setting change affects the next sweep, never a stored timestamp.

    The same memory, three windows. Under the default it is expired and goes. A
    second one, younger than the default window, survives the same sweep; lengthened
    to a decade it survives another; shortened to a day it goes. Nothing rewrote a
    ``recorded_at`` at any point — the only thing that moved is where the horizon is.
    """
    old = _seed(sweep, _row(age=_EXPIRED_AGE, title="older than a year"))
    middle = _seed(sweep, _row(age=timedelta(days=200), title="older than a week"))

    _run_one_sweep(sweep)
    assert sweep.stored(old.id) is None
    middle_row = sweep.stored(middle.id)
    assert middle_row is not None
    recorded_at = middle_row.recorded_at

    sweep.set_retention("3650")
    _run_one_sweep(sweep)
    assert sweep.stored(middle.id) is not None

    sweep.set_retention("1")
    _run_one_sweep(sweep)
    assert sweep.stored(middle.id) is None
    # The row's own clock never moved; the window did.
    assert recorded_at == middle_row.recorded_at


def test_a_member_audience_memory_expires_like_a_workspace_one(
    sweep: SweepWorkspace,
) -> None:
    """The sweep is accountless, and retention is the workspace's policy, not a
    caller's reach.

    An authorizer that asked whose record this was would refuse here for ever: the
    expiry context carries no account, so a member-audience row admits it under no
    audience rule that exists. Such a row would then be unexpirable — silently, and
    only for the memories a person marked private, which is the wrong half to keep.
    """
    private = _seed(
        sweep,
        _row(
            age=_EXPIRED_AGE, audience_kind="member", audience_id=sweep.owner_account_id
        ),
    )
    shared = _seed(sweep, _row(age=_EXPIRED_AGE))

    _run_one_sweep(sweep)

    assert sweep.stored(private.id) is None
    assert sweep.stored(shared.id) is None


# --- the closure: the property the disposition split exists for -----------------------


def test_an_expired_predecessor_leaves_its_live_replacement_on_its_own_clock(
    sweep: SweepWorkspace,
) -> None:
    """**The non-revival property.** A is expired, B replaced it yesterday, B stays.

    B's only link to A is the marked supersession-lineage edge supersession wrote,
    and § A5 gives scheduled expiry the ordinary edges only: *a replacement reached
    only by ancestry survives on its own clock*. Following ancestry here — which is
    what the erasure disposition does, correctly, on its own path — would mean that
    sweeping a predecessor nobody can read any more silently destroyed the record
    that replaced it.

    The pairing is the test. A sweep run against A alone passes whichever edges the
    participant follows, and so does a suite that checks only that A is gone.

    The predecessor is superseded **while it is still readable** and backdated
    afterwards, because that is the only order the real operations allow: ``supersede``
    authorises through the one eligibility function, which applies the retention
    horizon, so an already-expired row answers ``not_found`` and could never have been
    replaced in the first place. Seeding the pair the other way round would be
    building a state the service cannot produce.
    """
    predecessor = _seed(sweep, _row(age=_FRESH_AGE, title="what we used to think"))
    replacement_id = _supersede(sweep, sweep.context(), predecessor)
    _age_row(sweep, predecessor.id, _EXPIRED_AGE)
    before = sweep.stored(replacement_id)
    assert before is not None and before.invalidated_at is None

    _run_one_sweep(sweep)

    assert sweep.stored(predecessor.id) is None
    after = sweep.stored(replacement_id)
    assert after is not None, (
        "the replacement was destroyed with its expired predecessor: scheduled "
        "expiry followed a marked supersession-lineage edge"
    )
    # Untouched, not merely present: same clock, same revision, still current.
    assert after.recorded_at == before.recorded_at
    assert after.revision == before.revision
    assert after.invalidated_at is None and after.superseded_by_id is None
    # And the ledger names it, which is the identifier-only chain § A7 asks for.
    (event,) = [
        item
        for item in sweep.deleted_events()
        if item["subject_ref"] == memory_reference(predecessor.id)
    ]
    ledger = sweep.ledger(str(event["data"]["deletion_record_ref"]))
    assert ledger.cause == RETENTION_EXPIRY
    assert ledger.approval_id is None
    assert ledger.retained_successor_ref == memory_reference(replacement_id)
    # One row removed, not two: the replacement is not in the count either.
    assert ledger.invalidated_memory_count == 1


def test_a_replacement_reached_by_an_ordinary_path_goes_with_its_expired_source(
    sweep: SweepWorkspace,
) -> None:
    """The other half of the same closure row, and the reason it is edges not depth.

    ``ordinary`` is derived from the expired root through a real ``derived_from``
    link, so it is an ordinary dependant and goes, however fresh it is. A
    disposition that skipped the whole cascade — rather than skipping the *marked*
    hops — would leave it behind and pass the test above.
    """
    source = _seed(sweep, _row(age=_EXPIRED_AGE, title="the source"))
    ordinary = _seed(
        sweep,
        _row(age=_FRESH_AGE, title="built on it"),
        links=[(memory_reference(source.id), _DERIVED_FROM, False)],
    )

    _run_one_sweep(sweep)

    assert sweep.stored(source.id) is None
    assert sweep.stored(ordinary.id) is None


def test_a_chain_of_expired_ancestors_leaves_the_current_generation_standing(
    sweep: SweepWorkspace,
) -> None:
    """A -> B -> C with A and B both expired: C keeps its own clock.

    The A -> B -> C fixture ``test_memory_lifecycle.py`` builds for the ancestry-copy
    rule, now driven through the real sweep. C carries marked edges to **both** its
    ancestors, so a single ordinary-only step is not enough — every hop has to skip
    them, which is what "skip marked ancestry edges at every step" means.
    """
    first = _seed(sweep, _row(age=_FRESH_AGE, title="first"))
    ctx = sweep.context()
    second_id = _supersede(sweep, ctx, first)
    second = sweep.stored(second_id)
    assert second is not None
    third_id = _supersede(sweep, ctx, second)
    # Both ancestors are backdated past the horizon afterwards; C stays where
    # supersession put it. Afterwards, because an expired row cannot be superseded:
    # see the note on the test above.
    _age_row(sweep, first.id, _EXPIRED_AGE)
    _age_row(sweep, second_id, _EXPIRED_AGE)

    _run_one_sweep(sweep)

    assert sweep.stored(first.id) is None
    assert sweep.stored(second_id) is None
    surviving = sweep.stored(third_id)
    assert surviving is not None and surviving.invalidated_at is None


# --- the authority -------------------------------------------------------------------


def test_a_sweep_job_with_no_enabled_schedule_expires_nothing(
    sweep: SweepWorkspace,
) -> None:
    """Disablement while a job is queued, and the arbitrary-enqueue attempt at once.

    Both reach the same state: a leased job of this kind whose workspace has no
    single enabled schedule for it, so the worker mints no capability and the handler
    has no authority to expire anything with. It fails the attempt rather than
    finishing green on an empty sweep, and the ticker enqueues nothing more while the
    schedule is off.
    """
    kept = _seed(sweep, _row(age=_EXPIRED_AGE))
    now = datetime.now(UTC)
    with sweep.unit() as uow:
        uow.connection.execute(
            update(work_tables.schedule)
            .where(work_tables.schedule.c.job_kind == MEMORY_RETENTION_SWEEP)
            .values(enabled=False, next_run_at=now)
        )
        enqueue_job(
            uow.connection,
            kind=MEMORY_RETENTION_SWEEP,
            payload={"workspace_id": str(sweep.workspace)},
            now=now,
            max_attempts=8,
        )

    _visit(sweep, at=now)

    assert sweep.stored(kept.id) is not None
    (job,) = sweep.jobs_of(MEMORY_RETENTION_SWEEP)
    assert job.state == "queued"
    assert SWEEP_AUTHORITY_MISSING in str(job.last_error)
    # The disabled row was neither enqueued against nor advanced by the ticker.
    assert sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at == now


def test_a_forged_capability_refuses_rather_than_expiring(
    sweep: SweepWorkspace,
) -> None:
    """Three forgeries, one answer, and no deletion from any of them.

    A capability for a job kind no enabled schedule names; one whose database is
    another workspace's, so it asserts a routing the connection disagrees with; and
    one that is not the object the view carries at all. All three are
    ``scheduled_authority_invalid`` — one state, deliberately, because telling a
    caller which fact failed would describe the workspace's schedule table to
    something that has already proved it is not the worker.
    """
    target = _seed(sweep, _row(age=_EXPIRED_AGE))
    ref = canonical_ref(memory_reference(target.id))
    real = sweep.schedule_row(MEMORY_RETENTION_SWEEP)

    def _forgery(**overrides: object) -> VerifiedScheduledExecution:
        fields: dict[str, Any] = {
            "workspace_id": sweep.workspace,
            "database": sweep.database_name,
            "job_id": uuid7(),
            "job_kind": MEMORY_RETENTION_SWEEP,
            "lease_owner": _OWNER,
            "module_id": _MEMORY_MODULE,
            "schedule_id": real.id,
        }
        fields.update(overrides)
        return VerifiedScheduledExecution(**fields)

    attempts = [
        _forgery(job_kind="recallatron.no_such_kind"),
        _forgery(database="ws_no_such_database"),
    ]
    with sweep.reading() as uow:
        for forged in attempts:
            view = HandlerUnitOfWork(
                uow, consumers=ConsumerRegistry(), scheduled_execution=forged
            )
            answer = dispatch_memory_expiry_in(
                view,
                forged,
                record_type=MEMORY_RECORD_QUALIFIED,
                target_ref=ref,
                retention_key=RETENTION_DAYS_KEY,
                retention_days=RETENTION_DAYS_DEFAULT,
            )
            assert isinstance(answer, Refusal), answer
            assert answer.state == SCHEDULED_AUTHORITY_INVALID
        # And a capability the view does not carry at all, however well formed.
        stranger = _forgery()
        bare = HandlerUnitOfWork(uow, consumers=ConsumerRegistry())
        answer = dispatch_memory_expiry_in(
            bare,
            stranger,
            record_type=MEMORY_RECORD_QUALIFIED,
            target_ref=ref,
            retention_key=RETENTION_DAYS_KEY,
            retention_days=RETENTION_DAYS_DEFAULT,
        )
        assert isinstance(answer, Refusal), answer
        assert answer.state == SCHEDULED_AUTHORITY_INVALID

    assert sweep.stored(target.id) is not None
    assert sweep.deleted_events() == []


def test_an_absent_or_unexpired_target_is_skipped_without_a_deletion_record(
    monkeypatch: pytest.MonkeyPatch, sweep: SweepWorkspace
) -> None:
    """§ A9: neither writes a false ledger row, and neither fails the batch.

    The selection is substituted, and only the selection: the real capability, the
    real coordinator and the real authorizer all run. There is no other way to hand
    the expiry a target it would not have chosen, which is the point — a row that is
    not expired is not selectable, and the recheck under the lock exists precisely
    for the window in which that stops being true.
    """
    from rheo_recallatron import retention as retention_module
    from rheo_recallatron.storage.repository import ExpiredRoot

    fresh = _seed(sweep, _row(age=_FRESH_AGE, title="not expired"))
    absent = uuid7()
    monkeypatch.setattr(
        retention_module,
        "list_expired_memory_roots",
        lambda conn, *, horizon, limit: (
            ExpiredRoot(fresh.id, None),
            ExpiredRoot(absent, None),
        ),
    )

    _run_one_sweep(sweep)

    assert sweep.stored(fresh.id) is not None
    assert sweep.deleted_events() == []
    assert sweep.deletion_records() == 0
    # No remainder either — nothing was actually expired — so the batch finished.
    (job,) = sweep.jobs_of(MEMORY_RETENTION_SWEEP)
    assert job.state == "succeeded"


def test_a_verified_expiry_publishes_record_deleted_from_the_worker(
    sweep: SweepWorkspace,
) -> None:
    """The event fires on the scheduled path, not only on the approved one.

    ``work/loop.py`` threads the composition root's ``ConsumerRegistry`` onto the
    handler's view; without it the expiry refuses ``consumers_missing`` and the
    deletion goes with it, so a green sweep here is also the proof that the wiring is
    there. An event nobody consumes is not an error: the outbox row is written and
    the publish completes with zero deliveries.
    """
    target = _seed(sweep, _row(age=_EXPIRED_AGE))

    _run_one_sweep(sweep)

    (event,) = sweep.deleted_events()
    assert event["subject_ref"] == memory_reference(target.id)
    assert event["data"]["ref"] == memory_reference(target.id)
    ledger = sweep.ledger(str(event["data"]["deletion_record_ref"]))
    assert ledger.cause == RETENTION_EXPIRY
    assert (ledger.actor_kind, ledger.actor_id) == ("system", None)
    assert ledger.participants == (_MEMORY_MODULE,)


# --- the ticker: locking, coalescing and the wake instant -----------------------------


def test_a_long_running_sweep_does_not_delay_another_due_schedule(
    sweep: SweepWorkspace,
) -> None:
    """**The test that proves ``SKIP LOCKED``.**

    A sweep in flight holds its own schedule row for the length of its handler. The
    ticker runs *before* the job drain, in one short transaction, and reaches every
    due schedule in one pass — so a blocking ``FOR UPDATE`` on this one row would put
    ``core.retention_sweep`` and every other due schedule behind a sweep that may take
    minutes.

    ``lock_timeout`` is what turns that from a hang into a failure: under a blocking
    lock this raises instead of skipping, which is a red test rather than a suite that
    never finishes.
    """
    now = datetime.now(UTC)
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now)
    _due(sweep, RETENTION_SWEEP, at=now)
    held = sweep.schedule_row(MEMORY_RETENTION_SWEEP)

    holder = sweep.engine.connect()
    try:
        holder.begin()
        holder.execute(
            select(work_tables.schedule.c.id)
            .where(work_tables.schedule.c.id == held.id)
            .with_for_update()
        ).one()
        with sweep.engine.begin() as ticker:
            ticker.execute(text("SET LOCAL lock_timeout = '3s'"))
            run_due_schedules(ticker, workspace_id=sweep.workspace, now=now)
    finally:
        holder.close()

    # The unrelated schedule ran in that same pass: enqueued and advanced a day.
    assert [job.kind for job in sweep.jobs_of(RETENTION_SWEEP)] == [RETENTION_SWEEP]
    assert sweep.schedule_row(RETENTION_SWEEP).next_run_at == now + timedelta(days=1)
    # The locked one was skipped: no job, no error, no change to its instant.
    assert sweep.jobs_of(MEMORY_RETENTION_SWEEP) == []
    assert sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at == now


def test_two_simultaneous_tickers_enqueue_one_sweep(sweep: SweepWorkspace) -> None:
    """Overlapping visits, one job. The row lock is what serialises them.

    Without it both tickers read the same due row from their own snapshots and both
    enqueue, and the workspace runs two sweeps over one backlog.
    """
    now = datetime.now(UTC)
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now)

    first = sweep.engine.connect()
    try:
        first.begin()
        run_due_schedules(first, workspace_id=sweep.workspace, now=now)
        with sweep.engine.begin() as second:
            second.execute(text("SET LOCAL lock_timeout = '3s'"))
            run_due_schedules(second, workspace_id=sweep.workspace, now=now)
        first.commit()
    finally:
        first.close()

    assert len(sweep.jobs_of(MEMORY_RETENTION_SWEEP)) == 1


@pytest.mark.parametrize("state", ["queued", "leased"])
def test_an_unfinished_sweep_job_coalesces_the_next_tick(
    sweep: SweepWorkspace, state: str
) -> None:
    """A queued retry and a lease awaiting recovery both count.

    ``queued`` is a first attempt or one waiting out a backoff; ``leased`` with a
    lapsed ``lease_until`` is a worker that died mid-batch and a row another worker
    will re-acquire. Enqueueing beside either is two sweeps racing over one backlog,
    so the ticker does neither — and leaves ``next_run_at`` where it is, because
    advancing it would push the daily trigger a day out for a job that has not run.
    """
    now = datetime.now(UTC)
    with sweep.unit() as uow:
        job_id = enqueue_job(
            uow.connection,
            kind=MEMORY_RETENTION_SWEEP,
            payload={"workspace_id": str(sweep.workspace)},
            now=now,
            max_attempts=8,
        )
        if state == "leased":
            uow.connection.execute(
                update(work_tables.job)
                .where(work_tables.job.c.id == job_id)
                .values(
                    state="leased",
                    lease_owner="a-worker-that-died",
                    lease_until=now - timedelta(minutes=5),
                )
            )
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now)

    with sweep.unit() as uow:
        run_due_schedules(uow.connection, workspace_id=sweep.workspace, now=now)

    assert [job.id for job in sweep.jobs_of(MEMORY_RETENTION_SWEEP)] == [job_id]
    assert sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at == now


def test_a_cancelled_sweep_stops_coalescing_the_schedule(
    sweep: SweepWorkspace,
) -> None:
    """Cancellation ends the coalescing with the job, so the daily trigger returns.

    A queued job goes straight to ``cancelled``, which is neither queued nor leased,
    so the next tick is free to enqueue. Without that the schedule would be held shut
    for ever by a job nobody is ever going to run.
    """
    now = datetime.now(UTC)
    with sweep.unit() as uow:
        job_id = enqueue_job(
            uow.connection,
            kind=MEMORY_RETENTION_SWEEP,
            payload={"workspace_id": str(sweep.workspace)},
            now=now,
            max_attempts=8,
        )
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now)
    with sweep.unit() as uow:
        run_due_schedules(uow.connection, workspace_id=sweep.workspace, now=now)
    assert len(sweep.jobs_of(MEMORY_RETENTION_SWEEP)) == 1

    with sweep.unit() as uow:
        assert request_cancellation(uow.connection, job_id, now=now) == "cancelled"
    with sweep.unit() as uow:
        run_due_schedules(uow.connection, workspace_id=sweep.workspace, now=now)

    states = {str(job.state) for job in sweep.jobs_of(MEMORY_RETENTION_SWEEP)}
    assert len(sweep.jobs_of(MEMORY_RETENTION_SWEEP)) == 2
    assert states == {"cancelled", "queued"}


def test_a_past_schedule_instant_cannot_hot_wake_the_worker_during_backoff(
    sweep: SweepWorkspace,
) -> None:
    """``earliest_schedule_due_at`` excludes a coalesced catch-up schedule.

    After a batch asks for another, this row's instant is a second in the past. If the
    job it points at is under a retry backoff or held by a live lease, that instant
    would wake the worker on every single pass and find nothing to do — the job's own
    ``next_run_at`` or ``lease_until`` is the instant something will actually happen
    at, and ``jobs.earliest_due_at`` already owns it.

    Scoped: the core sweep's own instant is still reported, coalesced or not, which is
    what the assertion below is actually comparing against.
    """
    now = datetime.now(UTC)
    with sweep.unit() as uow:
        enqueue_job(
            uow.connection,
            kind=MEMORY_RETENTION_SWEEP,
            payload={"workspace_id": str(sweep.workspace)},
            now=now,
            max_attempts=8,
        )
        # Also a queued job of the *core* kind, so the exclusion cannot be "any
        # schedule with a job" and still pass.
        enqueue_job(
            uow.connection,
            kind=RETENTION_SWEEP,
            payload={"workspace_id": str(sweep.workspace)},
            now=now,
            max_attempts=8,
        )
    _due(sweep, MEMORY_RETENTION_SWEEP, at=now - timedelta(seconds=1))
    core_due = now + timedelta(hours=6)
    _due(sweep, RETENTION_SWEEP, at=core_due)

    with sweep.reading() as uow:
        assert earliest_schedule_due_at(uow.connection) == core_due


# --- catch-up: the backlog, and the batch that made no progress -----------------------


def test_a_backlog_larger_than_one_batch_drains_across_committed_batches(
    sweep: SweepWorkspace,
) -> None:
    """250 roots, three batches, no day-long pause, and a clean finish.

    The three claims, in order of how quietly they break.

    *Each batch commits.* The count drops by exactly ``SWEEP_BATCH_LIMIT`` per visit
    and the deletions are readable from a fresh transaction, so a batch that rolled
    back would show as a count that did not move.

    *The next batch is asked for in seconds, not a day.* The schedule's instant after
    a batch with a remainder is barely past the batch's finish; after the batch that
    clears the backlog it is the ordinary daily one the ticker wrote. A rearm that
    fired when it should not, or did not fire when it should, is a different instant
    either way.

    *Arrivals during catch-up join later batches.* Twenty more expired rows are
    written between two visits and are gone by the end, because each batch re-reads
    the window rather than working from a frozen list.
    """
    roots = [_row(age=_EXPIRED_AGE, title=f"old {index}") for index in range(250)]
    with sweep.unit() as uow:
        for row in roots:
            insert_memory(uow.connection, row)
    assert sweep.memory_count() == 250

    base = datetime.now(UTC)
    first = _run_one_sweep(sweep, at=base)
    assert sweep.memory_count() == 150
    after_first = sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at
    assert first < after_first < first + timedelta(minutes=1), after_first

    _run_one_sweep(sweep, at=base + timedelta(seconds=5))
    assert sweep.memory_count() == 50

    # Arrivals mid-catch-up.
    late = [_row(age=_EXPIRED_AGE, title=f"late {index}") for index in range(20)]
    with sweep.unit() as uow:
        for row in late:
            insert_memory(uow.connection, row)

    last = _run_one_sweep(sweep, at=base + timedelta(seconds=10))
    assert sweep.memory_count() == 0
    # No remainder, so no continuation: the daily instant the ticker wrote stands.
    assert sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at == last + timedelta(
        days=1
    )
    assert all(
        job.state == "succeeded" for job in sweep.jobs_of(MEMORY_RETENTION_SWEEP)
    )
    assert len(sweep.deleted_events()) == 270


def test_a_batch_with_a_remainder_and_no_progress_falls_through_to_backoff(
    monkeypatch: pytest.MonkeyPatch, sweep: SweepWorkspace
) -> None:
    """No progress is a failed attempt, never an empty success with a continuation.

    Rearming a batch that removed nothing mints a fresh job one second later, which
    finds the same rows, removes none of them, and does it again — one job per second,
    for ever, each one recorded as a success. Ordinary retry and backoff are the right
    answer, and the only thing that chooses between the two behaviours is the
    ``removed`` count.

    The core expiry entry is substituted because no reachable state makes a freshly
    selected expired root undeletable: the refusals that could — a capability that
    stopped verifying, a retention value that moved — need a concurrent committed
    write landing inside the batch's own transaction window, which is a thread rather
    than a test of this guard.
    """
    from rheo_recallatron import retention as retention_module

    stubborn = _seed(sweep, _row(age=_EXPIRED_AGE))
    monkeypatch.setattr(
        retention_module,
        "dispatch_memory_expiry_in",
        lambda *args, **kwargs: Refusal(
            SCHEDULED_AUTHORITY_INVALID, "refused by the test"
        ),
    )

    now = _run_one_sweep(sweep)

    assert sweep.stored(stubborn.id) is not None
    (job,) = sweep.jobs_of(MEMORY_RETENTION_SWEEP)
    assert job.state == "queued", "a no-progress batch must not finish succeeded"
    assert SWEEP_NO_PROGRESS in str(job.last_error)
    assert job.next_run_at > now, "the retry must sit under an ordinary backoff"
    # And no continuation was written: the daily instant the ticker set still stands.
    assert sweep.schedule_row(MEMORY_RETENTION_SWEEP).next_run_at == now + timedelta(
        days=1
    )
