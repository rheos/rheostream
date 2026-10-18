"""Proposal drafting's tables: the proposal profile, each job's proposal, its sections
and the answer library.

``proposal_profile`` holds at most one row: the owner's proposal evidence text and
writing policy (notes, example letters, vouched anecdotes, platform notes), which are
the workspace's own data and never package content. ``job_proposal`` holds one row per
Job search opportunity a draft was asked for: its questions, the drafting state and
progress, the findings and the retry bookkeeping. ``proposal_section`` holds the draft
itself, one row per part an application form takes separately: the cover letter, then
one answer per screening question. ``answer_library`` holds hand-written answers keyed
by the normalised question, for reuse across jobs. ``job_pricing`` holds one row per
job whose pricing was asked for: its mode, the milestone generation's state and
outcome; ``proposal_milestone`` holds the schedule, one row per stage.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from rheo_leads.storage.tables import metadata

PROPOSAL_STATES = ("idle", "queued", "drafting", "ready", "failed")
SECTION_KINDS = ("cover_letter", "answer")

proposal_profile = Table(
    "proposal_profile",
    metadata,
    Column("singleton", Boolean, primary_key=True, server_default="true"),
    Column("profile", Text, nullable=False),
    # The writing policy: notes, example letters, anecdotes, platform notes. Read
    # whole, never queried.
    Column("policy", JSONB, nullable=False),
    Column("version", Integer, nullable=False),
    # The account whose model login drafts: the owner who set the profile.
    Column("drafting_account_id", Uuid, nullable=False),
    Column("updated_by_kind", Text, nullable=False),
    Column("updated_by_id", Uuid),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("singleton", name="proposal_profile_singleton"),
    CheckConstraint("version > 0", name="proposal_profile_version"),
)

job_proposal = Table(
    "job_proposal",
    metadata,
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("state", Text, nullable=False),
    # The screening questions, one per line, as the owner entered them or as the
    # posting embedded them. The source of record for the next draft.
    Column("questions", Text),
    Column("stage", Text),
    Column("step", Integer),
    Column("total", Integer),
    # What the checks found and what the analysis decided, for the findings panel.
    Column("findings", JSONB),
    Column("generated_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # While queued, when it may run; while drafting, when its claim lapses.
    Column("retry_after", DateTime(timezone=True)),
    Column("failure_kind", Text),
    Column("profile_version", Integer),
    Column("model_id", Text),
    Column("prompt_version", Text),
    Column("requested_by_kind", Text, nullable=False),
    Column("requested_by_id", Uuid),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "state IN ('idle','queued','drafting','ready','failed')",
        name="job_proposal_state_known",
    ),
    CheckConstraint("attempts >= 0", name="job_proposal_attempts"),
)

answer_library = Table(
    "answer_library",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("question", Text, nullable=False),
    # Lowercased, whitespace collapsed, surrounding punctuation stripped: two
    # phrasings that differ only in those ways share one entry.
    Column("question_key", Text, nullable=False, unique=True),
    Column("answer", Text, nullable=False),
    Column("updated_by_kind", Text, nullable=False),
    Column("updated_by_id", Uuid),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

proposal_section = Table(
    "proposal_section",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.job_proposal.opportunity_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("position", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("question", Text),
    Column("body", Text, nullable=False),
    # Set when the body was taken from the library; a regenerate then needs force.
    Column(
        "library_answer_id",
        Uuid,
        ForeignKey("leads.answer_library.id", ondelete="SET NULL"),
    ),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("opportunity_id", "position", name="proposal_section_position"),
    CheckConstraint(
        "kind IN ('cover_letter','answer')", name="proposal_section_kind_known"
    ),
    CheckConstraint(
        "(kind = 'cover_letter') = (position = 0)", name="proposal_section_letter_first"
    ),
    CheckConstraint(
        "(kind = 'answer') = (question IS NOT NULL)", name="proposal_section_question"
    ),
)

PRICING_STATES = ("idle", "queued", "running", "ready", "failed")
PRICING_MODES = ("hourly", "milestones")

job_pricing = Table(
    "job_pricing",
    metadata,
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # How the application prices: generating milestones chooses "milestones".
    Column("mode", Text),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # While queued, when it may run; while running, when its claim lapses.
    Column("retry_after", DateTime(timezone=True)),
    Column("failure_kind", Text),
    # The last generation's outcome: the scope-fit warning, or why the posting
    # was too vague to stage.
    Column("warning", Text),
    Column("too_vague", Text),
    Column("generated_at", DateTime(timezone=True)),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "state IN ('idle','queued','running','ready','failed')",
        name="job_pricing_state_known",
    ),
    CheckConstraint(
        "mode IS NULL OR mode IN ('hourly','milestones')", name="job_pricing_mode_known"
    ),
    CheckConstraint("attempts >= 0", name="job_pricing_attempts"),
)

proposal_milestone = Table(
    "proposal_milestone",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.job_pricing.opportunity_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("position", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("amount", Numeric(12, 2), nullable=False),
    Column("due_on", Date),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("opportunity_id", "position", name="proposal_milestone_position"),
    CheckConstraint("amount >= 0", name="proposal_milestone_amount"),
)
