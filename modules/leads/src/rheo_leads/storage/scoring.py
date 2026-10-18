"""Job scoring's tables: the job-search profile and each job's scoring state.

``jobsearch_profile`` holds at most one row: the owner's profile text and scoring
policy, which are the workspace's own data and never package content.
``job_score_state`` holds one row per Job search opportunity the scorer has seen: the
digest of the inputs it last scored or is about to score, and the retry bookkeeping.
The score itself lives on the qualification record.
``job_promotion`` holds one row per job the owner promoted past the client filters:
an override of tier 1, kept apart from the scoring state so that rescoring, a moved
posting and a new profile all leave it in place.
``model_call`` holds one row per model call Leads makes (scoring, drafting, pricing):
what it was for, the model, the token counts, how long it took and how it ended. It
is content-free: no task, answer or posting text, only counts, times and fixed words.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from rheo_leads.storage.tables import metadata

SCORE_STATES = ("pending", "scored", "failed")

jobsearch_profile = Table(
    "jobsearch_profile",
    metadata,
    Column("singleton", Boolean, primary_key=True, server_default="true"),
    Column("profile", Text, nullable=False),
    # The scoring policy document: goals, exclusions, economics, known gaps,
    # per-platform notes and the tier-1 thresholds. Read whole, never queried.
    Column("policy", JSONB, nullable=False),
    Column("version", Integer, nullable=False),
    # The account whose model login scores the jobs: the owner who set the profile.
    Column("scoring_account_id", Uuid, nullable=False),
    Column("updated_by_kind", Text, nullable=False),
    Column("updated_by_id", Uuid),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("singleton", name="jobsearch_profile_singleton"),
    CheckConstraint("version > 0", name="jobsearch_profile_version"),
)

job_score_state = Table(
    "job_score_state",
    metadata,
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("input_digest", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("retry_after", DateTime(timezone=True)),
    Column("failure_kind", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "state IN ('pending','scored','failed')", name="job_score_state_known"
    ),
    CheckConstraint("attempts >= 0", name="job_score_state_attempts"),
)

job_promotion = Table(
    "job_promotion",
    metadata,
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("promoted_by_kind", Text, nullable=False),
    Column("promoted_by_id", Uuid),
    Column("promoted_at", DateTime(timezone=True), nullable=False),
)

CALL_OUTCOMES = ("running", "answered", "deferred", "failed")

model_call = Table(
    "model_call",
    metadata,
    Column("id", Uuid, primary_key=True),
    # A fixed word: scoring, or the proposal chain's role (analysis, gate, draft,
    # repair, milestones).
    Column("purpose", Text, nullable=False),
    # The job it was for; a deleted job leaves its usage behind without the link.
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="SET NULL"),
    ),
    # The model setting's value when the call started, or "default".
    Column("model_id", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("outcome", Text, nullable=False),
    Column("failure_kind", Text),
    CheckConstraint(
        "outcome IN ('running','answered','deferred','failed')",
        name="model_call_outcome_known",
    ),
    CheckConstraint(
        "(outcome = 'running') = (finished_at IS NULL)",
        name="model_call_finished_when_done",
    ),
    CheckConstraint(
        "input_tokens IS NULL OR input_tokens >= 0", name="model_call_input_tokens"
    ),
    CheckConstraint(
        "output_tokens IS NULL OR output_tokens >= 0",
        name="model_call_output_tokens",
    ),
    Index("model_call_started", "started_at"),
    Index("model_call_opportunity", "opportunity_id"),
)
