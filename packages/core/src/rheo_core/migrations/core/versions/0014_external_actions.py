"""Retained keyed outcomes and durable external-action state.

Revision ID: 0014_external_actions
Revises: 0013_outbox_retention

This is the complete schema boundary for keyed replay and external continuation. It
creates both tables from one dedicated metadata object and changes no earlier table.
Later behavior must append a new migration rather than amend this revision.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.operations.idempotency_tables import external_action_metadata

revision: str = "0014_external_actions"
down_revision: str | None = "0013_outbox_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    external_action_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    external_action_metadata.drop_all(op.get_bind(), checkfirst=False)
