"""The seven durable-work tables of a workspace database.

Revision ID: 0002_durable_work
Revises: 0001_core_schema

Creates exactly ``outbox_event``, ``event_delivery``, ``consumer_processed``, ``job``,
``schedule``, ``operation`` and ``audit_record`` from the ``Table`` objects in
``rheo_core.storage.work_tables``, which carry their own ``MetaData`` so that revision
0001 still creates exactly its own six. Schema only: no behaviour reads or writes these
tables in this revision's run. Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.work_tables import work_metadata

revision: str = "0002_durable_work"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    work_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
