"""Frozen: the review queue's owner override: jobs promoted past the client filters."""

from alembic import op

revision = "0014_job_promotion"
down_revision = "0013_job_pricing"
branch_labels = None
depends_on = None
# Creates one empty owned table; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.job_promotion (
        opportunity_id UUID NOT NULL,
        promoted_by_kind TEXT NOT NULL,
        promoted_by_id UUID,
        promoted_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (opportunity_id),
        FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE
    )""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
