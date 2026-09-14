"""AC 5 and AC 24: the control-plane due-work index, and the compare-and-set that keeps
a concurrent ``mark_work_due`` from being lost.

Seams: ``workspaces_with_due_work``'s ``LEFT JOIN``/ordering/limit contract, and
``record_visit``'s predicate against every interleaving AC 24 names. Every assertion
reads the database back through a fresh transaction rather than trusting the return
value alone, because the defect this file exists to catch — a visit overwriting a mark
— shows up in the row and not in the call.

**How the interleavings are staged.** Each of AC 24's four cases is one test, and each
runs its read, its mark and its visit in three separate committed transactions. That is
the race the compare-and-set defends: not two writers blocking on a row lock, but a mark
that *commits* in the window between a reader observing ``due_at`` and that reader
writing back. Holding two transactions open would test Postgres's row locking instead.

**Why the fixture parks the workspaces it did not create.** The control database is
session-scoped and earlier files leave real active workspaces behind, every one of which
is due (absent row means due now). A test asserting on an exact result set therefore has
to make its own workspaces the only due ones. The fixture writes a far-future row for
each pre-existing active workspace with a plain upsert, deliberately *not* through
``mark_work_due`` — that function only ever lowers ``due_at``, which is the property
under test and not a tool to set one up with.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables, work_index_tables
from rheo_core.storage.control_plane import (
    insert_workspace_if_absent,
    list_workspaces,
    set_workspace_state,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.work_index import (
    DueWorkspace,
    mark_work_due,
    record_visit,
    workspaces_with_due_work,
)
from sqlalchemy import Connection, Engine, delete, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

pytestmark = pytest.mark.postgres

RECONCILE = 900
# Far enough out that no test's ``now`` reaches it, and readable in a failure message.
PARKED = datetime(2999, 1, 1, tzinfo=UTC)
TIMESTAMPTZ = "timestamp with time zone"


@dataclass
class Registry:
    """Workspace registry rows a test owns, and the connection to read them with.

    Rows are made directly rather than through ``provision``: the index touches only
    the control plane, and provisioning a real database per workspace would make the
    limit case below cost four ``CREATE DATABASE`` calls to assert an ``ORDER BY``.
    """

    engine: Engine
    created: list[UUID]

    def make(
        self,
        *,
        due_at: datetime | None = None,
        state: WorkspaceState = WorkspaceState.ACTIVE,
    ) -> UUID:
        workspace_id = uuid7()
        with self.engine.begin() as conn:
            insert_workspace_if_absent(
                conn,
                workspace_id=workspace_id,
                slug=f"ws-{workspace_id.hex[:12]}",
                display_name="work index fixture",
                database_name=f"rheo_ws_{workspace_id.hex}",
                state_detail="created by tests/postgres/test_work_index.py",
            )
            set_workspace_state(conn, workspace_id, state=state, state_detail=None)
            if due_at is not None:
                _park(conn, workspace_id, due_at)
        self.created.append(workspace_id)
        return workspace_id

    def due_row(self, workspace_id: UUID) -> datetime | None:
        """The ``due_at`` the table currently holds, read in its own transaction."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(work_index_tables.workspace_work_due.c.due_at).where(
                    work_index_tables.workspace_work_due.c.workspace_id == workspace_id
                )
            ).scalar_one_or_none()

    def observe(self, workspace_id: UUID, *, now: datetime) -> DueWorkspace:
        """Read the index the way a visitor would, and pick out one workspace."""
        with self.engine.begin() as conn:
            found = workspaces_with_due_work(conn, now=now, limit=200)
        matching = [d for d in found if d.workspace_id == workspace_id]
        assert matching, f"{workspace_id} is not reported as due: {found}"
        return matching[0]

    def due_ids(self, *, now: datetime, limit: int) -> list[UUID]:
        with self.engine.begin() as conn:
            return [
                d.workspace_id
                for d in workspaces_with_due_work(conn, now=now, limit=limit)
            ]


