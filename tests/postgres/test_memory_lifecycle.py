"""AC 7: correction, supersession and user erasure over a real Recallatron memory.

Seams under test: ``correct``/``supersede``'s compare-and-set and their closure
dispositions (ordinary, lineage and mixed, across several derivative depths); user
erasure through the **real** owned-delete attachment on ``recallatron.memory``, not
the synthetic owner ``tests/postgres/test_record_deletion.py`` drives; that
``recallatron.memory.invalidated`` is published for exactly the rows a closure marks
non-erasure invalidated and never for a row that was physically removed; and that
the content-free ledger's four counters read ``(0, 0, 0, N)`` with ``N`` the real
number of distinct rows the deletion removed.

**Everything that can go through ``dispatch`` does.** The two lifecycle operations
are dispatched against the registry the loader built, and every erasure goes through
``dispatch`` of the core's record-delete operation followed by an approval, because
the properties under test are properties of that path — the hold, the guards, the
rebuilt original caller, and one transaction around the lot. The two exceptions are
deliberate and are named where they appear: rows are *seeded* through the module's
own repository (there is no writer for an already-expired row, and § A3 says a
synthetic fixture seeds through the repository), and :func:`dependent_closure` is
called directly in the one case whose subject is the traversal itself over rows no
live operation can reach any more.

**Counts and states are read off the tables, never off a handler's return.** The
deletion operation answers ``{deletion_ref}`` and nothing else — that is the
disclosure rule, not an omission — and the two lifecycle operations answer no closure
size either, so "did the closure actually go" is answered by looking for the rows.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
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
from harness.registry import add_member
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import (
    OWNED_DELETIONS,
    USER_ERASURE,
    WORKSPACE_LIFECYCLE_LOCK_KEY,
    DeletionRecordRow,
    get_deletion_record,
)
from rheo_core.deletion.operations import (
    RECORD_DELETE,
    RECORD_DELETED,
    RecordDeleted,
)
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.contracts import (
    MemoryCorrected,
    MemorySuperseded,
    MemoryWritten,
)
from rheo_recallatron.eligibility import LIFECYCLE_ROLES, memory_reference
from rheo_recallatron.events import (
    EVENT_SCHEMA_VERSION,
    MEMORY_INVALIDATED,
    MEMORY_RECORDED,
)
from rheo_recallatron.lifecycle import DELETION_PARTICIPANT_TYPES, dependent_closure
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_DERIVE,
    MEMORY_SUPERSEDE,
)
from rheo_recallatron.references import canonical_ref
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.source_units import REPRESENTATION_MEMORY
from rheo_recallatron.storage import tables as memory_tables
from rheo_recallatron.storage.repository import (
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryLinkRow,
    MemoryMentionRow,
    MemoryPurposeRow,
    MemoryRow,
    SourceReceiptRow,
    get_memory,
    get_memory_embedding,
    get_memory_entity,
    get_source_receipt,
    insert_memory,
    insert_memory_embedding,
    insert_memory_entity,
    insert_memory_link,
    insert_memory_mention,
    insert_memory_purpose,
    insert_source_receipt,
    list_memory_links,
)
from sqlalchemy import Engine, func, select, text

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id
_RESPOND = "respond"
_DERIVED_FROM = "derived_from"
_ABOUT = "about"
_SOURCE_CORRECTED = "source_corrected"
_SOURCE_SUPERSEDED = "source_superseded"
_MODEL = "probe-embedding-model"


# --- the workspace --------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleWorkspace:
    """A workspace with Recallatron installed, enabled and migrated."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str
    engine: Engine
    consumers: ConsumerRegistry

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
        """One dispatch of a **module** operation, against the loaded registry."""
        return dispatch(
            ctx,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def core_call(
        self, ctx: WorkspaceContext, name: str, payload: dict[str, object]
    ) -> OperationOutcome:
        """One dispatch of a **core** operation, against the process-wide registry.

        The deletion coordinator and the approval operation are the core's own and
        are registered by ``register_core_operations()``; the loader's local registry
        holds the module's operations alone, and ``execute_approved`` resolves the
        held operation against the process-wide table whatever registry dispatched
        the approval.
        """
        return dispatch(ctx, name, payload, consumers=self.consumers)

    def rows(self) -> dict[str, int]:
        tables = {
            "memory": memory_tables.memory,
            "purpose": memory_tables.memory_purpose,
            "entity": memory_tables.memory_entity,
            "mention": memory_tables.memory_mention,
            "link": memory_tables.memory_link,
            "embedding": memory_tables.memory_embedding,
        }
        with self.reading() as uow:
            return {
                name: int(
                    uow.connection.execute(
                        select(func.count()).select_from(table)
                    ).scalar_one()
                )
                for name, table in tables.items()
            }

    def stored(self, memory_id: UUID) -> MemoryRow | None:
        with self.reading() as uow:
            return get_memory(uow.connection, memory_id)

    def links_of(self, memory_id: UUID) -> set[tuple[str, str, bool]]:
        with self.reading() as uow:
            return {
                (link.ref, link.relation, link.supersession_lineage)
                for link in list_memory_links(uow.connection, memory_id)
            }

    def events(self, event_type: str) -> list[dict[str, Any]]:
        with self.reading() as uow:
            rows = uow.connection.execute(
                select(
                    work_tables.outbox_event.c.type,
                    work_tables.outbox_event.c.subject_ref,
                    work_tables.outbox_event.c.subject_revision,
                    work_tables.outbox_event.c.schema_version,
                    work_tables.outbox_event.c.data,
                )
                .where(work_tables.outbox_event.c.type == event_type)
                .order_by(work_tables.outbox_event.c.position)
            )
            return [
                {
                    "type": str(row.type),
                    "subject_ref": str(row.subject_ref),
                    "subject_revision": int(row.subject_revision),
                    "schema_version": int(row.schema_version),
                    "data": dict(row.data),
                }
                for row in rows
            ]

    def invalidated_refs(self) -> list[str]:
        return [event["subject_ref"] for event in self.events(MEMORY_INVALIDATED)]

    def ledger(self, reference: str) -> DeletionRecordRow:
        with self.reading() as uow:
            row = get_deletion_record(
                uow.connection, deletion_id=RecordRef.parse(reference).id
            )
        assert row is not None, f"no deletion ledger row for {reference}"
        return row


@pytest.fixture
def lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[LifecycleWorkspace]:
    """Recallatron loaded, installed, enabled, and its deletion hooks registered.

    ``deletions=OWNED_DELETIONS`` is what ``apps/core``'s own composition root
    passes, and it is what makes this module's deletion participant run: the
    coordinator reads that one instance, so a registry local to this fixture would
    register a hook nothing ever calls. The owned-delete pair on the record type
    needs no such argument — the loader registers it unconditionally — and the
    assertion below is what holds that claim to the loader rather than to this
    comment.

    The memory resolver also goes on the **process-wide** table, which is where a
    real deployment's ``load_modules`` puts it: the record-state guard resolves a
    held deletion's subject through that table, and the two places this module
    resolves a reference itself call ``resolve_in`` with its default.
    """
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
        yield LifecycleWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
            engine=cluster.backend.pools.engine_for(row.database_name),
            consumers=ConsumerRegistry(),
        )


