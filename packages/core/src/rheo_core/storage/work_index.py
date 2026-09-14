"""The due-work index's repository functions: the whole of workspace discovery.

The index answers exactly one question — *which workspaces might have work right now?* —
and answers it without becoming a queue, a lease, or a source of truth.

**A hint, never a lease.** Its only guarantee is a bounded upper delay. Two readers may
observe the same ``due_at``, both visit the same workspace, and both write back;
whether a unit of work actually runs is decided by the workspace's own tables and by
the visitor's own exclusion, never by this table. A later run building a visitor must
therefore build its own exclusion and must not read this table as a substitute for one.

**Absent row means due now.** ``workspaces_with_due_work`` reads a ``LEFT JOIN`` from
``control.workspace``, not a scan of the index, so a never-visited workspace is
returned on the first pass with ``observed_due_at is None``. Nothing backfills, and
``provisioning`` is not involved.

**Why ``record_visit`` is a compare-and-set and not a plain write.** Without a way to
raise ``due_at`` back up after a visit, the index can only ever be lowered: a workspace
touched once stays at ``due_at <= now`` forever and is reopened every pass, which is
the defect the index exists to remove. But an *unconditional* write on the way out is
also a defect. ``mark_work_due`` can commit a lower ``due_at`` while a visit is in
progress; an unconditional write at the end of that visit would overwrite the mark, and
the newly enqueued work would go undiscovered for up to one reconcile interval with no
error and no log line. The fix is to write only while the column still holds the value
the read observed:

* ``mark_work_due`` **only ever lowers** ``due_at`` (``LEAST(existing, excluded)``).
  That monotonicity is what makes the compare-and-set sound: any mark landing between
  the read and the visit necessarily leaves the column different from the observed
  value, so the predicate matches nothing and the mark stands.
* With no row observed, the same argument in insert form: a row that appeared in
  between was put there by a mark, and ``ON CONFLICT DO NOTHING`` keeps it.
* A mark landing *at or above* the observed value leaves the column unchanged, so the
  predicate still matches and the visit's write applies, discarding the mark's value.
  That loses nothing that was due, because the write lands no later than
  ``now + reconcile_seconds`` and the workspace is rediscovered within one reconcile
  interval — which is the whole of the guarantee this index offers.

**Why the reconcile floor exists.** An enqueue writes to a workspace database and the
mark writes to the control database; no transaction spans the two. A crash between them
loses the mark silently. The floor bounds that loss to one reconcile interval.

**``mark_work_due`` is a control-database write on the enqueue path.** The control
engine is created with ``pool_size = storage.pool_max_connections`` and
``max_overflow = 0`` (``storage/postgres.py``, ``PostgresBackend.control_engine``) —
the same connections the connection budget reserves outside the workspace pool cache.
Every enqueue therefore contends for that one pool, so under load the contention is on
the enqueue side, not on the visitor's.

Every function takes the caller's ``Connection`` and runs inside the caller's
transaction; none commits. Timestamps are ``timestamptz``; ``updated_at`` is written
from the process clock in UTC, and ``due_at`` is whatever the caller computed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, func, nulls_first, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_core.storage import control_tables as t
from rheo_core.storage import work_index_tables as i
from rheo_core.storage.control_tables import WorkspaceState


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DueWorkspace:
    """One workspace the index reports as possibly having work.

    ``observed_due_at`` is the ``due_at`` this read saw, or ``None`` when the workspace
    had no index row. It is not decoration: it is the token ``record_visit`` compares
    against, and passing back a value from anywhere else defeats the compare-and-set.
    """

    workspace_id: UUID
    observed_due_at: datetime | None


def workspaces_with_due_work(
    conn: Connection, *, now: datetime, limit: int
) -> tuple[DueWorkspace, ...]:
    """Active workspaces with no index row, or with ``due_at <= now``.

    Ordered by ``due_at`` nulls first, so a never-visited workspace is served before a
    merely overdue one, and capped at ``limit``. Rows that tie on ``due_at`` come back
    in no defined order; that is within the hint's contract, which promises a bounded
    delay and not a schedule.

    ``limit`` is keyword-only with no default and must be positive — a caller that
    wants a large window passes an actual number rather than a sentinel, mirroring
    ``EnginePool.__init__``'s refusal of a non-positive bound.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    due_at = i.workspace_work_due.c.due_at
    statement = (
        select(t.workspace.c.id, due_at)
        .select_from(
            t.workspace.outerjoin(
                i.workspace_work_due,
                i.workspace_work_due.c.workspace_id == t.workspace.c.id,
            )
        )
        .where(t.workspace.c.state == WorkspaceState.ACTIVE.value)
        .where(or_(due_at.is_(None), due_at <= now))
        .order_by(nulls_first(due_at.asc()))
        .limit(limit)
    )
    return tuple(
        DueWorkspace(workspace_id=row.id, observed_due_at=row.due_at)
        for row in conn.execute(statement)
    )


