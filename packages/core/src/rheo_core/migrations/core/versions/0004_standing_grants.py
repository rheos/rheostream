"""The standing grant and the operations it covers.

Revision ID: 0004_standing_grants
Revises: 0003_approvals

Creates exactly ``standing_grant`` and ``standing_grant_operation`` from the ``Table``
objects in ``rheo_core.approvals.grant_tables``, which carry their own ``MetaData`` so
that revisions 0001, 0002 and 0003 still create exactly their own six, seven and two.
``standing_grant.expires_at`` is required because every grant records a bounded
period. Schema only: the behaviour over these tables is ``rheo_core.approvals.grants``
and ``rheo_core.approvals.grant_operations``. Downgrade is not supported in release
one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.approvals.grant_tables import grant_metadata

revision: str = "0004_standing_grants"
down_revision: str | None = "0003_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    grant_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