# --- seeding --------------------------------------------------------------------------


def _recent(offset_seconds: int = 0) -> datetime:
    return datetime.now(UTC) - timedelta(days=1) + timedelta(seconds=offset_seconds)


def _row(
    *,
    title: str = "a memory",
    body: str = "the body of a memory about apples",
    audience_kind: str = "workspace",
    audience_id: UUID | None = None,
    kind: str = "note",
    revision: int = 1,
    source_namespace: str | None = None,
    external_source_key: str | None = None,
) -> MemoryRow:
    return MemoryRow(
        id=uuid7(),
        kind=kind,
        title=title,
        body=body,
        audience_kind=audience_kind,
        audience_id=audience_id,
        confidence=None,
        occurred_at=None,
        recorded_at=_recent(),
        recorded_by_kind="account",
        recorded_by_id=None,
        origin="told",
        revision=revision,
        corrected_at=None,
        superseded_by_id=None,
        invalidated_at=None,
        invalidation_reason=None,
        source_namespace=source_namespace,
        external_source_key=external_source_key,
    )


Link = tuple[str, str, bool]
"""``(ref, relation, supersession_lineage)``, as a fixture spells one."""


def _seed(
    memory: LifecycleWorkspace,
    row: MemoryRow,
    *,
    purposes: Sequence[str] = (_RESPOND,),
    links: Sequence[Link] = (),
    embed: bool = True,
) -> MemoryRow:
    """One memory, its purposes, its links and (by default) one embedding.

    Through the module's own repository, which is what § A3 says a synthetic fixture
    does: there is no writer for an already-invalidated or already-expired row, and
    no embedding provider at all in this run.
    """
    with memory.unit() as uow:
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
        if embed:
            insert_memory_embedding(
                uow.connection,
                MemoryEmbeddingRow(
                    memory_id=row.id,
                    model_id=_MODEL,
                    dimensions=3,
                    vector=(0.1, 0.2, 0.3),
                    embedded_at=row.recorded_at,
                ),
            )
    return row


def _embedded(memory: LifecycleWorkspace, memory_id: UUID) -> bool:
    with memory.reading() as uow:
        return get_memory_embedding(uow.connection, memory_id, _MODEL) is not None


def _derived(
    target: MemoryRow, *, lineage: bool = False, relation: str = _DERIVED_FROM
) -> Link:
    """One link naming ``target``, as the seeder spells it."""
    return (memory_reference(target.id), relation, lineage)


# --- driving the two lifecycle operations ---------------------------------------------


def _corrected(outcome: OperationOutcome) -> MemoryCorrected:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemoryCorrected), outcome
    return outcome.result


def _superseded(outcome: OperationOutcome) -> MemorySuperseded:
    assert outcome.ok, outcome
    assert isinstance(outcome.result, MemorySuperseded), outcome
    return outcome.result


def _refused(outcome: OperationOutcome, state: str) -> None:
    assert outcome.state == state, outcome
    assert outcome.result is None, outcome
    assert outcome.error is not None and outcome.error.error_code == state, outcome


def _correct(
    memory: LifecycleWorkspace,
    ctx: WorkspaceContext,
    target: MemoryRow,
    *,
    expected_revision: int | None = None,
    title: str = "a corrected memory",
    body: str = "the corrected body, about pears",
) -> OperationOutcome:
    return memory.call(
        ctx,
        MEMORY_CORRECT,
        {
            "ref": memory_reference(target.id),
            "expected_revision": (
                target.revision if expected_revision is None else expected_revision
            ),
            "title": title,
            "body": body,
        },
    )


