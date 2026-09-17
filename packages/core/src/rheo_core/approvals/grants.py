"""Every read and write against ``core.standing_grant`` and
``core.standing_grant_operation``: the mint, the one transition, and the two reads.

Shaped after ``approvals/records.py`` in every respect that matters: each function
takes the caller's ``Connection`` and runs inside the caller's transaction, **none of
the writes below commits**, and ``now`` is always an explicit keyword so nothing here
reads the process clock. A grant row and its operation rows are written together by
one call, because a grant that named nothing would cover nothing and the two writes
are one fact.

**The one transition is predicated on the state it may leave.** :func:`revoke` matches
only a grant that is not already revoked and reports whether it applied, which is
``approvals/records.py``'s rule for the same reason: a conditional write that reports
whether it matched leaves the meaning of a zero rowcount to the call site. Here it
means the grant was revoked already, and the call site turns that into a refusal
rather than moving ``revoked_at`` a second time.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Row, insert, select, update

from rheo_core.approvals import grant_tables as t
from rheo_core.refs import uuid7


@dataclass(frozen=True, slots=True)
class GrantRow:
    """One ``core.standing_grant`` row, with the operation names it covers.

    The names travel with the row because a grant is never useful without them and
    both reads below need them; carrying them here means no caller has to remember to
    make the second query.
    """

    id: UUID
    actor_kind: str
    actor_id: UUID
    granted_by_id: UUID | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    operation_names: tuple[str, ...]

    def covers(self, now: datetime) -> tuple[str, ...]:
        """The operations this grant covers at ``now``: all of them, or none.

        **This is the definition of "covered", and it lives here** rather than in a
        consultation path, because there is no consultation path in release one: the
        dispatcher never consults grants for the three upper classes by design, and no
        lower-class call in this phase requires a confirmation a grant could satisfy.
        The published record of both grant operations reads it, so "no longer covered"
        is answerable through a supported operation rather than by reading a timestamp
        and inferring what it means.

        Revoked or expired covers nothing; the grant's own row is what a reader gets
        either way, so a revocation is visible rather than silent.
        """
        if self.revoked_at is not None:
            return ()
        if self.expires_at is not None and self.expires_at <= now:
            return ()
        return self.operation_names


_COLUMNS: Final = (
    t.standing_grant.c.id,
    t.standing_grant.c.actor_kind,
    t.standing_grant.c.actor_id,
    t.standing_grant.c.granted_by_id,
    t.standing_grant.c.created_at,
    t.standing_grant.c.expires_at,
    t.standing_grant.c.revoked_at,
)
"""The columns every read selects; ``approvals/records.py``'s own reasoning for the
same constant."""


def _row(row: Row[Any], operation_names: tuple[str, ...]) -> GrantRow:
    """One selected row as a :class:`GrantRow`, field for field — written out rather
    than splatted, for the reason ``approvals/records.py``'s ``_row`` gives."""
    return GrantRow(
        id=row.id,
        actor_kind=str(row.actor_kind),
        actor_id=row.actor_id,
        granted_by_id=row.granted_by_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        operation_names=operation_names,
    )


def operation_names(conn: Connection, *, grant_id: UUID) -> tuple[str, ...]:
    """The operations one grant covers, in name order.

    Ordered by name rather than by insertion, so two reads of the same grant answer
    the same sequence and a test can compare against a sorted literal.
    """
    rows = conn.execute(
        select(t.standing_grant_operation.c.operation_name)
        .where(t.standing_grant_operation.c.grant_id == grant_id)
        .order_by(t.standing_grant_operation.c.operation_name)
    ).scalars()
    return tuple(str(name) for name in rows)


def create(
    conn: Connection,
    *,
    actor_kind: str,
    actor_id: UUID,
    granted_by_id: UUID | None,
    names: Iterable[str],
    created_at: datetime,
    expires_at: datetime | None,
) -> UUID:
    """Write one grant and its operation rows; return its id. Does not commit.

    ``names`` is de-duplicated and sorted here rather than at the call site: the
    operation table's primary key is ``(grant_id, operation_name)``, so a list naming
    the same operation twice would otherwise reach the database as a unique violation
    — a driver error for an input the caller cannot be expected to have normalised.
    The stored set is what the grant covers, and a repeat adds nothing to it.
    """
    grant_id = uuid7()
    conn.execute(
        insert(t.standing_grant).values(
            id=grant_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            granted_by_id=granted_by_id,
            created_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )
    )
    unique: Sequence[str] = sorted(set(names))
    if unique:
        conn.execute(
            insert(t.standing_grant_operation),
            [{"grant_id": grant_id, "operation_name": name} for name in unique],
        )
    return grant_id


def revoke(conn: Connection, *, grant_id: UUID, now: datetime) -> bool:
    """Stamp ``revoked_at`` on a grant that is not revoked yet. Reports whether it
    applied.

    The operation rows are left alone. A revoked grant is a grant that covered those
    operations until ``revoked_at`` and covers nothing after it, and deleting the rows
    would turn the record of what was granted into a record that nothing was.
    """
    return (
        conn.execute(
            update(t.standing_grant)
            .where(
                (t.standing_grant.c.id == grant_id)
                & t.standing_grant.c.revoked_at.is_(None)
            )
            .values(revoked_at=now)
        ).rowcount
        == 1
    )


def get(conn: Connection, *, grant_id: UUID) -> GrantRow | None:
    """One grant by id with its operation names, or ``None`` when there is no such
    row."""
    row = conn.execute(
        select(*_COLUMNS).where(t.standing_grant.c.id == grant_id)
    ).one_or_none()
    if row is None:
        return None
    return _row(row, operation_names(conn, grant_id=grant_id))
