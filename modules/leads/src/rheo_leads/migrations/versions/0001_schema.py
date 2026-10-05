"""Create the Leads schema."""

from alembic import op

revision = "0001_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS leads")


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
