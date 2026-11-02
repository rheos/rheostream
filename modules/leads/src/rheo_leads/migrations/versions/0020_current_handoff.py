"""Frozen: a handoff routes to Current through core's approved external action.

The handoff gains its attempt count and the minimal projection of Current's lasting
source link (live and last project, project state, link revision). Its snapshot
records the qualification a request was made against and whether that
qualification's evidence was stale. Two repair scans keep one cursor each.

Numbered 0020 and based on 0018: 0019 is held by separate, unmerged work.
"""

from alembic import op

revision = "0020_current_handoff"
down_revision = "0018_invited_jobs"
branch_labels = None
depends_on = None
# Adds columns with constant defaults, checks every existing row satisfies, two
# partial indexes and one empty table; no row changes.
non_destructive_upgrade = True

STATES = (
    "'unavailable','pending','succeeded','failed','refused','cancelled','unresolved'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE leads.handoff "
        "ADD COLUMN attempt_count INTEGER DEFAULT 0 NOT NULL, "
        "ADD COLUMN current_project_ref TEXT, "
        "ADD COLUMN last_current_project_ref TEXT, "
        "ADD COLUMN current_project_state TEXT DEFAULT 'never_assigned' NOT NULL, "
        "ADD COLUMN current_link_revision INTEGER, "
        f"ADD CONSTRAINT handoff_state_known CHECK (state IN ({STATES})), "
        "ADD CONSTRAINT handoff_attempts CHECK (attempt_count >= 0 AND "
        "(attempt_count = 0) = (external_action_id IS NULL)), "
        "ADD CONSTRAINT handoff_pending_staged CHECK "
        "(state <> 'pending' OR external_action_id IS NOT NULL), "
        "ADD CONSTRAINT handoff_success_result CHECK "
        "(state <> 'succeeded' OR result_ref IS NOT NULL), "
        "ADD CONSTRAINT handoff_project_state CHECK (current_project_state IN "
        "('never_assigned','linked','cleared','deleted')), "
        "ADD CONSTRAINT handoff_project_live CHECK "
        "((current_project_state = 'linked') = (current_project_ref IS NOT NULL)), "
        "ADD CONSTRAINT handoff_project_history CHECK "
        "(current_project_state NOT IN ('cleared','deleted') "
        "OR last_current_project_ref IS NOT NULL), "
        "ADD CONSTRAINT handoff_link_revision CHECK "
        "(current_link_revision IS NULL OR current_link_revision >= 1), "
        "ADD CONSTRAINT handoff_link_projection CHECK "
        "(current_link_revision IS NOT NULL OR (current_project_state = "
        "'never_assigned' AND last_current_project_ref IS NULL))"
    )
    # Pending handoffs, and unresolved ones whose core action still exists: a
    # person's later resolution of that action reaches Leads through this scan.
    op.execute(
        "CREATE INDEX handoff_nonterminal_scan ON leads.handoff "
        "(requested_at, id) WHERE state = 'pending' OR (state = 'unresolved' "
        "AND external_action_id IS NOT NULL AND COALESCE(result_detail, '') "
        "NOT IN ('restored_unresolved', 'external_action_missing'))"
    )
    op.execute(
        "CREATE INDEX handoff_succeeded_scan ON leads.handoff "
        "(requested_at, id) WHERE state = 'succeeded'"
    )
    op.execute(
        "ALTER TABLE leads.handoff_snapshot "
        "ADD COLUMN qualification_ref TEXT, "
        "ADD COLUMN qualification_stale BOOLEAN, "
        "ADD CONSTRAINT handoff_snapshot_qualification CHECK "
        "((qualification_ref IS NULL) = (qualification_stale IS NULL))"
    )
    op.execute(
        """CREATE TABLE leads.current_link_cursor (
    scan TEXT NOT NULL,
    after_requested_at TIMESTAMP WITH TIME ZONE,
    after_id UUID,
    cycles INTEGER DEFAULT 0 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (scan),
    CONSTRAINT current_link_cursor_scan CHECK (scan IN ('nonterminal','succeeded')),
    CONSTRAINT current_link_cursor_key CHECK
        ((after_requested_at IS NULL) = (after_id IS NULL)),
    CONSTRAINT current_link_cursor_cycles CHECK (cycles >= 0)
)"""
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this migration")
