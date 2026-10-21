"""Frozen: whether a proposal request answers the posting's own questions in separate
fields. Off by default, so a draft is the cover letter alone unless the owner asks."""

from alembic import op

revision = "0017_proposal_posting_questions"
down_revision = "0016_jobfeed_runs"
branch_labels = None
depends_on = None
# Adds one column with a constant default; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute(
        "ALTER TABLE leads.job_proposal "
        "ADD COLUMN posting_questions BOOLEAN DEFAULT false NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
