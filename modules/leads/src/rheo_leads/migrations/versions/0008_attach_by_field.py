"""Frozen: routing can attach a repeat observation by a matching field value."""

from alembic import op

revision = "0008_attach_by_field"
down_revision = "0007_jobsearch_preset"
branch_labels = None
depends_on = None
# Adds a nullable column and widens one CHECK; existing rules keep their meaning.
non_destructive_upgrade = True


def upgrade() -> None:
    op.execute("ALTER TABLE leads.routing_rule ADD COLUMN match_field TEXT")
    # The 0004 action CHECK is unnamed; find it by its definition, not a guessed name.
    op.execute("""DO $$
DECLARE name TEXT;
BEGIN
    SELECT conname INTO STRICT name FROM pg_constraint
    WHERE conrelid = 'leads.routing_rule'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%attach_to_open_opportunity%';
    EXECUTE format('ALTER TABLE leads.routing_rule DROP CONSTRAINT %I', name);
END $$""")
    op.execute("""ALTER TABLE leads.routing_rule
    ADD CONSTRAINT routing_rule_action_known CHECK (action IN (
        'record_only', 'create_opportunity',
        'attach_to_open_opportunity', 'attach_by_field'))""")
    op.execute("""ALTER TABLE leads.routing_rule
    ADD CONSTRAINT routing_rule_match_field
    CHECK ((action = 'attach_by_field') = (match_field IS NOT NULL))""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
