"""Frozen: the proposal profile, per-job proposals, sections and answer library."""

from alembic import op

revision = "0012_job_proposals"
down_revision = "0011_job_scoring"
branch_labels = None
depends_on = None
# Creates four empty owned tables; no row changes.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.proposal_profile (
        singleton BOOLEAN DEFAULT true NOT NULL,
        profile TEXT NOT NULL,
        policy JSONB NOT NULL,
        version INTEGER NOT NULL,
        drafting_account_id UUID NOT NULL,
        updated_by_kind TEXT NOT NULL,
        updated_by_id UUID,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (singleton),
        CONSTRAINT proposal_profile_singleton CHECK (singleton),
        CONSTRAINT proposal_profile_version CHECK (version > 0)
    )""")
    op.execute("""CREATE TABLE leads.job_proposal (
        opportunity_id UUID NOT NULL,
        state TEXT NOT NULL,
        questions TEXT,
        stage TEXT,
        step INTEGER,
        total INTEGER,
        findings JSONB,
        generated_at TIMESTAMP WITH TIME ZONE,
        attempts INTEGER DEFAULT 0 NOT NULL,
        retry_after TIMESTAMP WITH TIME ZONE,
        failure_kind TEXT,
        profile_version INTEGER,
        model_id TEXT,
        prompt_version TEXT,
        requested_by_kind TEXT NOT NULL,
        requested_by_id UUID,
        requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (opportunity_id),
        FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
        CONSTRAINT job_proposal_state_known
            CHECK (state IN ('idle','queued','drafting','ready','failed')),
        CONSTRAINT job_proposal_attempts CHECK (attempts >= 0)
    )""")
    op.execute("""CREATE TABLE leads.answer_library (
        id UUID NOT NULL,
        question TEXT NOT NULL,
        question_key TEXT NOT NULL,
        answer TEXT NOT NULL,
        updated_by_kind TEXT NOT NULL,
        updated_by_id UUID,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (question_key)
    )""")
    op.execute("""CREATE TABLE leads.proposal_section (
        id UUID NOT NULL,
        opportunity_id UUID NOT NULL,
        position INTEGER NOT NULL,
        kind TEXT NOT NULL,
        question TEXT,
        body TEXT NOT NULL,
        library_answer_id UUID,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(opportunity_id)
            REFERENCES leads.job_proposal (opportunity_id) ON DELETE CASCADE,
        FOREIGN KEY(library_answer_id)
            REFERENCES leads.answer_library (id) ON DELETE SET NULL,
        CONSTRAINT proposal_section_position UNIQUE (opportunity_id, position),
        CONSTRAINT proposal_section_kind_known
            CHECK (kind IN ('cover_letter','answer')),
        CONSTRAINT proposal_section_letter_first
            CHECK ((kind = 'cover_letter') = (position = 0)),
        CONSTRAINT proposal_section_question
            CHECK ((kind = 'answer') = (question IS NOT NULL))
    )""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
