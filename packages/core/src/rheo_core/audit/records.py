"""The ``core.audit_record`` repository: the insert a sink makes, and the read
``core.audit.list`` publishes.

Shaped after ``operations/records.py``, deliberately and in the respects that matter:
each function takes the caller's ``Connection`` and runs inside the caller's
transaction, **neither :func:`insert_audit_record` nor :func:`list_audit_records`
commits**, and ``occurred_at`` is an explicit keyword rather than a clock read. The
last one is what lets the success-path row commit with the operation's own effects —
a commit in here would end the operation's transaction underneath it — and what keeps
the process clock out of a repository, exactly as ``operations/records.py`` states for
its own writes.

**The outcome names are the DDL's tuple spelled twice, and the two are compared at
import time.** ``work_tables.AUDIT_OUTCOMES`` is the check constraint's own list;
:data:`AuditOutcome` is the ``Literal`` that puts a real enum in the generated OpenAPI
document and makes an unknown value a type error rather than a driver error at the
INSERT. ``operations/operation_ops.py`` already runs this same check over its two
aliases, and this is the same pattern for the same reason.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, get_args
from uuid import UUID

from sqlalchemy import Connection, Row, insert, select

from rheo_core.refs import uuid7
from rheo_core.storage import work_tables as t

AUDIT_SUCCEEDED: Final = "succeeded"
AUDIT_FAILED: Final = "failed"
AUDIT_REFUSED: Final = "refused"
"""The three members of the ``audit_record_outcome`` check constraint
(``work_tables.AUDIT_OUTCOMES``), named here so no caller spells one by hand. The
``AUDIT_`` prefix is not decoration: ``operations/refusals.py`` exports ``SUCCEEDED``
and ``FAILED`` as *dispatch outcome states*, which are a different vocabulary that
happens to share two spellings, and the dispatcher imports both."""

AuditOutcome = Literal["succeeded", "failed", "refused"]

if set(get_args(AuditOutcome)) != set(  # pragma: no cover - import-time invariant
    t.AUDIT_OUTCOMES
):
    raise RuntimeError(
        "AuditOutcome and work_tables.AUDIT_OUTCOMES disagree; they are the same set "
        "spelled twice and must be changed together"
    )

if {AUDIT_SUCCEEDED, AUDIT_FAILED, AUDIT_REFUSED} != set(  # pragma: no cover
    t.AUDIT_OUTCOMES
):
    raise RuntimeError(
        "this module's outcome constants and work_tables.AUDIT_OUTCOMES disagree"
    )


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One ``core.audit_record`` row as its supported read publishes it.

    Every column of the table, because unlike ``core.operation`` there is no
    provenance here a reader already knows: the whole content of an audit record is
    who acted, through what, on what, and how it came out.
    """

    id: UUID
    occurred_at: datetime
    actor_kind: str
    actor_id: UUID | None
    entry: str
    operation_name: str
    safety_class: str
    operation_id: UUID | None
    subject_ref: str | None
    request_digest: bytes
    outcome: str


_COLUMNS: Final = (
    t.audit_record.c.id,
    t.audit_record.c.occurred_at,
    t.audit_record.c.actor_kind,
    t.audit_record.c.actor_id,
    t.audit_record.c.entry,
    t.audit_record.c.operation_name,
    t.audit_record.c.safety_class,
    t.audit_record.c.operation_id,
    t.audit_record.c.subject_ref,
    t.audit_record.c.request_digest,
    t.audit_record.c.outcome,
)
"""The read's select list, named once so :func:`_row` has one row shape to map."""


def _row(row: Row[Any]) -> AuditRow:
    """One selected row as an :class:`AuditRow`, field for field.

    Written out rather than splatted, for the reason ``operations/records.py``'s own
    ``_row`` gives: a splat pairs the wrong column with the wrong field the moment
    :data:`_COLUMNS` and the dataclass drift apart, which is the one defect this
    mapping can have.
    """
    return AuditRow(
        id=row.id,
        occurred_at=row.occurred_at,
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        entry=str(row.entry),
        operation_name=str(row.operation_name),
        safety_class=str(row.safety_class),
        operation_id=row.operation_id,
        subject_ref=None if row.subject_ref is None else str(row.subject_ref),
        request_digest=bytes(row.request_digest),
        outcome=str(row.outcome),
    )


def insert_audit_record(
    conn: Connection,
    *,
    occurred_at: datetime,
    actor_kind: str,
    actor_id: UUID | None,
    entry: str,
    operation_name: str,
    safety_class: str,
    operation_id: UUID | None,
    subject_ref: str | None,
    request_digest: bytes,
    outcome: AuditOutcome,
) -> UUID:
    """Write one audit row and return its id. Does not commit.

    Every argument is required and none has a default. A default on ``outcome`` or
    ``request_digest`` would let a call site omit the two columns that carry the whole
    meaning of the row and satisfy the database's NOT NULL by accident; the same
    reasoning ``operations/records.py``'s ``finish_succeeded`` gives for its terminal
    check fields.
    """
    audit_id = uuid7()
    conn.execute(
        insert(t.audit_record).values(
            id=audit_id,
            occurred_at=occurred_at,
            actor_kind=actor_kind,
            actor_id=actor_id,
            entry=entry,
            operation_name=operation_name,
            safety_class=safety_class,
            operation_id=operation_id,
            subject_ref=subject_ref,
            request_digest=request_digest,
            outcome=outcome,
        )
    )
    return audit_id


def list_audit_records(conn: Connection, *, limit: int) -> tuple[AuditRow, ...]:
    """The most recent audit records, newest ``occurred_at`` first, capped at ``limit``.

    ``id`` breaks a tie on ``occurred_at`` for the reason ``list_recent`` gives one
    layer over: the ids are uuid7, so descending id is the same ordering
    ``occurred_at`` intends, and an unordered tie makes the page a caller reads depend
    on the plan Postgres happened to choose. Ties are ordinary here rather than
    exotic — a dispatch that refuses and a dispatch that succeeds can be written from
    the same microsecond in a busy workspace.

    ``limit`` is bounded by the caller's input model, never here, exactly as
    ``work/operations.py``'s ``FailureListInput`` arranges for the failure list.
    """
    rows = conn.execute(
        select(*_COLUMNS)
        .order_by(t.audit_record.c.occurred_at.desc(), t.audit_record.c.id.desc())
        .limit(limit)
    )
    return tuple(_row(row) for row in rows)
