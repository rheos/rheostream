"""Every read and write against ``core.approval`` and ``core.approval_payload``: the
mint, the three state transitions this chunk can reach, and the two reads.

Shaped after ``operations/records.py``, deliberately and in every respect that
matters: each function takes the caller's ``Connection`` and runs inside the caller's
transaction, **none of the writes below commits**, and ``now`` is always an explicit
keyword so nothing here reads the process clock. The gated call's approval row, its
payload snapshot and the ``core.operation`` record that holds it are written in one
transaction by ``approvals/gate.py``, and a commit in here would end that transaction
underneath it.

**Every transition is predicated on the state it may leave**, and each returns whether
it applied — :func:`mark_approved` on ``pending``, :func:`mark_refused` on ``pending``,
:func:`mark_executed` on ``approved``. That is ``operations/records.py``'s rule with a
different predicate for the same reason: a conditional write that reports whether it
matched leaves the decision about a zero rowcount to the call site, which is the only
place that knows what one means. Here it means a second approver got there first, and
the call site turns it into a refusal rather than executing twice.

**Who refused is not a column here.** The ratified column list gives the row
``approved_by_kind``, ``approved_by_id``, ``approved_at`` and ``approved_entry`` and
no refusal counterpart, so :func:`mark_refused` writes the state and nothing else;
the actor, the entry and the instant of a refusal are on the ``core.audit_record`` row
``core.approval.refuse`` writes like any other mutate operation. Recording a refusal
in columns named for an approval would be worse than not recording it twice.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Row, insert, select, update
from sqlalchemy.sql.elements import ColumnElement

from rheo_core.approvals import tables as t
from rheo_core.refs import uuid7


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    """One ``core.approval`` row as the supported reads publish it.

    Every column of the table except ``payload_ref``, which is this row's own id
    while a snapshot exists and carries nothing a reader of the row could act on:
    the snapshot itself is read through :func:`payload_body`, which answers ``None``
    for exactly the case ``payload_ref`` is null for.
    """

    id: UUID
    actor_kind: str
    actor_id: UUID | None
    operation_name: str
    operation_id: UUID
    destination_ref: str | None
    subject_ref: str | None
    subject_revision: int | None
    payload_digest: bytes
    purpose: str | None
    window_start: datetime
    window_end: datetime
    state: str
    approved_by_kind: str | None
    approved_by_id: UUID | None
    approved_at: datetime | None
    approved_entry: str | None
    executed_at: datetime | None
    invalidated_reason: str | None


_COLUMNS: Final = (
    t.approval.c.id,
    t.approval.c.actor_kind,
    t.approval.c.actor_id,
    t.approval.c.operation_name,
    t.approval.c.operation_id,
    t.approval.c.destination_ref,
    t.approval.c.subject_ref,
    t.approval.c.subject_revision,
    t.approval.c.payload_digest,
    t.approval.c.purpose,
    t.approval.c.window_start,
    t.approval.c.window_end,
    t.approval.c.state,
    t.approval.c.approved_by_kind,
    t.approval.c.approved_by_id,
    t.approval.c.approved_at,
    t.approval.c.approved_entry,
    t.approval.c.executed_at,
    t.approval.c.invalidated_reason,
)
"""The columns every read selects. One tuple rather than a select list per read, so
:func:`_row` has exactly one row shape to map — ``operations/records.py``'s own
reasoning for the same constant."""


def _row(row: Row[Any]) -> ApprovalRow:
    """One selected row as an :class:`ApprovalRow`, field for field.

    Written out rather than splatted, for the reason ``operations/records.py``'s own
    ``_row`` gives: a splat pairs the wrong column with the wrong field the moment
    :data:`_COLUMNS` and the dataclass drift apart.
    """
    return ApprovalRow(
        id=row.id,
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        operation_name=str(row.operation_name),
        operation_id=row.operation_id,
        destination_ref=(
            None if row.destination_ref is None else str(row.destination_ref)
        ),
        subject_ref=None if row.subject_ref is None else str(row.subject_ref),
        subject_revision=row.subject_revision,
        payload_digest=bytes(row.payload_digest),
        purpose=None if row.purpose is None else str(row.purpose),
        window_start=row.window_start,
        window_end=row.window_end,
        state=str(row.state),
        approved_by_kind=(
            None if row.approved_by_kind is None else str(row.approved_by_kind)
        ),
        approved_by_id=row.approved_by_id,
        approved_at=row.approved_at,
        approved_entry=(
            None if row.approved_entry is None else str(row.approved_entry)
        ),
        executed_at=row.executed_at,
        invalidated_reason=(
            None if row.invalidated_reason is None else str(row.invalidated_reason)
        ),
    )


def _apply(conn: Connection, where: ColumnElement[bool], **values: object) -> bool:
    """Run one predicated ``UPDATE`` on ``core.approval`` and report whether it
    matched."""
    return conn.execute(update(t.approval).where(where).values(**values)).rowcount == 1


def _in_state(approval_id: UUID, state: str) -> ColumnElement[bool]:
    """This approval, still in ``state``."""
    return (t.approval.c.id == approval_id) & (t.approval.c.state == state)


def create(
    conn: Connection,
    *,
    actor_kind: str,
    actor_id: UUID | None,
    operation_name: str,
    operation_id: UUID,
    destination_ref: str | None,
    subject_ref: str | None,
    subject_revision: int | None,
    payload_digest: bytes,
    body: Mapping[str, object],
    byte_length: int,
    purpose: str | None,
    window_start: datetime,
    window_end: datetime,
) -> UUID:
    """Write one ``pending`` approval and its payload snapshot; return its id. Does
    not commit.

    Both rows, always: the snapshot is what the interface shows at the point of
    confirmation, and an approval whose payload was written separately could exist
    without one. ``payload_ref`` is set to the approval's own id here and is cleared
    only when the deletion cascade removes the snapshot, which is the whole of what
    that column tells a reader.

    Everything the state machine has not reached is null — nobody has approved,
    nothing has executed, nothing invalidated it — which is what makes ``pending``
    the representable first state.
    """
    approval_id = uuid7()
    conn.execute(
        insert(t.approval).values(
            id=approval_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_name=operation_name,
            operation_id=operation_id,
            destination_ref=destination_ref,
            subject_ref=subject_ref,
            subject_revision=subject_revision,
            payload_digest=payload_digest,
            payload_ref=approval_id,
            purpose=purpose,
            window_start=window_start,
            window_end=window_end,
            state=t.PENDING,
            approved_by_kind=None,
            approved_by_id=None,
            approved_at=None,
            approved_entry=None,
            executed_at=None,
            invalidated_reason=None,
        )
    )
    conn.execute(
        insert(t.approval_payload).values(
            approval_id=approval_id, body=dict(body), byte_length=byte_length
        )
    )
    return approval_id


def mark_approved(
    conn: Connection,
    *,
    approval_id: UUID,
    approved_by_kind: str,
    approved_by_id: UUID | None,
    approved_entry: str,
    now: datetime,
) -> bool:
    """Approve a new or restored approval, recording who and through which entry."""
    return _apply(
        conn,
        (t.approval.c.id == approval_id)
        & t.approval.c.state.in_((t.PENDING, t.REQUIRES_REAPPROVAL)),
        state=t.APPROVED,
        approved_by_kind=approved_by_kind,
        approved_by_id=approved_by_id,
        approved_entry=approved_entry,
        approved_at=now,
    )


def mark_refused(conn: Connection, *, approval_id: UUID) -> bool:
    """Move a ``pending`` approval to ``refused``. Reports whether it applied.

    No instant and no actor: see the module docstring — the row has no columns for
    either on this path, and the audit row for ``core.approval.refuse`` carries both.
    """
    return _apply(
        conn,
        (t.approval.c.id == approval_id)
        & t.approval.c.state.in_((t.PENDING, t.REQUIRES_REAPPROVAL)),
        state=t.REFUSED,
    )


def mark_guard_refused(conn: Connection, *, approval_id: UUID) -> bool:
    """Move an ``approved`` approval to ``refused`` because a guard refused its
    execution. Reports whether it applied.

    A second transition into ``refused``, predicated on ``approved`` rather than on
    ``pending``, because the two arrive from different states and mean different
    things: :func:`mark_refused` is a person declining a request that was never
    released, and this one is the system withdrawing a release a person had already
    given. Sharing one function would mean one predicate covering both, and a
    predicate of "pending or approved" would let a refusal land on a request nobody had
    decided yet.

    No instant and no actor, for :func:`mark_refused`'s reason: the ratified column
    list gives the row no refusal counterpart to ``approved_at``/``approved_by_*``, and
    the guard that did this is named on the held operation record this refusal
    terminalises (``confirmation-and-safety.md`` § Execution guards: "the operation
    ``failed`` with the guard's code").
    """
    return _apply(conn, _in_state(approval_id, t.APPROVED), state=t.REFUSED)


def mark_executed(conn: Connection, *, approval_id: UUID, now: datetime) -> bool:
    """Move an ``approved`` approval to ``executed``, stamping ``executed_at``.
    Reports whether it applied.

    Predicated on ``approved`` and written **in the same transaction as the effect**,
    which is what makes "the fixture executes once" a property of the database rather
    than of the order two callers happened to run in: a second execution of the same
    approval finds the row no longer ``approved`` and refuses before the handler.
    """
    return _apply(
        conn, _in_state(approval_id, t.APPROVED), state=t.EXECUTED, executed_at=now
    )


def get(conn: Connection, *, approval_id: UUID) -> ApprovalRow | None:
    """One approval by id, or ``None`` when there is no such row."""
    row = conn.execute(
        select(*_COLUMNS).where(t.approval.c.id == approval_id)
    ).one_or_none()
    return None if row is None else _row(row)


def payload_body(conn: Connection, *, approval_id: UUID) -> dict[str, object] | None:
    """The stored snapshot of the operation input, or ``None`` when there is none.

    ``None`` means the snapshot has been removed by the deletion cascade, never that
    the approval had no payload: :func:`create` writes both rows together.
    """
    row = conn.execute(
        select(t.approval_payload.c.body).where(
            t.approval_payload.c.approval_id == approval_id
        )
    ).one_or_none()
    if row is None:
        return None
    body = row.body
    return dict(body) if isinstance(body, Mapping) else {}
