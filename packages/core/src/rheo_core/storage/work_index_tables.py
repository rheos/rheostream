"""The control-plane due-work index, as one SQLAlchemy Core ``Table`` object in schema
``control``.

**Why a second ``MetaData`` and not an eleventh table in ``control_tables.py``.**
``0001_control_plane`` calls ``control_metadata.create_all(checkfirst=False)``, and both
that revision and ``control_tables.py`` state it is never edited after run 0b1.
Attaching this table to ``control_metadata`` would make revision 0001 create eleven
tables on a fresh database and edit a frozen revision by side effect. One ``MetaData``
per revision keeps every ``create_all`` exact: ``0002_work_index`` creates exactly the
one table below. This mirrors ``work_tables.py``'s own split of the core chain.

**Absent row means due now.** There is no lifecycle to maintain and no backfill: a
workspace with no row here is discovered on the first pass because
``work_index.workspaces_with_due_work`` reads a ``LEFT JOIN`` from ``control.workspace``
rather than scanning this table. ``ON DELETE CASCADE`` is the whole of the removal
half — deleting a workspace takes its index row with it.

**A hint, not a lease.** The only guarantee this table offers is a bounded upper delay.
Two readers may observe the same ``due_at`` and both visit the workspace; exclusion
lives in the visitor's own leasing and in the workspace's own tables, never here. See
``work_index.py`` for the compare-and-set that keeps a concurrent mark from being lost.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, MetaData, Table
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.control_tables import CONTROL_SCHEMA, workspace

work_index_metadata = MetaData(schema=CONTROL_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


# The index on ``due_at`` serves the only read there is: the earliest-due window, in
# ``due_at`` order. ``updated_at`` carries no index because nothing queries by it; it
# exists so an operator can tell a stale row from a fresh one.
workspace_work_due = Table(
    "workspace_work_due",
    work_index_metadata,
    Column(
        "workspace_id",
        UUID(as_uuid=True),
        ForeignKey(workspace.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("due_at", _timestamptz(), nullable=False),
    Column("updated_at", _timestamptz(), nullable=False),
    Index("workspace_work_due_due_at", "due_at"),
)
