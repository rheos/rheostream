"""Frozen: a Job search pipeline may have its own manual paste connection."""

from alembic import op

revision = "0010_job_paste"
down_revision = "0009_jobfeed_collector"
branch_labels = None
depends_on = None
# Drops one partial unique index; no row changes. Capture, webhook creation and
# export now name the seeded connection by its ``manual_capture`` namespace.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("DROP INDEX leads.one_manual_connection")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
