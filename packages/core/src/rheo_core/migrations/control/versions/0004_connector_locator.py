"""Add the connection-to-workspace locator; no credentials or domain records."""

from alembic import op
from rheo_core.connectors.tables import metadata

revision = "0004_connector_locator"
down_revision = "0003_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
