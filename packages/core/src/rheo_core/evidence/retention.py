"""The daily sweep's half of evidence retention: age what nobody drained, then delete.

Called by ``core.retention_sweep`` (``rheo_core.work.schedules.run_retention_sweep``)
on every workspace, before that handler's CLI-config-directory early return. It is
the path that clears text when nothing ever drains a workspace: the provider was
removed, or the subscribing module was disabled after recording.

**Imports only the evidence table and SQLAlchemy.** ``rheo_core.work`` calls this,
so it must never import ``rheo_core.work``; the owning package holds it the way
``rheo_core.audit.tool_telemetry`` holds ``purge_expired_tool_telemetry``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Connection, delete, select, update

from rheo_core.storage.evidence_tables import (
    OUTCOME_EXPIRED_PENDING,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)

_SETTLED_STATES: Final = (STATE_SETTLED, STATE_GAP)


@dataclass(frozen=True, slots=True)
class EvidencePurge:
    """How many rows one call aged into gaps, and how many it deleted."""

    aged: int
    deleted: int


def purge_evidence(
    conn: Connection, *, now: datetime, settled_before: datetime
) -> EvidencePurge:
    """Age expired ``pending`` rows to ``gap/expired_pending``; delete old settled rows.

    1. Every ``pending`` row whose ``source_expires_at <= now`` becomes
       ``gap/expired_pending`` with its body cleared and ``settled_at = now``. The rows
       are chosen by a ``FOR UPDATE SKIP LOCKED`` subquery: a row another transaction
       holds is skipped, never waited on, and the next sweep ages it if that
       transaction rolls back. A later drain job will hold row locks across a model
       call and age rows with the same subquery when it claims them.
    2. Every ``settled`` or ``gap`` row whose ``settled_at`` is older than
       ``settled_before`` is deleted. The caller passes
       ``now - runtime.transcript_retention_days``.

    A row aged in step 1 carries ``settled_at = now``, so step 2 never deletes it in
    the same call. Does not commit; the sweep's own unit of work owns that.
    """
    expired = (
        select(evidence_unit.c.id)
        .where(
            evidence_unit.c.state == STATE_PENDING,
            evidence_unit.c.source_expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    aged = conn.execute(
        update(evidence_unit)
        .where(evidence_unit.c.id.in_(expired))
        .values(
            state=STATE_GAP,
            outcome=OUTCOME_EXPIRED_PENDING,
            body=None,
            settled_at=now,
        )
    ).rowcount
    deleted = conn.execute(
        delete(evidence_unit).where(
            evidence_unit.c.state.in_(_SETTLED_STATES),
            evidence_unit.c.settled_at < settled_before,
        )
    ).rowcount
    return EvidencePurge(aged=int(aged), deleted=int(deleted))
