"""Frozen: a passed or lost job can be reopened.

The Job search preset had no move out of its closed stages, so a job dismissed by
mistake stayed closed. A preset version is immutable once used, so the moves arrive
in a new version: a full copy of the preset's current version (stages, transitions,
requirements, fields, rubric, template) plus a move from each closed stage back to
every open stage that can close into it, so an undo returns a job to the stage it
left: ``passed`` to review and qualified, ``lost`` to review, qualified, applied and
interviewing. ``hired`` stays final. The preset's current version moves to it, and
every opportunity on the old current version moves with it, as 0018 did. The new
version is a strict superset with the same stages and outcomes, so each opportunity
keeps its stage, its field states and its disposition. A reopen is an ordinary
transition into an open stage, which clears the disposition and is audited and
revision-checked like any other move.

Nothing is deleted or rewritten in place; a workspace whose current version already
has both moves is left alone.
"""

from alembic import op

revision = "0019_reopen_closed_jobs"
down_revision = "0018_invited_jobs"
branch_labels = None
depends_on = None
# Adds rows and repoints opportunities to a superset of what they had; nothing is
# removed or narrowed.
non_destructive_upgrade = True

PRESET = "019bf0b0-0000-7000-8000-000000000002"
REOPEN = (
    ("passed", "review"),
    ("passed", "qualified"),
    ("lost", "review"),
    ("lost", "qualified"),
    ("lost", "applied"),
    ("lost", "interviewing"),
)


def upgrade() -> None:
    pairs = ", ".join(f"('{start}', '{end}')" for start, end in REOPEN)
    op.execute(
        f"""
DO $$
DECLARE
    from_v INTEGER;
    to_v INTEGER;
BEGIN
    SELECT current_version INTO from_v
    FROM leads.pipeline_preset WHERE id = '{PRESET}';
    IF from_v IS NULL OR (
        SELECT count(*) FROM leads.preset_transition
        WHERE preset_id = '{PRESET}' AND version = from_v
          AND (from_stage_id, to_stage_id) IN ({pairs})
    ) = {len(REOPEN)} THEN
        RETURN;
    END IF;
    to_v := from_v + 1;
    INSERT INTO leads.preset_version
        (preset_id, version, created_at, created_by_id, note)
    VALUES ('{PRESET}', to_v, CURRENT_TIMESTAMP, NULL,
            'Package preset: reopen closed jobs');
    INSERT INTO leads.preset_stage
        (preset_id, version, stage_id, label, ordinal, kind, outcome)
    SELECT preset_id, to_v, stage_id, label, ordinal, kind, outcome
    FROM leads.preset_stage WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_transition
        (preset_id, version, from_stage_id, to_stage_id)
    SELECT preset_id, to_v, from_stage_id, to_stage_id
    FROM leads.preset_transition WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_transition
        (preset_id, version, from_stage_id, to_stage_id)
    SELECT '{PRESET}', to_v, pair.start, pair.finish
    FROM (VALUES {pairs}) AS pair (start, finish)
    WHERE EXISTS (
        SELECT 1 FROM leads.preset_stage s
        WHERE s.preset_id = '{PRESET}' AND s.version = from_v
          AND s.stage_id = pair.start
    ) AND EXISTS (
        SELECT 1 FROM leads.preset_stage s
        WHERE s.preset_id = '{PRESET}' AND s.version = from_v
          AND s.stage_id = pair.finish
    ) AND NOT EXISTS (
        SELECT 1 FROM leads.preset_transition tr
        WHERE tr.preset_id = '{PRESET}' AND tr.version = from_v
          AND tr.from_stage_id = pair.start AND tr.to_stage_id = pair.finish
    );
    INSERT INTO leads.preset_requirement (preset_id, version, to_stage_id, field)
    SELECT preset_id, to_v, to_stage_id, field
    FROM leads.preset_requirement WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_field
        (preset_id, version, target, type, required, sensitivity, enum_values)
    SELECT preset_id, to_v, target, type, required, sensitivity, enum_values
    FROM leads.preset_field WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_rubric (preset_id, version, rubric_slug, rubric_version)
    SELECT preset_id, to_v, rubric_slug, rubric_version
    FROM leads.preset_rubric WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_template (preset_id, version, kind, body)
    SELECT preset_id, to_v, kind, body
    FROM leads.preset_template WHERE preset_id = '{PRESET}' AND version = from_v;
    UPDATE leads.pipeline_preset SET current_version = to_v
    WHERE id = '{PRESET}' AND current_version = from_v;
    UPDATE leads.opportunity SET preset_version = to_v
    WHERE preset_id = '{PRESET}' AND preset_version = from_v;
END $$;
"""
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