def mark_work_due(conn: Connection, workspace_id: UUID, *, at: datetime) -> None:
    """Bring ``workspace_id``'s ``due_at`` down to ``at``, never up.

    Called after an enqueue commits. ``LEAST(existing, excluded)`` on conflict is what
    makes a late mark unable to push work later, and is the monotonicity
    ``record_visit``'s compare-and-set rests on.

    This run ships no caller: the enqueue belongs to the runs that build the work
    behaviour on top of this schema. A ``workspace_id`` with no registry row violates
    the foreign key and raises ``IntegrityError`` — deliberately untranslated, because
    the only caller that can reach it is one enqueueing into a workspace it just wrote.
    """
    now = _now()
    statement = pg_insert(i.workspace_work_due).values(
        workspace_id=workspace_id, due_at=at, updated_at=now
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[i.workspace_work_due.c.workspace_id],
            set_={
                "due_at": func.least(
                    i.workspace_work_due.c.due_at, statement.excluded.due_at
                ),
                "updated_at": now,
            },
        )
    )


def record_visit(
    conn: Connection,
    workspace_id: UUID,
    *,
    observed_due_at: datetime | None,
    next_due_at: datetime | None,
    reconcile_seconds: int,
) -> bool:
    """Push ``due_at`` forward after a visit, but only if no mark landed meanwhile.

    ``observed_due_at`` is the value ``workspaces_with_due_work`` returned for this
    workspace. With a row observed the write is an ``UPDATE`` predicated on that value;
    with none observed it is an ``INSERT ... ON CONFLICT DO NOTHING``. Either way a
    ``mark_work_due`` that committed since the read survives untouched.

    The value written is ``LEAST(next_due_at, now + reconcile_seconds)``, computed here
    rather than in SQL so that a naive ``next_due_at`` fails loudly instead of being
    coerced against the session's timezone; ``next_due_at is None`` means the caller
    knows of nothing further to do, which is the ``coalesce(..., 'infinity')`` half and
    leaves the reconcile floor as the only term.

    Returns whether the write applied, so a caller can log a lost race rather than
    assume one. **A ``False`` is not a failure** — it means a mark arrived and the
    workspace stays due, which is the outcome the compare-and-set exists to produce.

    This is not a lease. It arbitrates one column in a hint table, not access to the
    workspace: two visitors can both observe the same ``due_at``, both do real work,
    and one write applies while the other no-ops.

    This run ships no caller; the drain that would supply ``next_due_at`` belongs to a
    later run.
    """
    if reconcile_seconds < 1:
        raise ValueError("reconcile_seconds must be positive")
    now = _now()
    floor = now + timedelta(seconds=reconcile_seconds)
    new_due_at = floor if next_due_at is None else min(next_due_at, floor)
    if observed_due_at is None:
        statement = (
            pg_insert(i.workspace_work_due)
            .values(workspace_id=workspace_id, due_at=new_due_at, updated_at=now)
            .on_conflict_do_nothing(
                index_elements=[i.workspace_work_due.c.workspace_id]
            )
            .returning(i.workspace_work_due.c.workspace_id)
        )
        # RETURNING yields a row only when the insert happened; ``rowcount`` is not a
        # reliable signal for an ``ON CONFLICT DO NOTHING`` insert.
        return conn.execute(statement).first() is not None
    result = conn.execute(
        update(i.workspace_work_due)
        .where(i.workspace_work_due.c.workspace_id == workspace_id)
        .where(i.workspace_work_due.c.due_at == observed_due_at)
        .values(due_at=new_due_at, updated_at=now)
    )
    return result.rowcount == 1
