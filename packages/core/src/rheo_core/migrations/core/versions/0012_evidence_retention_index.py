"""Index settled evidence units for the daily retention sweep.

Revision ID: 0012_evidence_retention_index
Revises: 0011_local_evidence
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0012_evidence_retention_index"
down_revision: str | None = "0011_local_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "evidence_unit_settled_retention"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "evidence_unit",
        ["settled_at"],
        schema="core",
        postgresql_where=text("state IN ('settled', 'gap')"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="evidence_unit", schema="core")
