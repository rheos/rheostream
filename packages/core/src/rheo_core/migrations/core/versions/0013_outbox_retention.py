"""The outbox retention sweep's record of the highest position it has deleted.

Revision ID: 0013_outbox_retention
Revises: 0012_evidence_retention_index

Creates exactly ``core.outbox_retention`` from
``rheo_core.storage.outbox_retention_tables``'s own ``MetaData`` and inserts its one
row with a null position: nothing has been swept yet. Additive; no existing table or
row changes.

Downgrade drops the table. Reversible in code; treat it as one-way in production. Once
a sweep has deleted outbox rows, the code before this revision gates replay on the
smallest position still present, which is the unsound check this revision exists to
replace.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.outbox_retention_tables import (
    OUTBOX_RETENTION_ROW_ID,
    outbox_retention,
    outbox_retention_metadata,
)
from sqlalchemy import insert

revision: str = "0013_outbox_retention"
down_revision: str | None = "0012_evidence_retention_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    outbox_retention_metadata.create_all(bind, checkfirst=False)
    bind.execute(
        insert(outbox_retention).values(
            id=OUTBOX_RETENTION_ROW_ID, swept_through_position=None, swept_at=None
        )
    )


def downgrade() -> None:
    outbox_retention_metadata.drop_all(op.get_bind(), checkfirst=False)
