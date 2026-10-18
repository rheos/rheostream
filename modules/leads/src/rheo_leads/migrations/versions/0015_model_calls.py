"""Frozen: the model call log: one content-free row per model call Leads makes,
and an index for reading a job's current assessment."""

from alembic import op

revision = "0015_model_calls"
down_revision = "0014_job_promotion"
branch_labels = None
depends_on = None
# Creates one empty owned table and one index; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.model_call (
        id UUID NOT NULL,
        purpose TEXT NOT NULL,
        opportunity_id UUID,
        model_id TEXT NOT NULL,
        started_at TIMESTAMP WITH TIME ZONE NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE,
        input_tokens INTEGER,
        output_tokens INTEGER,
        outcome TEXT NOT NULL,
        failure_kind TEXT,
        PRIMARY KEY (id),
        CONSTRAINT model_call_outcome_known
            CHECK (outcome IN ('running','answered','deferred','failed')),
        CONSTRAINT model_call_finished_when_done
            CHECK ((outcome = 'running') = (finished_at IS NULL)),
        CONSTRAINT model_call_input_tokens
            CHECK (input_tokens IS NULL OR input_tokens >= 0),
        CONSTRAINT model_call_output_tokens
            CHECK (output_tokens IS NULL OR output_tokens >= 0),
        FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE SET NULL
    )""")
    op.execute("CREATE INDEX model_call_started ON leads.model_call (started_at)")
    op.execute(
        "CREATE INDEX model_call_opportunity ON leads.model_call (opportunity_id)"
    )
    op.execute(
        "CREATE INDEX qualification_current ON leads.qualification "
        "(opportunity_id, input_revision DESC, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
