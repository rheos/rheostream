"""Frozen: record what evidence each qualification assessment judged."""

from alembic import op

revision = "0006_assessment_evidence_digest"
down_revision = "0005_followup_reminders"
branch_labels = None
depends_on = None
# Adds one nullable column; preserves every existing record. Assessments made before
# this revision keep a null digest and fall back to the revision comparison.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("ALTER TABLE leads.qualification ADD COLUMN evidence_digest TEXT")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
