"""``harness.probe``: the synthetic owned record type the deletion coordinator is
driven against, with its resolver, its owned-delete declaration and one participant.

Recallatron does not own a record type yet, and ``core`` cannot own one at all (the
segment is reserved), so without a synthetic owner ``core.record.delete`` would have
nothing to delete and its whole cascade would be untested. This module is that owner,
registered under ``origin = test_harness`` and therefore only ever under
``profile = test`` — the same gate ``harness.note`` is behind.

**Two tables, and the split is the point.** ``harness.probe`` is the record; each
``harness.probe_memory`` row stands in for a memory row physically removed *because*
of the deletion, which is what ``core.deletion_record.invalidated_memory_count``
counts. Rows with ``derived = false`` are the owner's own closure and go with
``delete_owned``; rows with ``derived = true`` are somebody else's and go with the
participant. A test that wrote two of each therefore expects a count of four, from two
different callables, which is what makes the union in the coordinator observable.

**The participant's failure mode is data, not a second registration.** The registry is
process-global (there is nowhere else the coordinator could read it from), so a test
that registered a raising participant would leave it registered for every test after
it. Instead one participant is registered once, idempotently, and raises when its own
rows carry :data:`MODE_PARTICIPANT_RAISES` — so the failure is a property of the probe
a test creates rather than of the process it runs in.

**The revision is the constant 1**, exactly as ``harness.note``'s resolver reports:
this harness has no revision machinery, and the coordinator's revision comparison is
exercised against the *owner's* answer either side of the lifecycle lock rather than
against a moving number.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    OWNED_DELETIONS,
    DeleteAuthorization,
    OwnedDeletion,
    OwnedDeletionRegistry,
    RemovedMemories,
)
from rheo_core.operations import HARNESS_MODULE_ID
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import (
    LIVE,
    NOT_FOUND,
    RESOLVERS,
    RecordHead,
    ResolverRegistry,
    Unavailable,
)
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    DateTime,
    Table,
    Text,
    delete,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from harness.records import HARNESS_SCHEMA, harness_metadata

PROBE_RECORD_TYPE: Final = "probe"
PROBE_REVISION: Final = 1
PROBE_UNAUTHORIZED: Final = "probe_unauthorized"
"""The owner's *own* refusal, which the coordinator passes through unchanged.

Distinct from ``rheo_core.deletion.RECORD_NOT_DELETABLE``, which is the generic one
core answers for a type nobody owns or a module this workspace has not enabled — the
two being distinguishable is what lets a test tell "the authorizer ran and said no"
from "the authorizer was never reached".
"""

MODE_OK: Final = "ok"
MODE_PARTICIPANT_RAISES: Final = "participant_raises"

PARTICIPANT_FAILURE: Final = "the probe participant was told to fail"
"""The message the raising participant carries, so a test can recognise its own
injected failure rather than any exception at all."""

probe = Table(
    "probe",
    harness_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("label", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

probe_memory = Table(
    "probe_memory",
    harness_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("probe_id", PG_UUID(as_uuid=True), nullable=False),
    Column("derived", Boolean, nullable=False),
    # Copied from the probe at creation rather than read back off it: the participant
    # runs *after* the owner has deleted the probe row, so a mode stored only there
    # would be unreadable at exactly the moment it is needed.
    Column("mode", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class ProbeRow:
    id: UUID
    label: str
    owned_memory_ids: tuple[UUID, ...]
    derived_memory_ids: tuple[UUID, ...]

    @property
    def memory_ids(self) -> frozenset[UUID]:
        return frozenset(self.owned_memory_ids) | frozenset(self.derived_memory_ids)


def probe_ref(probe_id: UUID) -> RecordRef:
    return RecordRef(
        module=HARNESS_MODULE_ID, record_type=PROBE_RECORD_TYPE, id=probe_id
    )


def ensure_probe_tables(conn: Connection) -> None:
    """Create both tables in the connection's database if they are absent."""
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {HARNESS_SCHEMA}"))
    probe.create(conn, checkfirst=True)
    probe_memory.create(conn, checkfirst=True)


def write_probe(
    conn: Connection,
    *,
    label: str = "a deletable probe",
    owned: int = 2,
    derived: int = 2,
    mode: str = MODE_OK,
) -> ProbeRow:
    """One probe with ``owned`` of its own memory rows and ``derived`` of somebody
    else's."""
    now = datetime.now(UTC)
    probe_id = uuid7()
    conn.execute(insert(probe).values(id=probe_id, label=label, created_at=now))
    owned_ids = tuple(uuid7() for _ in range(owned))
    derived_ids = tuple(uuid7() for _ in range(derived))
    rows = [
        {
            "id": memory_id,
            "probe_id": probe_id,
            "derived": is_derived,
            "mode": mode,
            "created_at": now,
        }
        for is_derived, ids in ((False, owned_ids), (True, derived_ids))
        for memory_id in ids
    ]
    if rows:
        conn.execute(insert(probe_memory), rows)
    return ProbeRow(probe_id, label, owned_ids, derived_ids)


