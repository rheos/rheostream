"""Frozen: the job feed's run history: one content-free row per collector run,
with the reason codes of its failed detail reads."""

from alembic import op

revision = "0016_jobfeed_runs"
down_revision = "0015_model_calls"
branch_labels = None
depends_on = None
# Creates one empty owned table and its index; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.jobfeed_run (
        id UUID NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE NOT NULL,
        outcome TEXT NOT NULL,
        stop_code TEXT,
        fetched INTEGER DEFAULT '0' NOT NULL,
        imported INTEGER DEFAULT '0' NOT NULL,
        skipped_dup INTEGER DEFAULT '0' NOT NULL,
        refreshed INTEGER DEFAULT '0' NOT NULL,
        filtered INTEGER DEFAULT '0' NOT NULL,
        skipped_untracked INTEGER DEFAULT '0' NOT NULL,
        errors INTEGER DEFAULT '0' NOT NULL,
        detail_reads INTEGER DEFAULT '0' NOT NULL,
        pages INTEGER DEFAULT '0' NOT NULL,
        read_failures JSONB DEFAULT '{}' NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT jobfeed_run_outcome
            CHECK (outcome IN ('completed','partial','stopped'))
    )""")
    op.execute("CREATE INDEX jobfeed_run_finished ON leads.jobfeed_run (finished_at)")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
