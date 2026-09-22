"""Every read and write against ``core.deletion_record``: one insert and one read.

Shaped after ``approvals/records.py`` and ``operations/records.py`` in the two respects
that matter: each function takes the caller's ``Connection`` and runs inside the
caller's transaction, and **nothing here commits**. The ledger row, the owning delete,
the participants and the gated operation's audit row are one transaction by
construction, so a commit in here would end that transaction underneath its owner.

**There are exactly two writers, and only one of them chooses an identity.**
:func:`insert_deletion_record` is the normal path and generates ``id`` and
``deleted_at`` itself. The ratified rule is that only a validated import may supply
them verbatim, and that import now exists — restore — so it has its own writer,
:func:`insert_imported_deletion_record`, rather than an optional pair of parameters on
the normal one. Two functions rather than two modes because the difference is not a
default: a caller reaching the verbatim writer is declaring that it has already
validated an entire artifact, and a backdating channel hidden behind a keyword on the
ordinary insert is exactly what the original single-writer note refused.

**The three phase-three counters are written as literal zeros and take no parameter.**
``cancelled_job_count``, ``cancelled_action_count`` and ``removed_export_count`` count
the job, external-action and held-export cleanup that criterion 65 owns and that this
tree does not implement. A parameter for each would be three ways for a caller to
claim a cascade that did not run; a literal zero is the honest value and makes the
claim mechanical. The run that builds that cleanup widens this function.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from rheo_contracts import RecordRef
from sqlalchemy import Connection, Row, insert, select

from rheo_core.deletion import tables as t
from rheo_core.refs import uuid7

UNIMPLEMENTED_CASCADE_COUNT: Final = 0
"""What the three phase-three counters hold, named rather than typed as a bare ``0``.

``docs/architecture/deletion-export-migration.md`` § The cascade steps 4 and 5 describe
cancelling queued jobs and pending external actions and marking held exports; none of
that is built, so the honest count is zero and the reader of this constant learns *why*
it is zero rather than reading a literal that looks like a measurement.
"""


@dataclass(frozen=True, slots=True)
class DeletionRecordRow:
    """One ``core.deletion_record`` row, field for field.

    Every column, because every column is content-free: there is nothing on this row
    a narrower published shape would be protecting.
    """

    id: UUID
    deleted_at: datetime
    record_type: str
    record_id: UUID
    actor_kind: str
    actor_id: UUID | None
    approval_id: UUID | None
    participants: tuple[str, ...]
    cancelled_job_count: int
    cancelled_action_count: int
    removed_export_count: int
    invalidated_memory_count: int
    cause: str
    retained_successor_ref: str | None


def deletion_ref(deletion_id: UUID) -> RecordRef:
    """The canonical reference to one ledger row.

    The only identifier a caller of ``core.record.delete`` receives, so it is minted
    from one place rather than formatted at each call site.
    """
    return RecordRef(module="core", record_type="deletion_record", id=deletion_id)


def _row(row: Row[Any]) -> DeletionRecordRow:
    """One selected row, written out rather than splatted.

    ``operations/records.py``'s own ``_row`` gives the reason: a splat pairs the wrong
    column with the wrong field the moment the select list and the dataclass drift.
    """
    return DeletionRecordRow(
        id=row.id,
        deleted_at=row.deleted_at,
        record_type=str(row.record_type),
        record_id=row.record_id,
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        approval_id=row.approval_id,
        participants=tuple(str(item) for item in row.participants),
        cancelled_job_count=int(row.cancelled_job_count),
        cancelled_action_count=int(row.cancelled_action_count),
        removed_export_count=int(row.removed_export_count),
        invalidated_memory_count=int(row.invalidated_memory_count),
        cause=str(row.cause),
        retained_successor_ref=(
            None
            if row.retained_successor_ref is None
            else str(row.retained_successor_ref)
        ),
    )


def insert_deletion_record(
    conn: Connection,
    *,
    ref: RecordRef,
    actor_kind: str,
    actor_id: UUID | None,
    approval_id: UUID | None,
    participants: tuple[str, ...],
    invalidated_memory_count: int,
    cause: str,
    retained_successor_ref: str | None,
    now: datetime,
) -> UUID:
    """Write one ledger row in the caller's transaction; return its id.

    ``ref`` is split into the qualified type and the record id here rather than by the
    caller, so the two columns cannot be filled from two different references.
    """
    deletion_id = uuid7()
    conn.execute(
        insert(t.deletion_record).values(
            id=deletion_id,
            deleted_at=now,
            record_type=f"{ref.module}.{ref.record_type}",
            record_id=ref.id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            approval_id=approval_id,
            participants=list(participants),
            cancelled_job_count=UNIMPLEMENTED_CASCADE_COUNT,
            cancelled_action_count=UNIMPLEMENTED_CASCADE_COUNT,
            removed_export_count=UNIMPLEMENTED_CASCADE_COUNT,
            invalidated_memory_count=invalidated_memory_count,
            cause=cause,
            retained_successor_ref=retained_successor_ref,
        )
    )
    return deletion_id


def insert_imported_deletion_record(conn: Connection, row: DeletionRecordRow) -> None:
    """Write one validated artifact row back **verbatim**, identity and clock included.

    The export's deletion-evidence category is part of what a restored workspace is
    compared against, and the comparison is over the bytes: a re-minted ``id`` or a
    ``deleted_at`` taken from this restore's clock would make a faithful restore
    report a different digest from its own source, and would also re-date every
    erasure the ledger is the proof of. Both columns therefore travel and neither is
    regenerated.

    Nothing else is relaxed. The columns are the same columns, the two check
    constraints still apply, and the caller is expected to have validated the whole
    artifact first — this function performs no validation of its own precisely so
    that the validation lives in one place instead of being half here.
    """
    conn.execute(
        insert(t.deletion_record).values(
            id=row.id,
            deleted_at=row.deleted_at,
            record_type=row.record_type,
            record_id=row.record_id,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            approval_id=row.approval_id,
            participants=list(row.participants),
            cancelled_job_count=row.cancelled_job_count,
            cancelled_action_count=row.cancelled_action_count,
            removed_export_count=row.removed_export_count,
            invalidated_memory_count=row.invalidated_memory_count,
            cause=row.cause,
            retained_successor_ref=row.retained_successor_ref,
        )
    )


def get_deletion_record(
    conn: Connection, *, deletion_id: UUID
) -> DeletionRecordRow | None:
    """The ledger row for ``deletion_id``, or ``None`` when this workspace has none."""
    found = conn.execute(
        select(t.deletion_record).where(t.deletion_record.c.id == deletion_id)
    ).first()
    return None if found is None else _row(found)


def list_deletion_records(
    conn: Connection, *, limit: int | None = None
) -> tuple[DeletionRecordRow, ...]:
    """Every ledger row, oldest first, or the ``limit`` most recent, newest first.

    Two orderings out of one function because the two readers want opposite things
    and neither should re-sort the other's answer. The export category wants the whole
    ledger in a **stable ascending** order, because its bytes are compared against a
    restored copy of themselves and any ordering that depends on how many rows exist
    would make the digest depend on the page. The audit read wants the most recent
    ``limit`` rows newest first, exactly as every other bounded collection on that
    operation does.

    ``id`` is a UUIDv7, so ascending id is ascending mint time and no second sort key
    is needed to break a tie inside one ``deleted_at``.
    """
    statement = select(t.deletion_record)
    if limit is None:
        statement = statement.order_by(t.deletion_record.c.id)
    else:
        statement = statement.order_by(t.deletion_record.c.id.desc()).limit(limit)
    return tuple(_row(found) for found in conn.execute(statement).all())
