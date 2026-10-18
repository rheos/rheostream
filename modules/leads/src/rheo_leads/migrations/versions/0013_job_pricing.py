"""Frozen: proposal pricing: each job's pricing state and its milestone rows."""

from alembic import op

revision = "0013_job_pricing"
down_revision = "0012_job_proposals"
branch_labels = None
depends_on = None
# Creates two empty owned tables; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.job_pricing (
        opportunity_id UUID NOT NULL,
        mode TEXT,
        state TEXT NOT NULL,
        attempts INTEGER DEFAULT 0 NOT NULL,
        retry_after TIMESTAMP WITH TIME ZONE,
        failure_kind TEXT,
        warning TEXT,
        too_vague TEXT,
        generated_at TIMESTAMP WITH TIME ZONE,
        requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (opportunity_id),
        FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
        CONSTRAINT job_pricing_state_known
            CHECK (state IN ('idle','queued','running','ready','failed')),
        CONSTRAINT job_pricing_mode_known
            CHECK (mode IS NULL OR mode IN ('hourly','milestones')),
        CONSTRAINT job_pricing_attempts CHECK (attempts >= 0)
    )""")
    op.execute("""CREATE TABLE leads.proposal_milestone (
        id UUID NOT NULL,
        opportunity_id UUID NOT NULL,
        position INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        amount NUMERIC(12, 2) NOT NULL,
        due_on DATE,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(opportunity_id)
            REFERENCES leads.job_pricing (opportunity_id) ON DELETE CASCADE,
        CONSTRAINT proposal_milestone_position UNIQUE (opportunity_id, position),
        CONSTRAINT proposal_milestone_amount CHECK (amount >= 0)
    )""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
