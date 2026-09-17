"""The approval record and its payload snapshot.

Revision ID: 0003_approvals
Revises: 0002_durable_work

Creates exactly ``approval`` and ``approval_payload`` from the ``Table`` objects in
``rheo_core.approvals.tables``, which carry their own ``MetaData`` so that revisions
0001 and 0002 still create exactly their own six and seven. Schema only: no behaviour
reads or writes these tables in this revision's run. Downgrade is not supported in
release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.approvals.tables import approval_metadata

revision: str = "0003_approvals"
down_revision: str | None = "0002_durable_work"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    approval_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
