"""Frozen next-follow-up table; one pending reminder per opportunity."""

from alembic import op

revision = "0005_followup_reminders"
down_revision = "0004_opportunities"
branch_labels = None
depends_on = None
# Adds an empty owned table and indexes; preserves every existing record.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("""CREATE TABLE leads.followup_reminder (
        id UUID PRIMARY KEY,
        opportunity_id UUID NOT NULL REFERENCES leads.opportunity(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        due_on DATE NOT NULL,
        state TEXT NOT NULL
            CHECK (state IN ('pending','completed','cancelled','superseded')),
        created_by_kind TEXT NOT NULL,
        created_by_id UUID,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        resolved_by_kind TEXT,
        resolved_by_id UUID,
        resolved_at TIMESTAMP WITH TIME ZONE,
        CHECK ((state = 'pending') = (resolved_at IS NULL))
    )""")
    op.execute("""CREATE UNIQUE INDEX followup_one_pending
        ON leads.followup_reminder (opportunity_id) WHERE state = 'pending'""")
    op.execute("""CREATE INDEX followup_due
        ON leads.followup_reminder (state, due_on)""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