def _supersede(
    memory: LifecycleWorkspace,
    ctx: WorkspaceContext,
    target: MemoryRow,
    *,
    expected_revision: int | None = None,
    title: str = "the replacement",
    body: str = "what the record says now",
) -> OperationOutcome:
    return memory.call(
        ctx,
        MEMORY_SUPERSEDE,
        {
            "ref": memory_reference(target.id),
            "expected_revision": (
                target.revision if expected_revision is None else expected_revision
            ),
            "kind": "note",
            "title": title,
            "body": body,
        },
    )


# --- driving a real erasure through the coordinator -----------------------------------


def _held(memory: LifecycleWorkspace, ctx: WorkspaceContext, target: UUID) -> UUID:
    outcome = memory.core_call(ctx, RECORD_DELETE, {"ref": memory_reference(target)})
    assert outcome.state == "approval_required", outcome
    assert outcome.approval_id is not None, outcome
    return outcome.approval_id


def _approve(
    memory: LifecycleWorkspace, ctx: WorkspaceContext, approval_id: UUID
) -> OperationOutcome:
    return memory.core_call(ctx, APPROVAL_APPROVE, {"approval_id": str(approval_id)})


def _erase(
    memory: LifecycleWorkspace,
    target: UUID,
    *,
    holder: WorkspaceContext | None = None,
    approver: WorkspaceContext | None = None,
) -> DeletionRecordRow:
    """Hold and approve one deletion; answer the ledger row it wrote."""
    held_by = memory.context() if holder is None else holder
    approved_by = memory.context() if approver is None else approver
    approval_id = _held(memory, held_by, target)
    outcome = _approve(memory, approved_by, approval_id)
    assert outcome.ok, outcome
    (event,) = [
        item
        for item in memory.events(RECORD_DELETED)
        if item["subject_ref"] == memory_reference(target)
    ]
    return memory.ledger(str(event["data"]["deletion_record_ref"]))


# --- the loader's own registration ----------------------------------------------------