def _park(conn: Connection, workspace_id: UUID, due_at: datetime) -> None:
    """Set ``due_at`` unconditionally — a fixture's tool, never the contract's."""
    table = work_index_tables.workspace_work_due
    values = {"due_at": due_at, "updated_at": datetime.now(UTC)}
    statement = pg_insert(table).values(workspace_id=workspace_id, **values)
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.workspace_id], set_=values
        )
    )


@pytest.fixture
def registry(cluster: ClusterSession) -> Iterator[Registry]:
    engine = cluster.backend.control_engine
    with engine.begin() as conn:
        for row in list_workspaces(conn):
            if row.state is WorkspaceState.ACTIVE:
                _park(conn, row.id, PARKED)
    owned = Registry(engine=engine, created=[])
    yield owned
    with engine.begin() as conn:
        for workspace_id in owned.created:
            conn.execute(
                delete(control_tables.workspace).where(
                    control_tables.workspace.c.id == workspace_id
                )
            )


# --- AC 5: the table the second control revision creates ------------------------------


def test_the_index_table_exists_in_the_control_schema(cluster: ClusterSession) -> None:
    """AC 5: created by ``0002_work_index``, in schema ``control``.

    Read back through reflection rather than compared against the module's own
    ``Table``, so a column the migration never applied fails here instead of agreeing
    with itself.
    """
    engine = cluster.backend.control_engine
    inspector = inspect(engine)
    assert "workspace_work_due" in inspector.get_table_names(schema="control")
    # ``information_schema`` rather than the reflected SQLAlchemy type: ``TIMESTAMP``'s
    # repr drops the timezone flag, so a column created without one would still read
    # as "TIMESTAMP" and the assertion would pass on the defect it is here to catch.
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'control' AND table_name = 'workspace_work_due'"
            )
        ).all()
    columns = {str(name): (str(kind), yes == "YES") for name, kind, yes in rows}
    assert columns == {
        "workspace_id": ("uuid", False),
        "due_at": (TIMESTAMPTZ, False),
        "updated_at": (TIMESTAMPTZ, False),
    }
    primary = inspector.get_pk_constraint("workspace_work_due", schema="control")
    assert primary["constrained_columns"] == ["workspace_id"]
    indexed = {
        tuple(index["column_names"])
        for index in inspector.get_indexes("workspace_work_due", schema="control")
    }
    assert ("due_at",) in indexed, indexed


# --- discovery ------------------------------------------------------------------------


def test_a_workspace_with_no_index_row_is_due_now(registry: Registry) -> None:
    """Absent row means due now, and the read says so with ``observed_due_at is None``.

    This is the property that makes the index need no backfill and no provisioning
    hook: a workspace nobody has ever visited is discovered on the first pass.
    """
    workspace_id = registry.make()
    assert registry.due_row(workspace_id) is None
    observed = registry.observe(workspace_id, now=datetime.now(UTC))
    assert observed.observed_due_at is None


def test_an_inactive_workspace_is_never_returned(registry: Registry) -> None:
    for state in (
        WorkspaceState.PROVISIONING,
        WorkspaceState.UNAVAILABLE,
        WorkspaceState.RESTORING,
    ):
        workspace_id = registry.make(state=state)
        assert workspace_id not in registry.due_ids(now=datetime.now(UTC), limit=200)


def test_a_workspace_due_later_is_not_returned_yet(registry: Registry) -> None:
    now = datetime.now(UTC)
    workspace_id = registry.make(due_at=now + timedelta(minutes=5))
    assert workspace_id not in registry.due_ids(now=now, limit=200)
    assert workspace_id in registry.due_ids(now=now + timedelta(minutes=6), limit=200)


def test_the_limit_is_honoured_in_due_at_order_with_nulls_first(
    registry: Registry,
) -> None:
    now = datetime.now(UTC)
    oldest = registry.make(due_at=now - timedelta(hours=3))
    middle = registry.make(due_at=now - timedelta(hours=2))
    registry.make(due_at=now - timedelta(hours=1))
    never_visited = registry.make()

    assert registry.due_ids(now=now, limit=2) == [never_visited, oldest]
    assert registry.due_ids(now=now, limit=3) == [never_visited, oldest, middle]
    assert len(registry.due_ids(now=now, limit=200)) == 4


