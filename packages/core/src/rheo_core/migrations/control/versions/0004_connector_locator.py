"""Add the connection-to-workspace locator; no credentials or domain records."""

import sqlalchemy as sa
from alembic import op

revision = "0004_connector_locator"
down_revision = "0003_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_locator",
        sa.Column("connection_id", sa.Uuid, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid,
            sa.ForeignKey("control.workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("module_id", sa.Text, nullable=False),
        sa.Column("transport", sa.Text, nullable=False),
        schema="control",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
