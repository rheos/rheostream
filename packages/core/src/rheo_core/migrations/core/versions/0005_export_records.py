"""Export and restore records.

Revision ID: 0005_export_records
Revises: 0004_standing_grants
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.exports.tables import export_metadata

revision: str = "0005_export_records"
down_revision: str | None = "0004_standing_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    export_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
