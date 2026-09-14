"""The control-plane due-work index.

Revision ID: 0002_work_index
Revises: 0001_control_plane

Creates exactly ``workspace_work_due`` from the ``Table`` object in
``rheo_core.storage.work_index_tables``, which carries its own ``MetaData`` so that
revision 0001 still creates exactly its own ten. Schema only: this run ships no caller
for the index's writes. Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.work_index_tables import work_index_metadata

revision: str = "0002_work_index"
down_revision: str | None = "0001_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    work_index_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