def test_a_limit_below_one_is_refused(registry: Registry) -> None:
    """A caller that wants everything passes a number, not a sentinel."""
    with registry.engine.begin() as conn:
        for limit in (0, -1):
            with pytest.raises(ValueError, match="limit must be positive"):
                workspaces_with_due_work(conn, now=datetime.now(UTC), limit=limit)


def test_deleting_a_workspace_removes_its_index_row(registry: Registry) -> None:
    now = datetime.now(UTC)
    workspace_id = registry.make(due_at=now - timedelta(hours=1))
    assert registry.due_row(workspace_id) is not None
    with registry.engine.begin() as conn:
        conn.execute(
            delete(control_tables.workspace).where(
                control_tables.workspace.c.id == workspace_id
            )
        )
    assert registry.due_row(workspace_id) is None


# --- mark_work_due: monotonic downward ------------------------------------------------


def test_mark_work_due_only_ever_lowers_due_at(registry: Registry) -> None:
    """The monotonicity the whole compare-and-set rests on.

    If a mark could raise ``due_at``, a mark landing between a read and a visit could
    leave the column equal to the observed value and the visit would overwrite it
    without the predicate ever noticing.
    """
    now = datetime.now(UTC)
    workspace_id = registry.make()
    later = now + timedelta(hours=2)
    sooner = now + timedelta(minutes=10)

    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=later)
    assert registry.due_row(workspace_id) == later

    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=sooner)
    assert registry.due_row(workspace_id) == sooner

    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=later)
    assert registry.due_row(workspace_id) == sooner, (
        "a later mark must not push work out"
    )


# --- AC 24: the four interleavings ----------------------------------------------------


def test_ac24_case3_the_uncontended_visit_pushes_due_at_forward(
    registry: Registry,
) -> None:
    """Case 3, the baseline the other three are read against.

    Nothing marks between the read and the visit, so the write applies and the row
    lands on ``LEAST(next_due_at, now + reconcile_seconds)`` — here ``next_due_at``,
    chosen well inside the floor so the ``LEAST`` is the assertion and not the clock.
    """
    now = datetime.now(UTC)
    workspace_id = registry.make(due_at=now - timedelta(hours=1))
    observed = registry.observe(workspace_id, now=now)
    assert observed.observed_due_at == now - timedelta(hours=1)

    next_due_at = now + timedelta(seconds=60)
    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=observed.observed_due_at,
            next_due_at=next_due_at,
            reconcile_seconds=RECONCILE,
        )
    assert applied is True
    assert registry.due_row(workspace_id) == next_due_at


def test_ac24_case3_an_absent_row_is_created_by_the_uncontended_visit(
    registry: Registry,
) -> None:
    """The absent-row branch of the uncontended path: the insert actually happens."""
    now = datetime.now(UTC)
    workspace_id = registry.make()
    observed = registry.observe(workspace_id, now=now)
    assert observed.observed_due_at is None

    next_due_at = now + timedelta(seconds=60)
    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=None,
            next_due_at=next_due_at,
            reconcile_seconds=RECONCILE,
        )
    assert applied is True
    assert registry.due_row(workspace_id) == next_due_at


def test_ac24_case1_a_mark_below_the_observed_value_survives_the_visit(
    registry: Registry,
) -> None:
    """Case 1: the mark commits between the read and the visit, and it wins.

    This is the defect the compare-and-set exists for. An unconditional write here
    would raise ``due_at`` past the mark, and the work just enqueued would go
    undiscovered for up to one reconcile interval with no error and no log line.
    """
    now = datetime.now(UTC)
    workspace_id = registry.make(due_at=now - timedelta(hours=1))
    observed = registry.observe(workspace_id, now=now)

    marked = now - timedelta(minutes=90)
    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=marked)

    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=observed.observed_due_at,
            next_due_at=None,
            reconcile_seconds=RECONCILE,
        )
    assert applied is False, "the visit must not write over a mark it did not observe"
    assert registry.due_row(workspace_id) == marked
    # And the consequence that actually matters: the workspace is still discovered.
    assert workspace_id in registry.due_ids(now=now, limit=200)


