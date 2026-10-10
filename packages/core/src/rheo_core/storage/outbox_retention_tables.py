"""DDL for revision ``0013_outbox_retention``: the outbox sweep's one-row record.

``core.outbox_retention`` holds exactly one row, written by the migration and never
inserted or deleted again. ``swept_through_position`` is the highest
``core.outbox_event.position`` the retention sweep has ever deleted, or null before
the first deletion. It only moves forward.

**Why a record and not ``min(position)``.** The sweep deletes an event only once its
deliveries are all terminal, so an old event with a stuck delivery survives while newer
ones around it go. The smallest position still present then sits below holes, and a
replay gated on it could start inside one (``intake-and-events.md`` § Retention).
Replay gates on this column instead.

**The row is also the lock.** The sweep takes it ``FOR UPDATE`` before it deletes
anything and ``core.work.replay`` reads it ``FOR SHARE`` before its gap check, so the
two serialise on it without either pausing deliveries. That is why the row is created
once by the migration rather than upserted: a replay that found no row would have
nothing to wait on.

Its own ``MetaData``, so ``0013`` creates exactly this table and frozen revisions keep
creating exactly theirs.
"""

from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    SmallInteger,
    Table,
)

from rheo_core.storage.core_tables import CORE_SCHEMA

OUTBOX_RETENTION_ROW_ID: Final = 1
"""The one row's key. The CHECK makes a second row unrepresentable."""

outbox_retention_metadata = MetaData(schema=CORE_SCHEMA)

outbox_retention = Table(
    "outbox_retention",
    outbox_retention_metadata,
    Column("id", SmallInteger, primary_key=True),
    Column("swept_through_position", BigInteger, nullable=True),
    Column("swept_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(f"id = {OUTBOX_RETENTION_ROW_ID}", name="outbox_retention_one_row"),
)
