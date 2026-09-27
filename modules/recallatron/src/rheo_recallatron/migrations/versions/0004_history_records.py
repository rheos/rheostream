"""Owner-only searchable predecessor history, distinct from curated memory.

Revision ID: 0004_history_records
Revises: 0003_dense_retrieval
"""

from collections.abc import Sequence

from alembic import op
from rheo_recallatron.storage.tables import history_metadata

revision: str = "0004_history_records"
down_revision: str | None = "0003_dense_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    history_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
