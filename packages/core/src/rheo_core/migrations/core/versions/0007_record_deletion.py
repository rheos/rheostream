"""The content-free deletion ledger.

Revision ID: 0007_record_deletion
Revises: 0006_runtime

Creates exactly ``core.deletion_record`` from ``rheo_core.deletion.tables``'s own
``MetaData``, so frozen revisions 0001-0006 still create exactly their own sets.
``export_record.deletion_record_id`` stays the bare, unconstrained column revision
0005 left: the foreign key and its writer are criterion 65's, not this revision's.
Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.deletion.tables import deletion_metadata

revision: str = "0007_record_deletion"
down_revision: str | None = "0006_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    deletion_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
