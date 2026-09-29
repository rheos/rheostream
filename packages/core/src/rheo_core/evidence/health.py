"""Settlement counts for operator checks: how many evidence units left ``pending`` in
a window, and how many of those settled ``gap/acceptance_failed`` (#221).

``rheo doctor`` reads this per workspace. A drain that contains an acceptance fault
defers the unit through ``defer_unit`` and succeeds, so the fifth attempt's
``gap/acceptance_failed`` is otherwise silent: the job reports success and the
evidence is gone. These counts are what makes it visible.

Not on the package's public surface, and never imported by a module: an operator
read over one workspace database's own connection.

**Content-free.** Two counts and nothing else: no body, native key or row id.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, func, select, text

from rheo_core.storage.evidence_tables import (
    OUTCOME_ACCEPTANCE_FAILED,
    STATE_GAP,
    evidence_unit,
)


@dataclass(frozen=True, slots=True)
class SettlementCounts:
    """Units that left ``pending`` since the window opened, and how many of them
    settled ``gap/acceptance_failed``."""

    settled: int
    acceptance_failed: int


def settlement_counts_since(
    connection: Connection, *, since: datetime
) -> SettlementCounts | None:
    """Counts over rows with ``settled_at >= since``; ``None`` when this database has
    no ``core.evidence_unit`` (a workspace not yet migrated to revision 0010).

    ``settled_at`` is stamped on every row that leaves ``pending``, ``gap`` included,
    so one column bounds both counts. The table has no index on it; it is transient
    working state the retention sweep keeps small, so the scan is cheap.
    """
    present = connection.execute(
        text("SELECT to_regclass('core.evidence_unit')")
    ).scalar_one()
    if present is None:
        return None
    failed = and_(
        evidence_unit.c.state == STATE_GAP,
        evidence_unit.c.outcome == OUTCOME_ACCEPTANCE_FAILED,
    )
    row = connection.execute(
        select(
            func.count().label("settled"),
            func.count().filter(failed).label("acceptance_failed"),
        ).where(evidence_unit.c.settled_at >= since)
    ).one()
    return SettlementCounts(
        settled=row.settled, acceptance_failed=row.acceptance_failed
    )
