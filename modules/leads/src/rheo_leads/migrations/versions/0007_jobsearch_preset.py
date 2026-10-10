"""Frozen: seed the Job search preset used by the job-search capability."""

from alembic import op

revision = "0007_jobsearch_preset"
down_revision = "0006_assessment_evidence_digest"
branch_labels = None
depends_on = None
# Inserts one new preset and its rows; touches no existing record.
non_destructive_upgrade = True

PRESET = "019bf0b0-0000-7000-8000-000000000002"
STAGES = (
    ("review", "Review", "open", None),
    ("qualified", "Qualified", "open", None),
    ("applied", "Applied", "open", None),
    ("interviewing", "Interviewing", "open", None),
    ("hired", "Hired", "terminal", "succeeded"),
    ("lost", "Lost", "terminal", "lost"),
    ("passed", "Passed", "terminal", "disqualified"),
)
TRANSITIONS = (
    ("review", "qualified"),
    ("review", "passed"),
    ("review", "lost"),
    ("qualified", "review"),
    ("qualified", "applied"),
    ("qualified", "passed"),
    ("qualified", "lost"),
    ("applied", "interviewing"),
    ("applied", "hired"),
    ("applied", "lost"),
    ("interviewing", "hired"),
    ("interviewing", "lost"),
)
# (name, type, enum values). Listing and client facts are business data from a
# public posting, so they are internal rather than restricted.
FIELDS = (
    ("platform", "enum", ("upwork", "braintrust", "other")),
    ("posting_id", "text", ()),
    ("posting_url", "text", ()),
    ("job_type", "enum", ("fixed", "hourly")),
    ("budget_min", "decimal", ()),
    ("budget_max", "decimal", ()),
    ("proposal_count", "integer", ()),
    ("proposal_count_exact", "boolean", ()),
    ("proposal_observed_at", "text", ()),
    ("posted_on", "date", ()),
    ("client_payment_verified", "boolean", ()),
    ("client_spend", "decimal", ()),
    ("client_rating", "decimal", ()),
    ("client_reviews", "integer", ()),
    ("client_hire_rate", "decimal", ()),
    ("applied", "boolean", ()),
)


def upgrade() -> None:
    op.execute(
        f"INSERT INTO leads.pipeline_preset VALUES "
        f"('{PRESET}', 'Job search', 1, CURRENT_TIMESTAMP)"
    )
    op.execute(
        f"INSERT INTO leads.preset_version VALUES "
        f"('{PRESET}', 1, CURRENT_TIMESTAMP, NULL, 'Package preset')"
    )
    for ordinal, (stage, label, kind, outcome) in enumerate(STAGES):
        value = "NULL" if outcome is None else f"'{outcome}'"
        op.execute(
            f"INSERT INTO leads.preset_stage VALUES "
            f"('{PRESET}', 1, '{stage}', '{label}', {ordinal}, '{kind}', {value})"
        )
    for source, target in TRANSITIONS:
        op.execute(
            f"INSERT INTO leads.preset_transition VALUES "
            f"('{PRESET}', 1, '{source}', '{target}')"
        )
    for name, kind, values in FIELDS:
        enum = "ARRAY[" + ",".join(f"'{v}'" for v in values) + "]::TEXT[]"
        op.execute(
            f"INSERT INTO leads.preset_field VALUES ('{PRESET}', 1, "
            f"'ext.jobsearch.{name}', '{kind}', false, 'internal', {enum})"
        )
    op.execute(
        f"INSERT INTO leads.preset_rubric VALUES ('{PRESET}', 1, 'job_search', 1)"
    )
    op.execute(
        f"INSERT INTO leads.preset_template VALUES "
        f"('{PRESET}', 1, 'followup', 'Thanks for considering my application.')"
    )


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
