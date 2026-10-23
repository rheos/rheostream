"""Frozen: the Job search preset and the job mappings learn ``invited``.

A preset version is immutable once used, so the field arrives in a new version: a
full copy of the preset's current version (stages, transitions, requirements,
fields, rubric, template) plus one boolean field, ``ext.jobsearch.invited``. The
preset's current version moves to it, and every opportunity on the old current
version moves with it. The new version is a strict superset with the same stages
and outcomes, so each opportunity keeps its stage, its field states and its
disposition; none is orphaned. Opportunities still on an older version stay there.

Mappings are versioned the same way: every intake connection whose job feed or job
paste mapping lacks the rule gets a new mapping version, a copy of its current one
plus ``invited`` read from ``/invited``, and moves to it. The old versions stay,
since accepted receipts name the version they were processed under.

Nothing is deleted or rewritten in place; a workspace already carrying the field
or the rule is left alone.
"""

from alembic import op

revision = "0018_invited_jobs"
down_revision = "0017_proposal_separate_answers"
branch_labels = None
depends_on = None
# Adds rows and repoints opportunities and connections to supersets of what they
# had; nothing is removed or narrowed.
non_destructive_upgrade = True

PRESET = "019bf0b0-0000-7000-8000-000000000002"
FIELD = "ext.jobsearch.invited"
MAPPINGS = ("job_feed", "job_paste")


def upgrade() -> None:
    op.execute(
        f"""
DO $$
DECLARE
    from_v INTEGER;
    to_v INTEGER;
BEGIN
    SELECT current_version INTO from_v
    FROM leads.pipeline_preset WHERE id = '{PRESET}';
    IF from_v IS NULL OR EXISTS (
        SELECT 1 FROM leads.preset_field
        WHERE preset_id = '{PRESET}' AND version = from_v AND target = '{FIELD}'
    ) THEN
        RETURN;
    END IF;
    to_v := from_v + 1;
    INSERT INTO leads.preset_version
        (preset_id, version, created_at, created_by_id, note)
    VALUES ('{PRESET}', to_v, CURRENT_TIMESTAMP, NULL, 'Package preset: invited jobs');
    INSERT INTO leads.preset_stage
        (preset_id, version, stage_id, label, ordinal, kind, outcome)
    SELECT preset_id, to_v, stage_id, label, ordinal, kind, outcome
    FROM leads.preset_stage WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_transition
        (preset_id, version, from_stage_id, to_stage_id)
    SELECT preset_id, to_v, from_stage_id, to_stage_id
    FROM leads.preset_transition WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_requirement (preset_id, version, to_stage_id, field)
    SELECT preset_id, to_v, to_stage_id, field
    FROM leads.preset_requirement WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_field
        (preset_id, version, target, type, required, sensitivity, enum_values)
    SELECT preset_id, to_v, target, type, required, sensitivity, enum_values
    FROM leads.preset_field WHERE preset_id = '{PRESET}' AND version = from_v;
    INSERT INTO leads.preset_field
        (preset_id, version, target, type, required, sensitivity, enum_values)
    VALUES ('{PRESET}', to_v, '{FIELD}', 'boolean', false, 'internal',
            ARRAY[]::TEXT[]);
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
    names = ", ".join(f"'{name}'" for name in MAPPINGS)
    op.execute(
        f"""
DO $$
DECLARE
    c RECORD;
    to_v INTEGER;
BEGIN
    FOR c IN
        SELECT ic.id, ic.mapping_id, ic.mapping_version
        FROM leads.intake_connection ic
        JOIN leads.field_mapping m
          ON m.id = ic.mapping_id AND m.version = ic.mapping_version
        WHERE m.name IN ({names})
          AND NOT EXISTS (
              SELECT 1 FROM leads.field_mapping_rule r
              WHERE r.mapping_id = ic.mapping_id
                AND r.version = ic.mapping_version
                AND r.target = '{FIELD}'
          )
    LOOP
        SELECT max(version) + 1 INTO to_v
        FROM leads.field_mapping WHERE id = c.mapping_id;
        INSERT INTO leads.field_mapping (id, version, name, source_kind, state)
        SELECT id, to_v, name, source_kind, state
        FROM leads.field_mapping
        WHERE id = c.mapping_id AND version = c.mapping_version;
        INSERT INTO leads.field_mapping_identity
            (mapping_id, version, event_id_path, occurred_at_path, subject_id_path,
             verified_email_path)
        SELECT mapping_id, to_v, event_id_path, occurred_at_path, subject_id_path,
               verified_email_path
        FROM leads.field_mapping_identity
        WHERE mapping_id = c.mapping_id AND version = c.mapping_version;
        INSERT INTO leads.field_mapping_rule
            (mapping_id, version, target, source_path, transform, transform_arg,
             required, clear_on_null)
        SELECT mapping_id, to_v, target, source_path, transform, transform_arg,
               required, clear_on_null
        FROM leads.field_mapping_rule
        WHERE mapping_id = c.mapping_id AND version = c.mapping_version;
        INSERT INTO leads.field_mapping_rule
            (mapping_id, version, target, source_path, transform, transform_arg,
             required, clear_on_null)
        VALUES (c.mapping_id, to_v, '{FIELD}', '/invited', NULL, NULL, false, false);
        UPDATE leads.intake_connection SET mapping_version = to_v WHERE id = c.id;
    END LOOP;
END $$;
"""
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
