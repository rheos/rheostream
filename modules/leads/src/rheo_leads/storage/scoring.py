"""Job scoring's tables: the job-search profile and each job's scoring state.

``jobsearch_profile`` holds at most one row: the owner's profile text and scoring
policy, which are the workspace's own data and never package content.
``job_score_state`` holds one row per Job search opportunity the scorer has seen: the
digest of the inputs it last scored or is about to score, and the retry bookkeeping.
The score itself lives on the qualification record.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
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