def test_ac24_case2_a_mark_into_an_absent_row_survives_the_visit(
    registry: Registry,
) -> None:
    """Case 2: the same interleaving in the absent-row branch.

    The read saw no row; a mark inserted one before the visit. The visit's
    ``INSERT ... ON CONFLICT DO NOTHING`` must leave the mark's row exactly as it is.
    """
    now = datetime.now(UTC)
    workspace_id = registry.make()
    observed = registry.observe(workspace_id, now=now)
    assert observed.observed_due_at is None

    marked = now - timedelta(minutes=5)
    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=marked)

    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=None,
            next_due_at=None,
            reconcile_seconds=RECONCILE,
        )
    assert applied is False
    assert registry.due_row(workspace_id) == marked
    assert workspace_id in registry.due_ids(now=now, limit=200)


def test_ac24_case4_a_mark_at_or_above_the_observed_value_is_discarded_inside_the_floor(
    registry: Registry,
) -> None:
    """Case 4: the one branch where the write applies despite a mark landing.

    ``mark_work_due`` only lowers, so a mark at or above the observed value leaves the
    column exactly where the read found it; the predicate still matches and the visit
    writes. That is correct rather than lossy **because of the floor**: the row lands
    no later than ``now + reconcile_seconds``, so the workspace is rediscovered within
    one reconcile interval and nothing that was due is lost.

    The assertion is the bound, not an exact timestamp. Pinning the value would break
    on any later change to the floor while saying nothing about the guarantee, which
    is the hole this case was added to close.
    """
    now = datetime.now(UTC)
    observed_at = now - timedelta(hours=1)
    workspace_id = registry.make(due_at=observed_at)
    observed = registry.observe(workspace_id, now=now)
    assert observed.observed_due_at == observed_at

    # Above the observed value, so ``LEAST`` keeps the column unchanged.
    marked = observed_at + timedelta(minutes=30)
    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=marked)
    assert registry.due_row(workspace_id) == observed_at, (
        "a mark above the observed value must leave the column alone; "
        "without that the predicate below would not match"
    )

    before = datetime.now(UTC)
    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=observed.observed_due_at,
            next_due_at=None,
            reconcile_seconds=RECONCILE,
        )
    after = datetime.now(UTC)

    assert applied is True
    written = registry.due_row(workspace_id)
    assert written is not None
    # Finite, and inside the floor in both directions: the guarantee, exactly.
    assert written <= after + timedelta(seconds=RECONCILE)
    assert written >= before + timedelta(seconds=RECONCILE)
    # The mark's own value is gone, which is what "discarded" means here.
    assert written > marked


def test_a_visit_that_lost_the_race_leaves_updated_at_alone(
    registry: Registry,
) -> None:
    """A ``False`` return writes nothing at all, not merely nothing to ``due_at``.

    Without this, a visit could no-op on ``due_at`` while still stamping
    ``updated_at``, which would make the row look freshly visited to an operator
    reading the table to find out why work was sitting.
    """
    now = datetime.now(UTC)
    workspace_id = registry.make(due_at=now - timedelta(hours=1))
    observed = registry.observe(workspace_id, now=now)
    with registry.engine.begin() as conn:
        mark_work_due(conn, workspace_id, at=now - timedelta(hours=2))

    table = work_index_tables.workspace_work_due
    with registry.engine.connect() as conn:
        stamped = conn.execute(
            select(table.c.updated_at).where(table.c.workspace_id == workspace_id)
        ).scalar_one()

    with registry.engine.begin() as conn:
        applied = record_visit(
            conn,
            workspace_id,
            observed_due_at=observed.observed_due_at,
            next_due_at=None,
            reconcile_seconds=RECONCILE,
        )
    assert applied is False
    with registry.engine.connect() as conn:
        after = conn.execute(
            select(table.c.updated_at).where(table.c.workspace_id == workspace_id)
        ).scalar_one()
    assert after == stamped


def test_a_non_positive_reconcile_interval_is_refused(registry: Registry) -> None:
    workspace_id = registry.make()
    with registry.engine.begin() as conn:
        with pytest.raises(ValueError, match="reconcile_seconds must be positive"):
            record_visit(
                conn,
                workspace_id,
                observed_due_at=None,
                next_due_at=None,
                reconcile_seconds=0,
            )
