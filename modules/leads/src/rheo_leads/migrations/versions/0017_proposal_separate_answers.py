"""Frozen: whether a proposal request answers each screening question in its own field.
Off by default, so the cover letter answers them all unless the owner asks."""

from alembic import op

revision = "0017_proposal_separate_answers"
down_revision = "0016_jobfeed_runs"
branch_labels = None
depends_on = None
# Adds one column with a constant default; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute(
        "ALTER TABLE leads.job_proposal "
        "ADD COLUMN separate_answers BOOLEAN DEFAULT false NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
