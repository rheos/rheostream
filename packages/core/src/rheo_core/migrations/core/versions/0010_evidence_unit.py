"""The evidence table automatic memory records turns into.

Revision ID: 0010_evidence_unit
Revises: 0009_runtime_session_policy

Creates exactly ``core.evidence_unit`` from ``rheo_core.storage.evidence_tables``'s own
``MetaData``, with its CHECK-constrained vocabularies and its two indexes (the unique
unit identity and the partial pending-claim index), so frozen revisions 0001-0009 still
create exactly their own sets. Downgrade drops the table, which takes its constraints
and indexes with it; the table holds only transient pending turns, so nothing durable
is lost.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.evidence_tables import evidence_metadata

revision: str = "0010_evidence_unit"
down_revision: str | None = "0009_runtime_session_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    evidence_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    evidence_metadata.drop_all(op.get_bind(), checkfirst=False)