def probe_exists(conn: Connection, probe_id: UUID) -> bool:
    if not inspect(conn).has_table(PROBE_RECORD_TYPE, schema=HARNESS_SCHEMA):
        return False
    return (
        conn.execute(select(probe.c.id).where(probe.c.id == probe_id)).first()
        is not None
    )


def surviving_memory_ids(conn: Connection, probe_id: UUID) -> frozenset[UUID]:
    """Every ``harness.probe_memory`` row still present for ``probe_id``."""
    if not inspect(conn).has_table("probe_memory", schema=HARNESS_SCHEMA):
        return frozenset()
    return frozenset(
        row.id
        for row in conn.execute(
            select(probe_memory.c.id).where(probe_memory.c.probe_id == probe_id)
        )
    )


# --- the owned-delete declaration -----------------------------------------------------


def resolve_probe(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    """The ``harness.probe`` resolver, so ``RecordStateGuard`` has a record to guard.

    An upper-class declaration that names a subject field gets that guard attached, so
    without a resolver every approved deletion would refuse "no longer resolves" before
    the coordinator ran. A deleted probe is ``not_found``, which is what makes the
    guard bite on a second approval of the same reference.
    """
    if not probe_exists(uow.connection, ref.id):
        return Unavailable(ref.format(), NOT_FOUND)
    row = uow.connection.execute(
        select(probe.c.label).where(probe.c.id == ref.id)
    ).one()
    return RecordHead(
        ref=ref,
        display=str(row.label)[:40],
        readable=True,
        state=LIVE,
        revision=PROBE_REVISION,
    )


def authorize_probe_delete(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> DeleteAuthorization | Refusal:
    """Content-free: the reference and a revision, or a refusal naming neither.

    The narrow check a type owner defines. It reads whether the row is there and
    nothing about what is in it — no label, no memory body — because an authorizer that
    returned content would make the pre-approval check a read of the record it is
    about to refuse to delete.
    """
    if not probe_exists(uow.connection, ref.id):
        return Refusal(
            PROBE_UNAUTHORIZED, f"{ref.format()} is not a probe this owner will delete"
        )
    return DeleteAuthorization(ref=ref, revision=PROBE_REVISION)


def delete_probe_owned(
    ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
) -> RemovedMemories:
    """Remove the probe and the memory rows the probe itself owns.

    The derived rows are deliberately left for the participant: a coordinator that
    ran the owner and skipped the participants would leave them behind, and the
    count would be short.
    """
    probe_id = authorization.ref.id
    conn = uow.connection
    removed = frozenset(
        row.id
        for row in conn.execute(
            select(probe_memory.c.id).where(
                (probe_memory.c.probe_id == probe_id)
                & (probe_memory.c.derived.is_(False))
            )
        )
    )
    conn.execute(
        delete(probe_memory).where(
            (probe_memory.c.probe_id == probe_id) & (probe_memory.c.derived.is_(False))
        )
    )
    conn.execute(delete(probe).where(probe.c.id == probe_id))
    return RemovedMemories(removed)


def on_probe_deleted(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RemovedMemories:
    """The participant: remove the derived rows, or raise when told to.

    Raising is how a test drives "a participant that raises aborts the whole deletion;
    the record survives untouched" — and it raises *before* its own delete, so the only
    writes in the transaction when it fails are the owner's, which is the harder case
    for the rollback to get right.
    """
    conn = uow.connection
    rows = tuple(
        conn.execute(
            select(probe_memory.c.id, probe_memory.c.mode).where(
                (probe_memory.c.probe_id == ref.id) & (probe_memory.c.derived.is_(True))
            )
        )
    )
    if any(str(row.mode) == MODE_PARTICIPANT_RAISES for row in rows):
        raise RuntimeError(PARTICIPANT_FAILURE)
    removed = frozenset(row.id for row in rows)
    conn.execute(
        delete(probe_memory).where(
            (probe_memory.c.probe_id == ref.id) & (probe_memory.c.derived.is_(True))
        )
    )
    return RemovedMemories(removed)


PROBE_DELETION: Final = OwnedDeletion(
    module_id=HARNESS_MODULE_ID,
    record_type=PROBE_RECORD_TYPE,
    authorize_delete=authorize_probe_delete,
    delete_owned=delete_probe_owned,
)


def register_deletion_harness(
    *,
    resolvers: ResolverRegistry = RESOLVERS,
    deletions: OwnedDeletionRegistry = OWNED_DELETIONS,
) -> None:
    """Register the probe resolver, its owned-delete declaration and its participant.

    Idempotent in all three, like :func:`harness.registry.register_harness`, and
    explicit rather than an import side effect: nothing under ``rheo_core`` knows this
    record type exists.
    """
    resolvers.register(
        HARNESS_MODULE_ID, PROBE_RECORD_TYPE, resolve_probe, origin=TEST_HARNESS_ORIGIN
    )
    deletions.register(PROBE_DELETION, origin=TEST_HARNESS_ORIGIN)
    deletions.register_participant(
        HARNESS_MODULE_ID,
        (f"{HARNESS_MODULE_ID}.{PROBE_RECORD_TYPE}",),
        on_probe_deleted,
    )