def test_loading_the_module_makes_its_memory_a_real_owned_deletable_type(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The owned-delete pair reaches the registry the coordinator reads.

    Asked of ``OWNED_DELETIONS`` rather than of the manifest, because a manifest that
    declared the pair and a loader that dropped it would agree with each other and
    leave every deletion refusing generically. The participant is asserted the same
    way and from the same registry, since the two halves of an erasure are registered
    by different lines.
    """
    reference = RecordRef(
        module=_MEMORY_MODULE, record_type=MEMORY_RECORD_TYPE, id=uuid7()
    )

    owner = OWNED_DELETIONS.owner_of(reference)

    assert owner is not None
    assert owner.qualified_type == DELETION_PARTICIPANT_TYPES[0]
    assert owner.module_id == _MEMORY_MODULE
    participants = OWNED_DELETIONS.participants_for(
        reference, enabled_modules=frozenset({_MEMORY_MODULE})
    )
    assert [item.module_id for item in participants] == [_MEMORY_MODULE]


def test_the_declared_delete_roles_are_the_set_the_authorizer_checks() -> None:
    """The declaration and the check read one constant, and it excludes ``service``.

    Equality rather than identity, because pydantic copies a ``frozenset`` field on
    validation and the manifest's is therefore never the same object the authorizer
    holds. The membership assertions are what the row is actually for: § A10 gives
    correcting, superseding and erasing to owner and member, and a service that may
    write what it learns may not erase what somebody else recorded.
    """
    (memory_type,) = [
        item for item in MANIFEST.record_types if item.name == MEMORY_RECORD_TYPE
    ]

    assert memory_type.delete_roles == LIFECYCLE_ROLES
    assert memory_type.delete_roles == frozenset({Role.OWNER, Role.MEMBER})
    assert Role.SERVICE not in LIFECYCLE_ROLES
    assert memory_type.authorize_delete is not None
    assert memory_type.delete_owned is not None


# --- the compare-and-set --------------------------------------------------------------


def test_a_stale_revision_is_record_stale_and_writes_nothing(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The CAS refuses, and the refusal is distinguishable from a missing record."""
    target = _seed(lifecycle, _row())
    before = lifecycle.rows()

    outcome = _correct(lifecycle, lifecycle.context(), target, expected_revision=2)

    _refused(outcome, "record_stale")
    assert lifecycle.rows() == before
    stored = lifecycle.stored(target.id)
    assert stored is not None and stored.title == target.title


def test_a_revision_below_one_is_an_input_error_rather_than_a_stale_copy(
    lifecycle: LifecycleWorkspace,
) -> None:
    target = _seed(lifecycle, _row())

    outcome = _correct(lifecycle, lifecycle.context(), target, expected_revision=0)

    _refused(outcome, "input_invalid")


def test_a_matching_revision_on_a_noncurrent_row_is_not_found_and_writes_nothing(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The third test in § A7's sequence, reached only when the second one passed.

    The row is seeded already invalidated, so ``history`` mode authorizes it and the
    revision matches — which is exactly the state that must refuse ``not_found``
    rather than ``record_stale``.
    """
    target = _row()
    _seed(lifecycle, target)
    with lifecycle.unit() as uow:
        uow.connection.execute(
            text(
                "UPDATE recallatron.memory SET invalidated_at = now(), "
                "invalidation_reason = 'source_corrected' WHERE id = :id"
            ),
            {"id": target.id},
        )
    before = lifecycle.rows()

    outcome = _correct(lifecycle, lifecycle.context(), target)

    _refused(outcome, "not_found")
    assert lifecycle.rows() == before


def test_a_target_this_caller_cannot_read_is_not_found(
    lifecycle: LifecycleWorkspace,
) -> None:
    """Another member's private memory is the same ``not_found`` as no memory at all."""
    other = add_member(
        lifecycle.cluster.backend,
        lifecycle.workspace,
        Role.MEMBER,
        display_name="somebody-else",
    )
    target = _seed(lifecycle, _row(audience_kind="member", audience_id=other))

    outcome = _correct(lifecycle, lifecycle.context(), target)

    _refused(outcome, "not_found")
    assert lifecycle.stored(target.id) is not None


def test_the_loser_of_a_concurrent_supersede_is_told_its_copy_is_stale(
    lifecycle: LifecycleWorkspace,
) -> None:
    """Two supersessions of one memory: the second is ``record_stale``, not
    ``not_found``.

    This is the case § A7 words explicitly, and it is why the compare-and-set is
    judged **before** the current-state test. The winner has made the predecessor
    noncurrent *and* incremented its revision; a sequence that tested current state
    first would answer ``not_found`` and tell the loser its record is gone when what
    actually happened is that its copy moved.
    """
    target = _seed(lifecycle, _row())
    ctx = lifecycle.context()
    winner = _superseded(_supersede(lifecycle, ctx, target, title="the winner"))
    assert winner.predecessor_revision == 2

    loser = _supersede(lifecycle, ctx, target, title="the loser")

    _refused(loser, "record_stale")
    stored = lifecycle.stored(target.id)
    assert stored is not None
    assert stored.superseded_by_id == canonical_ref(winner.replacement.ref).id


# --- correction and its closure -------------------------------------------------------


def test_correction_rewrites_in_place_and_invalidates_what_was_built_on_it(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The corrected row stays current; its derivatives do not.

    Three depths, because the traversal's onward rule is the thing under test: ``b``
    links to ``a`` by ``derived_from``, ``c`` to ``b``, and ``d`` is an unrelated row
    that must not be touched.
    """
    a = _seed(lifecycle, _row(title="the original"))
    b = _seed(lifecycle, _row(title="derived once"), links=[_derived(a)])
    c = _seed(lifecycle, _row(title="derived twice"), links=[_derived(b)])
    d = _seed(lifecycle, _row(title="unrelated"))

    result = _corrected(_correct(lifecycle, lifecycle.context(), a))

    assert result.ref == memory_reference(a.id)
    assert result.revision == 2
    corrected = lifecycle.stored(a.id)
    assert corrected is not None
    assert (corrected.title, corrected.body) == (
        "a corrected memory",
        "the corrected body, about pears",
    )
    assert corrected.corrected_at == result.corrected_at
    # Current, not invalidated: a correction is the same record saying something else.
    assert corrected.invalidated_at is None and corrected.invalidation_reason is None
    for row, expected in ((b, 2), (c, 2)):
        marked = lifecycle.stored(row.id)
        assert marked is not None
        assert marked.invalidation_reason == _SOURCE_CORRECTED
        assert marked.invalidated_at is not None
        assert marked.revision == expected
    untouched = lifecycle.stored(d.id)
    assert untouched is not None and untouched.invalidation_reason is None


def test_an_about_link_carries_the_closure_one_hop_and_no_further(
    lifecycle: LifecycleWorkspace,
) -> None:
    """§ A5's traversal: either relation on the first hop, ``derived_from`` onward.

    ``b`` is *about* ``a`` and is invalidated; ``c`` is *about* ``b`` and is not,
    because being about something is not a claim to have been built out of it.
    """
    a = _seed(lifecycle, _row(title="the subject"))
    b = _seed(lifecycle, _row(title="about a"), links=[_derived(a, relation=_ABOUT)])
    c = _seed(lifecycle, _row(title="about b"), links=[_derived(b, relation=_ABOUT)])

    _corrected(_correct(lifecycle, lifecycle.context(), a))

    first = lifecycle.stored(b.id)
    second = lifecycle.stored(c.id)
    assert first is not None and first.invalidation_reason == _SOURCE_CORRECTED
    assert second is not None and second.invalidation_reason is None


def test_correction_publishes_invalidated_for_the_derivatives_and_not_the_target(
    lifecycle: LifecycleWorkspace,
) -> None:
    """One ``.invalidated`` per newly marked row, through the declared event.

    The declaration is read off the manifest rather than restated, so a run that
    changed the event's schema version without changing the publisher would red here.
    """
    a = _seed(lifecycle, _row())
    b = _seed(lifecycle, _row(), links=[_derived(a)])

    _corrected(_correct(lifecycle, lifecycle.context(), a))

    (declared,) = [
        event for event in MANIFEST.events if event.type == MEMORY_INVALIDATED
    ]
    events = lifecycle.events(MEMORY_INVALIDATED)
    assert [event["subject_ref"] for event in events] == [memory_reference(b.id)]
    (event,) = events
    assert event["schema_version"] == declared.schema_version == EVENT_SCHEMA_VERSION
    assert event["subject_revision"] == 2
    assert event["data"] == {
        "memory_ref": memory_reference(b.id),
        "kind": b.kind,
        "reason": _SOURCE_CORRECTED,
    }
    # The corrected row itself was never marked, so nothing was published for it.
    assert memory_reference(a.id) not in lifecycle.invalidated_refs()


def test_an_already_invalidated_derivative_is_not_marked_or_published_twice(
    lifecycle: LifecycleWorkspace,
) -> None:
    """ "Newly invalidated" is the rule, and this is what it excludes.

    ``b`` is corrected out from under a second correction of ``a``; the second pass
    reaches it, finds a reason already recorded, and leaves the row and the outbox
    alone rather than restamping either.
    """
    a = _seed(lifecycle, _row())
    b = _seed(lifecycle, _row(), links=[_derived(a)])
    ctx = lifecycle.context()
    first = _corrected(_correct(lifecycle, ctx, a))
    marked = lifecycle.stored(b.id)
    assert marked is not None

    _corrected(
        _correct(lifecycle, ctx, a, expected_revision=first.revision, title="again")
    )

    again = lifecycle.stored(b.id)
    assert again is not None
    assert again.revision == marked.revision
    assert again.invalidated_at == marked.invalidated_at
    assert len(lifecycle.events(MEMORY_INVALIDATED)) == 1


def test_correction_removes_the_embeddings_of_every_row_it_touches(
    lifecycle: LifecycleWorkspace,
) -> None:
    a = _seed(lifecycle, _row())
    b = _seed(lifecycle, _row(), links=[_derived(a)])
    c = _seed(lifecycle, _row())
    assert all(_embedded(lifecycle, row.id) for row in (a, b, c))

    _corrected(_correct(lifecycle, lifecycle.context(), a))

    assert not _embedded(lifecycle, a.id)
    assert not _embedded(lifecycle, b.id)
    assert _embedded(lifecycle, c.id)


# --- supersession ---------------------------------------------------------------------


def test_supersession_replaces_the_row_and_copies_what_it_was_allowed_to_reach(
    lifecycle: LifecycleWorkspace,
) -> None:
    """§ A5's four steps, read off the rows they wrote.

    The replacement takes the predecessor's audience and purposes — the meet of a
    one-source derivation is that source — copies its ordinary restrictions, and
    gains one marked ``derived_from`` naming the predecessor. The predecessor keeps
    its row, gains a revision, and points at its successor.
    """
    other = add_member(
        lifecycle.cluster.backend,
        lifecycle.workspace,
        Role.MEMBER,
        display_name="the-audience",
    )
    subject = _seed(lifecycle, _row(title="a linked memory"))
    predecessor = _seed(
        lifecycle,
        _row(audience_kind="member", audience_id=other, title="the original"),
        purposes=(_RESPOND, "internal_analysis"),
        links=[_derived(subject, relation=_ABOUT)],
    )
    ctx = lifecycle.context(account_id=other, role=Role.MEMBER)

    result = _superseded(_supersede(lifecycle, ctx, predecessor))

    replacement = result.replacement
    assert isinstance(replacement, MemoryWritten)
    assert replacement.audience == "member"
    assert replacement.purposes == ("internal_analysis", _RESPOND)
    assert replacement.revision == 1
    replacement_id = canonical_ref(replacement.ref).id
    assert lifecycle.links_of(replacement_id) == {
        (memory_reference(subject.id), _ABOUT, False),
        (memory_reference(predecessor.id), _DERIVED_FROM, True),
    }
    retired = lifecycle.stored(predecessor.id)
    assert retired is not None
    assert retired.revision == result.predecessor_revision == 2
    assert retired.invalidation_reason == _SOURCE_SUPERSEDED
    assert retired.superseded_by_id == replacement_id
    assert not _embedded(lifecycle, predecessor.id)
    # The replacement is current, and no event says otherwise.
    fresh = lifecycle.stored(replacement_id)
    assert fresh is not None
    assert fresh.invalidated_at is None and fresh.superseded_by_id is None
    assert replacement.ref not in lifecycle.invalidated_refs()


def test_the_replacement_never_appears_in_its_own_closure(
    lifecycle: LifecycleWorkspace,
) -> None:
    """Step 2 captures the closure before step 3 creates the row that joins it.

    The replacement carries a marked ``derived_from`` edge back to the predecessor,
    and marked edges are followed, so a closure captured after the insert would
    contain the replacement and invalidate it on the spot. Both a ``.recorded``
    with no matching ``.invalidated`` and a live row are asserted, because the two
    fail differently: a closure computed too late marks the row, and a closure that
    excluded marked edges altogether would pass one of them by accident.
    """
    predecessor = _seed(lifecycle, _row())
    derivative = _seed(lifecycle, _row(), links=[_derived(predecessor)])

    result = _superseded(_supersede(lifecycle, lifecycle.context(), predecessor))

    replacement_id = canonical_ref(result.replacement.ref).id
    fresh = lifecycle.stored(replacement_id)
    assert fresh is not None
    assert fresh.invalidated_at is None
    assert fresh.invalidation_reason is None
    assert result.replacement.ref in [
        event["subject_ref"] for event in lifecycle.events(MEMORY_RECORDED)
    ]
    assert sorted(lifecycle.invalidated_refs()) == sorted(
        [memory_reference(derivative.id), memory_reference(predecessor.id)]
    )


def test_supersession_marks_the_pre_existing_derivatives_and_bumps_each_revision(
    lifecycle: LifecycleWorkspace,
) -> None:
    """A row whose state changed carries a new revision, on this path as on the
    other.

    Without the bump, a caller holding the derivative's old revision would pass the
    compare-and-set against a row that has since been invalidated underneath it.
    """
    a = _seed(lifecycle, _row())
    b = _seed(lifecycle, _row(), links=[_derived(a)])
    c = _seed(lifecycle, _row(), links=[_derived(b)])

    _superseded(_supersede(lifecycle, lifecycle.context(), a))

    for row in (b, c):
        marked = lifecycle.stored(row.id)
        assert marked is not None
        assert marked.invalidation_reason == _SOURCE_SUPERSEDED
        assert marked.revision == 2
        assert not _embedded(lifecycle, row.id)


def test_a_replacement_carries_its_predecessors_ancestry_forward(
    lifecycle: LifecycleWorkspace,
) -> None:
    """A -> B -> C, with A and B then physically gone: C stays addressable from A.

    The expiry is seeded directly — the real sweep is a later prompt's — and the
    assertion is the one § A5 words as "copied ancestry keeps closure connected
    across missing middle rows": the traversal from A's reference still reaches C
    even though B, the row that linked them, is no longer there.
    :func:`dependent_closure` is called directly here because its subject is exactly
    that, over rows no live operation can reach any more.
    """
    a = _seed(lifecycle, _row(title="first"))
    ctx = lifecycle.context()
    b_ref = _superseded(_supersede(lifecycle, ctx, a, title="second")).replacement.ref
    b_id = canonical_ref(b_ref).id
    b_row = lifecycle.stored(b_id)
    assert b_row is not None
    c_ref = _superseded(
        _supersede(lifecycle, ctx, b_row, title="third")
    ).replacement.ref
    c_id = canonical_ref(c_ref).id

    assert lifecycle.links_of(c_id) == {
        (memory_reference(a.id), _DERIVED_FROM, True),
        (memory_reference(b_id), _DERIVED_FROM, True),
    }

    with lifecycle.unit() as uow:
        uow.connection.execute(
            text("DELETE FROM recallatron.memory WHERE id = ANY(:ids)"),
            {"ids": [a.id, b_id]},
        )

    surviving = lifecycle.stored(c_id)
    assert surviving is not None and surviving.invalidated_at is None
    with lifecycle.reading() as uow:
        assert dependent_closure(uow.connection, a.id, include_marked=True) == (c_id,)
        assert dependent_closure(uow.connection, a.id, include_marked=False) == ()


# --- user erasure ---------------------------------------------------------------------


def test_an_approved_erasure_removes_the_closure_and_counts_only_removed_rows(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The owner half, the participant half, and the ledger's fourth counter.

    ``a`` is the target, ``b`` and ``c`` are its ordinary derivative chain, ``d`` is
    unrelated. The count is three because the owner removed one row and the
    participant two: a coordinator that ran the owner and skipped the participants
    would leave ``b`` and ``c`` behind and claim one.
    """
    a = _seed(lifecycle, _row(title="the target"))
    b = _seed(lifecycle, _row(), links=[_derived(a)])
    c = _seed(lifecycle, _row(), links=[_derived(b)])
    d = _seed(lifecycle, _row(title="unrelated"))

    row = _erase(lifecycle, a.id)

    assert (
        row.cancelled_job_count,
        row.cancelled_action_count,
        row.removed_export_count,
        row.invalidated_memory_count,
    ) == (0, 0, 0, 3)
    assert row.cause == USER_ERASURE
    assert row.retained_successor_ref is None
    assert row.record_type == DELETION_PARTICIPANT_TYPES[0]
    assert row.participants == (_MEMORY_MODULE,)
    assert all(lifecycle.stored(row_id) is None for row_id in (a.id, b.id, c.id))
    assert lifecycle.stored(d.id) is not None
    # Embeddings go on this path too, by cascade rather than by statement — the
    # third of the three ways a row's vectors can stop existing, and the one no
    # line in the module spells out.
    assert not any(_embedded(lifecycle, item.id) for item in (a, b, c))
    assert _embedded(lifecycle, d.id)
    assert lifecycle.rows()["embedding"] == 1


def test_a_mixed_path_erases_the_row_once_and_counts_it_once(
    lifecycle: LifecycleWorkspace,
) -> None:
    """``c`` is reachable from ``a`` twice: by ordinary derivation and by ancestry.

    The ordinary dependency alone would erase it, and so would the marked edge; what
    the counter must not do is count it twice. A seen set is the only thing standing
    between those two paths and a doubled ledger.
    """
    a = _seed(lifecycle, _row(title="the target"))
    b = _seed(lifecycle, _row(), links=[_derived(a)])
    c = _seed(lifecycle, _row(), links=[_derived(b), _derived(a, lineage=True)])

    row = _erase(lifecycle, a.id)

    assert row.invalidated_memory_count == 3
    assert all(lifecycle.stored(item.id) is None for item in (a, b, c))


def test_erasure_prunes_an_unbacked_entity_and_terminalizes_its_receipt(
    lifecycle: LifecycleWorkspace,
) -> None:
    """Two consequences of removing a memory that only removing it can produce.

    The entity is pruned because it lost its last mention; the receipt is
    terminalized so a later replay of that identity cannot resurrect what somebody
    asked to have erased. Both are written in the deleting transaction — the receipt
    while the row's source key is still readable off it, the mention before the
    cascade would have taken it silently.
    """
    namespace = "0" * 64
    target = _row(source_namespace=namespace, external_source_key="unit-1")
    _seed(lifecycle, target)
    entity_id = uuid7()
    with lifecycle.unit() as uow:
        insert_memory_entity(
            uow.connection,
            MemoryEntityRow(
                id=entity_id,
                kind="person",
                name="Someone",
                normalized_name="someone",
                ref=None,
                created_at=target.recorded_at,
                source_namespace=None,
                external_source_key=None,
            ),
        )
        insert_memory_mention(
            uow.connection,
            MemoryMentionRow(memory_id=target.id, entity_id=entity_id, role=None),
        )
        insert_source_receipt(
            uow.connection,
            SourceReceiptRow(
                representation_type=REPRESENTATION_MEMORY,
                source_namespace=namespace,
                external_source_key="unit-1",
                record_id=target.id,
                payload_digest=b"\x00" * 32,
                state="active",
                producer_kind="rheo_runtime",
                authority_id=uuid7(),
                principal_account_id=None,
                audience_kind="workspace",
                audience_id=None,
                bound_purpose=None,
                source_recorded_at=target.recorded_at,
                source_expires_at=target.recorded_at + timedelta(days=30),
            ),
        )

    row = _erase(lifecycle, target.id)

    assert row.invalidated_memory_count == 1
    with lifecycle.reading() as uow:
        assert get_memory_entity(uow.connection, entity_id) is None
        receipt = get_source_receipt(
            uow.connection, REPRESENTATION_MEMORY, namespace, "unit-1"
        )
    assert receipt is not None
    assert receipt.state == "erased"
    # The pre-creation tombstone is kept: the identity did have a representation.
    assert receipt.record_id == target.id


def test_a_physically_removed_row_gets_no_invalidated_event(
    lifecycle: LifecycleWorkspace,
) -> None:
    """The two events never both fire for one row.

    A removed row is covered by the core's record-deleted event and by nothing of
    this module's: publishing ``.invalidated`` for it would tell a subscriber the row
    was retired when it is gone.
    """
    a = _seed(lifecycle, _row())
    b = _seed(lifecycle, _row(), links=[_derived(a)])

    _erase(lifecycle, a.id)

    assert lifecycle.events(MEMORY_INVALIDATED) == []
    deleted = [event["subject_ref"] for event in lifecycle.events(RECORD_DELETED)]
    assert deleted == [memory_reference(a.id)]
    assert memory_reference(b.id) not in deleted


def test_the_erasure_runs_as_the_held_caller_at_every_checkpoint(
    lifecycle: LifecycleWorkspace,
) -> None:
    """A member holds, the owner approves, and the member's own memory is erased.

    The target's audience is the **member's**, so the owner's context cannot
    authorize it: the owned-delete authorizer refuses a member-audience row to any
    account but that member's. A coordinator that ran the authorizer under the
    approver's context at execution or at coordinator entry would therefore refuse,
    and the deletion would not happen at all — which makes a successful erasure here
    the proof that all three checkpoints saw the rebuilt original caller. The
    ledger's actor is asserted as well, because it is the durable half of the same
    claim.
    """
    member_id = add_member(
        lifecycle.cluster.backend,
        lifecycle.workspace,
        Role.MEMBER,
        display_name="held-caller",
    )
    assert member_id != lifecycle.owner_account_id
    member = lifecycle.context(account_id=member_id, role=Role.MEMBER)
    target = _seed(lifecycle, _row(audience_kind="member", audience_id=member_id))
    # The owner genuinely cannot reach it: the same reference held by the owner never
    # mints an approval at all, which is the pre-mint checkpoint refusing.
    refused = lifecycle.core_call(
        lifecycle.context(), RECORD_DELETE, {"ref": memory_reference(target.id)}
    )
    _refused(refused, "not_found")
    assert refused.approval_id is None

    row = _erase(lifecycle, target.id, holder=member, approver=lifecycle.context())

    assert row.actor_id == member_id
    assert lifecycle.stored(target.id) is None


def test_no_lifecycle_answer_carries_the_size_of_a_closure(
    lifecycle: LifecycleWorkspace,
) -> None:
    """Asserted on the declared models and on one real member-driven erasure.

    On the models because the rule is about what these operations *can* return — a
    count added to one of them would leak the shape of somebody else's graph on
    every call, and no single response would look wrong — and on a real response
    because a field set proves nothing about what the coordinator actually handed
    back.
    """
    assert set(MemoryCorrected.model_fields) == {
        "ref",
        "kind",
        "revision",
        "corrected_at",
    }
    assert set(MemorySuperseded.model_fields) == {
        "replacement",
        "predecessor_ref",
        "predecessor_revision",
    }

    member_id = add_member(
        lifecycle.cluster.backend,
        lifecycle.workspace,
        Role.MEMBER,
        display_name="non-owner",
    )
    member = lifecycle.context(account_id=member_id, role=Role.MEMBER)
    a = _seed(lifecycle, _row(audience_kind="member", audience_id=member_id))
    b = _seed(
        lifecycle,
        _row(audience_kind="member", audience_id=member_id),
        links=[_derived(a)],
    )
    approval_id = _held(lifecycle, member, a.id)

    outcome = _approve(lifecycle, lifecycle.context(), approval_id)

    assert outcome.ok, outcome
    assert outcome.result is not None
    # The deletion's own declared answer is one reference, and the approval record
    # the approver actually receives carries no closure field either.
    assert set(RecordDeleted.model_fields) == {"deletion_ref"}
    answered = json.dumps(outcome.result.model_dump(), default=str)
    published = json.dumps(lifecycle.events(RECORD_DELETED))
    for rendered in (answered, published):
        assert str(b.id) not in rendered
        # ``_count`` rather than ``count``: every counter on the ledger row carries
        # that suffix, and the bare word is a substring of ``account``.
        assert "_count" not in rendered
    assert lifecycle.stored(b.id) is None


# --- the two provenance-writer lock orderings -----------------------------------------


def _parked(thread: threading.Thread, handler: str) -> bool:
    """Is ``thread`` sitting inside a database call from inside ``handler``?

    Read off the thread's own stack, which is the technique
    ``tests/postgres/test_module_enable.py`` established for this suite after a
    ``pg_stat_activity`` poll from the holder's own connection failed to see the
    waiting backend at all.
    """
    frame = sys._current_frames().get(thread.ident or -1)
    if frame is None:
        return False
    stack = traceback.extract_stack(frame)
    return any(entry.name == handler for entry in stack) and any(
        entry.name == "wait" and "psycopg" in entry.filename for entry in stack
    )


def _wait_until_parked(
    thread: threading.Thread, handler: str, *, timeout: float = 30.0
) -> bool:
    """Poll until ``thread`` is parked in a database call, and stays parked.

    Two samples a tenth of a second apart, because every statement passes through
    ``psycopg``'s ``wait`` on its way and one sample can catch a call merely in
    flight. A call still parked 100ms later is one that is waiting on something.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _parked(thread, handler):
            time.sleep(0.1)
            if _parked(thread, handler):
                return True
        time.sleep(0.02)
    return False


@contextmanager
def _holding_the_lifecycle_lock(memory: LifecycleWorkspace) -> Iterator[Any]:
    """A second connection holding the workspace lifecycle lock, in its own
    transaction.

    It takes the lock with its own statement rather than through
    ``lock_workspace_lifecycle``, for the reason ``test_module_enable.py`` records
    about its own holder: reaching for the shipped helper would disarm the *fixture*
    along with the subject, so a mutation that removed the lock from the handler
    under test would also remove it from the thing holding it.
    """
    with memory.engine.connect() as holder:
        with holder.begin():
            holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": WORKSPACE_LIFECYCLE_LOCK_KEY},
            )
            yield holder


def test_a_waiting_provenance_writer_rereads_its_sources_after_the_lock(
    lifecycle: LifecycleWorkspace,
) -> None:
    """A ``derive`` queued behind the lock sees the supersession that beat it.

    Without the lock in ``derive``, its source read runs while the holder's
    transaction is still open, sees the source as current under ``READ COMMITTED``,
    and writes a memory derived from a row that is no longer current — a dependency
    the closure that invalidated it has already walked past. With the lock, the read
    happens after the holder commits and the source is refused.
    """
    source = _seed(lifecycle, _row(title="the source"))
    ctx = lifecycle.context()
    results: list[OperationOutcome] = []

    with _holding_the_lifecycle_lock(lifecycle) as holder:
        caller = threading.Thread(
            target=lambda: results.append(
                lifecycle.call(
                    ctx,
                    MEMORY_DERIVE,
                    {
                        "sources": (memory_reference(source.id),),
                        "kind": "note",
                        "title": "a derivation",
                        "body": "built on the source",
                    },
                )
            )
        )
        caller.start()
        assert _wait_until_parked(caller, "derive"), (
            "the derive never parked in a database call inside its handler, so it "
            "never queued behind the lifecycle lock"
        )
        assert caller.is_alive(), (
            "the derive finished while another transaction held the lifecycle lock, "
            "so it never waited for it"
        )
        holder.execute(
            text(
                "UPDATE recallatron.memory SET invalidated_at = now(), "
                "invalidation_reason = 'source_superseded', revision = revision + 1 "
                "WHERE id = :id"
            ),
            {"id": source.id},
        )
    caller.join(timeout=60)
    assert not caller.is_alive(), "the derive never resumed after the lock cleared"

    (outcome,) = results
    _refused(outcome, "not_found")
    assert lifecycle.rows()["memory"] == 1


def test_a_writer_committed_before_the_closure_is_visible_to_it(
    lifecycle: LifecycleWorkspace,
) -> None:
    """A ``correct`` queued behind the lock invalidates the derivative that beat it.

    The other ordering, and the other half of § A7's sentence. Without the lock in
    ``correct``, the closure is captured before the holder commits, the new
    derivative is not in it, and a memory built on the old text survives the
    correction of that text with nothing marking it.
    """
    target = _seed(lifecycle, _row(title="the target"))
    ctx = lifecycle.context()
    late = _row(title="derived while the closure waited")
    results: list[OperationOutcome] = []

    with _holding_the_lifecycle_lock(lifecycle) as holder:
        caller = threading.Thread(
            target=lambda: results.append(_correct(lifecycle, ctx, target))
        )
        caller.start()
        assert _wait_until_parked(caller, "correct"), (
            "the correction never parked in a database call inside its handler, so "
            "it never queued behind the lifecycle lock"
        )
        assert caller.is_alive(), (
            "the correction finished while another transaction held the lifecycle "
            "lock, so it never waited for it"
        )
        insert_memory(holder, late)
        insert_memory_purpose(
            holder, MemoryPurposeRow(memory_id=late.id, purpose=_RESPOND)
        )
        insert_memory_link(
            holder,
            MemoryLinkRow(
                memory_id=late.id,
                ref=memory_reference(target.id),
                relation=_DERIVED_FROM,
                created_at=late.recorded_at,
                supersession_lineage=False,
            ),
        )
    caller.join(timeout=60)
    assert not caller.is_alive(), "the correction never resumed after the lock cleared"

    (outcome,) = results
    _corrected(outcome)
    marked = lifecycle.stored(late.id)
    assert marked is not None, "the row the holder committed is gone"
    assert marked.invalidation_reason == _SOURCE_CORRECTED
    assert lifecycle.invalidated_refs() == [memory_reference(late.id)]
