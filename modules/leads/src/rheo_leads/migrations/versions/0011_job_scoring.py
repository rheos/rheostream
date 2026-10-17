"""Frozen: job scoring's score column, profile and per-job scoring state."""

from alembic import op

revision = "0011_job_scoring"
down_revision = "0010_job_paste"
branch_labels = None
depends_on = None
# Adds one nullable column and creates two empty owned tables; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""ALTER TABLE leads.qualification
    ADD COLUMN score INTEGER,
    ADD CONSTRAINT qualification_score CHECK (score BETWEEN 1 AND 100)""")
    op.execute("""CREATE TABLE leads.jobsearch_profile (
        singleton BOOLEAN DEFAULT true NOT NULL,
        profile TEXT NOT NULL,
        policy JSONB NOT NULL,
        version INTEGER NOT NULL,
        scoring_account_id UUID NOT NULL,
        updated_by_kind TEXT NOT NULL,
        updated_by_id UUID,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (singleton),
        CONSTRAINT jobsearch_profile_singleton CHECK (singleton),
        CONSTRAINT jobsearch_profile_version CHECK (version > 0)
    )""")
    op.execute("""CREATE TABLE leads.job_score_state (
        opportunity_id UUID NOT NULL,
        input_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER DEFAULT 0 NOT NULL,
        retry_after TIMESTAMP WITH TIME ZONE,
        failure_kind TEXT,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (opportunity_id),
        FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
        CONSTRAINT job_score_state_known
            CHECK (state IN ('pending','scored','failed')),
        CONSTRAINT job_score_state_attempts CHECK (attempts >= 0)
    )""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
