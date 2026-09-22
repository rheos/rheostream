"""The bounded tool-telemetry table.

Revision ID: 0008_tool_telemetry
Revises: 0007_record_deletion

Creates exactly ``core.tool_telemetry`` from ``rheo_core.audit.telemetry_tables``'s
own ``MetaData``, so frozen revisions 0001-0007 still create exactly their own sets.
Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.audit.telemetry_tables import telemetry_metadata

revision: str = "0008_tool_telemetry"
down_revision: str | None = "0007_record_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    telemetry_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
