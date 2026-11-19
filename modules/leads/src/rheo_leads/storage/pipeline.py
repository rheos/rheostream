"""Opportunity-owned rows. No foreign key leaves the Leads schema."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    func,
    or_,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from rheo_leads.storage.tables import metadata


def col(name: str, kind: object = Text, *, nullable: bool = False) -> Column:  # type: ignore[type-arg]
    return Column(name, kind, nullable=nullable)  # type: ignore[arg-type]


def idcol() -> Column:  # type: ignore[type-arg]
    return Column("id", Uuid, primary_key=True)


def version_columns() -> list:  # type: ignore[type-arg]
    return [
        Column("preset_id", Uuid, primary_key=True),
        Column("version", Integer, primary_key=True),
    ]


def version_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["preset_id", "version"],
        ["leads.preset_version.preset_id", "leads.preset_version.version"],
    )


pipeline_preset = Table(
    "pipeline_preset",
    metadata,
    idcol(),
    col("name"),
    col("current_version", Integer),
    col("created_at", DateTime(timezone=True)),
)
preset_version = Table(
    "preset_version",
    metadata,
    *version_columns(),
    col("created_at", DateTime(timezone=True)),
    col("created_by_id", Uuid, nullable=True),
    col("note"),
    ForeignKeyConstraint(["preset_id"], ["leads.pipeline_preset.id"]),
)
preset_stage = Table(
    "preset_stage",
    metadata,
    *version_columns(),
    Column("stage_id", Text, primary_key=True),
    col("label"),
    col("ordinal", Integer),
    col("kind"),
    col("outcome", nullable=True),
    version_fk(),
    CheckConstraint(
        "(kind = 'open' AND outcome IS NULL) OR (kind = 'terminal' "
        "AND outcome IN ('succeeded','lost','disqualified'))"
    ),
)
preset_transition = Table(
    "preset_transition",
    metadata,
    *version_columns(),
    Column("from_stage_id", Text, primary_key=True),
    Column("to_stage_id", Text, primary_key=True),
    ForeignKeyConstraint(
        ["preset_id", "version", "from_stage_id"],
        [
            "leads.preset_stage.preset_id",
            "leads.preset_stage.version",
            "leads.preset_stage.stage_id",
        ],
    ),
    ForeignKeyConstraint(
        ["preset_id", "version", "to_stage_id"],
        [
            "leads.preset_stage.preset_id",
            "leads.preset_stage.version",
            "leads.preset_stage.stage_id",
        ],
    ),
)
preset_requirement = Table(
    "preset_requirement",
    metadata,
    *version_columns(),
    Column("to_stage_id", Text, primary_key=True),
    Column("field", Text, primary_key=True),
    ForeignKeyConstraint(
        ["preset_id", "version", "to_stage_id"],
        [
            "leads.preset_stage.preset_id",
            "leads.preset_stage.version",
            "leads.preset_stage.stage_id",
        ],
    ),
)
preset_field = Table(
    "preset_field",
    metadata,
    *version_columns(),
    Column("target", Text, primary_key=True),
    col("type"),
    col("required", Boolean),
    col("sensitivity"),
    col("enum_values", ARRAY(Text)),
    version_fk(),
)
preset_rubric = Table(
    "preset_rubric",
    metadata,
    *version_columns(),
    col("rubric_slug"),
    col("rubric_version", Integer),
    version_fk(),
)
preset_template = Table(
    "preset_template",
    metadata,
    *version_columns(),
    Column("kind", Text, primary_key=True),
    col("body"),
    version_fk(),
)
pipeline = Table(
    "pipeline",
    metadata,
    idcol(),
    Column("preset_id", Uuid, ForeignKey("leads.pipeline_preset.id"), nullable=False),
    col("name"),
    col("owner_id", Uuid),
    col("created_at", DateTime(timezone=True)),
    UniqueConstraint("id", "preset_id"),
)
routing_rule = Table(
    "routing_rule",
    metadata,
    idcol(),
    Column(
        "connection_id", Uuid, ForeignKey("leads.intake_connection.id"), nullable=False
    ),
    col("ordinal", Integer),
    col("action"),
    Column("pipeline_id", Uuid, ForeignKey("leads.pipeline.id")),
    col("enabled", Boolean),
    col("match_field", nullable=True),
    UniqueConstraint("connection_id", "ordinal"),
    CheckConstraint(
        "action IN ('record_only','create_opportunity',"
        "'attach_to_open_opportunity','attach_by_field')",
        name="routing_rule_action_known",
    ),
    CheckConstraint("action = 'record_only' OR pipeline_id IS NOT NULL"),
    CheckConstraint(
        "(action = 'attach_by_field') = (match_field IS NOT NULL)",
        name="routing_rule_match_field",
    ),
)
routing_condition = Table(
    "routing_condition",
    metadata,
    Column("rule_id", Uuid, ForeignKey("leads.routing_rule.id"), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    col("field"),
    col("op"),
    col("value", nullable=True),
    CheckConstraint("op IN ('present','absent','equals','contains')"),
)
opportunity = Table(
    "opportunity",
    metadata,
    idcol(),
    col("pipeline_id", Uuid),
    col("preset_id", Uuid),
    col("preset_version", Integer),
    col("stage_id"),
    col("title"),
    col("owner_id", Uuid),
    Column("funnel_id", Uuid, ForeignKey("leads.funnel.id"), nullable=False),
    col("campaign_id", Uuid, nullable=True),
    Column(
        "source_observation_id", Uuid, ForeignKey("leads.observation.id"), nullable=True
    ),
    col("value_amount", Numeric, nullable=True),
    col("value_currency", nullable=True),
    col("value_basis", nullable=True),
    col("disposition_outcome", nullable=True),
    col("disposition_at", DateTime(timezone=True), nullable=True),
    col("revision", Integer),
    col("created_by_kind"),
    col("created_by_id", Uuid, nullable=True),
    col("created_at", DateTime(timezone=True)),
    col("updated_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["pipeline_id", "preset_id"], ["leads.pipeline.id", "leads.pipeline.preset_id"]
    ),
    ForeignKeyConstraint(
        ["preset_id", "preset_version", "stage_id"],
        [
            "leads.preset_stage.preset_id",
            "leads.preset_stage.version",
            "leads.preset_stage.stage_id",
        ],
    ),
    ForeignKeyConstraint(
        ["campaign_id", "funnel_id"], ["leads.campaign.id", "leads.campaign.funnel_id"]
    ),
    CheckConstraint("revision > 0"),
)


def opp_fk() -> Column:  # type: ignore[type-arg]
    return Column(
        "opportunity_id",
        Uuid,
        ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
        primary_key=True,
    )


opportunity_observation = Table(
    "opportunity_observation",
    metadata,
    opp_fk(),
    Column(
        "observation_id", Uuid, ForeignKey("leads.observation.id"), primary_key=True
    ),
    col("decision"),
    col("decided_by_kind"),
    col("decided_by_id", Uuid, nullable=True),
    col("decided_at", DateTime(timezone=True)),
)
opportunity_party = Table(
    "opportunity_party",
    metadata,
    opp_fk(),
    Column("party_ref", Text, primary_key=True),
    Column("role", Text, primary_key=True),
    col("added_at", DateTime(timezone=True)),
    col("added_by_kind"),
    col("added_by_id", Uuid, nullable=True),
    col("removed_at", DateTime(timezone=True), nullable=True),
)
opportunity_field_state = Table(
    "opportunity_field_state",
    metadata,
    opp_fk(),
    Column("target", Text, primary_key=True),
    col("value_kind"),
    col("value_text", nullable=True),
    col("owner"),
    Column("observation_id", Uuid, ForeignKey("leads.observation.id")),
    col("occurred_at", DateTime(timezone=True), nullable=True),
    col("completeness", Integer, nullable=True),
    col("priority", Integer, nullable=True),
    col("orphaned", Boolean),
    CheckConstraint("owner IN ('source','user')"),
    CheckConstraint("value_kind IN ('value','cleared')"),
)


def child(name: str, *columns: Column) -> Table:  # type: ignore[type-arg]
    return Table(
        name,
        metadata,
        idcol(),
        Column(
            "opportunity_id",
            Uuid,
            ForeignKey("leads.opportunity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *columns,
    )


opportunity_note = child(
    "opportunity_note",
    col("body"),
    col("stage_id", nullable=True),
    col("created_by_kind"),
    col("created_by_id", Uuid, nullable=True),
    col("created_at", DateTime(timezone=True)),
)
qualification = child(
    "qualification",
    col("preset_version", Integer),
    col("rubric_slug"),
    col("rubric_version", Integer),
    col("objective"),
    col("evidence_refs", ARRAY(Text)),
    col("input_revision", Integer),
    col("fit"),
    col("intent"),
    col("evidence_completeness"),
    col("urgency"),
    col("explanation"),
    col("uncertainty"),
    col("model_id", nullable=True),
    col("prompt_version", nullable=True),
    col("created_by_kind"),
    col("created_by_id", Uuid, nullable=True),
    col("created_at", DateTime(timezone=True)),
    col("evidence_digest", nullable=True),
    # 1-100 from the job scorer; null for every other assessment.
    col("score", Integer, nullable=True),
)
qualification.append_constraint(
    CheckConstraint("score BETWEEN 1 AND 100", name="qualification_score")
)
# A job's current assessment, read newest first (migration 0015).
Index(
    "qualification_current",
    qualification.c.opportunity_id,
    qualification.c.input_revision.desc(),
    qualification.c.created_at.desc(),
    qualification.c.id.desc(),
)
draft = child(
    "draft",
    col("kind"),
    col("body"),
    col("template_version", Integer, nullable=True),
    col("runtime_request_id", Uuid, nullable=True),
    col("created_by_kind"),
    col("created_by_id", Uuid, nullable=True),
    col("created_at", DateTime(timezone=True)),
)
followup_reminder = child(
    "followup_reminder",
    col("action"),
    col("due_on", Date),
    col("state"),
    col("created_by_kind"),
    col("created_by_id", Uuid, nullable=True),
    col("created_at", DateTime(timezone=True)),
    col("resolved_by_kind", nullable=True),
    col("resolved_by_id", Uuid, nullable=True),
    col("resolved_at", DateTime(timezone=True), nullable=True),
)
followup_reminder.append_constraint(
    CheckConstraint("state IN ('pending','completed','cancelled','superseded')")
)
followup_reminder.append_constraint(
    CheckConstraint("(state = 'pending') = (resolved_at IS NULL)")
)
Index(
    "followup_one_pending",
    followup_reminder.c.opportunity_id,
    unique=True,
    postgresql_where=followup_reminder.c.state == "pending",
)
Index("followup_due", followup_reminder.c.state, followup_reminder.c.due_on)
handoff = child(
    "handoff",
    col("purpose"),
    col("destination_ref", nullable=True),
    col("idempotency_key"),
    col("snapshot_digest", LargeBinary),
    col("state"),
    col("result_ref", nullable=True),
    col("result_detail", nullable=True),
    col("approval_id", Uuid, nullable=True),
    col("external_action_id", Uuid, nullable=True),
    col("requested_by_kind"),
    col("requested_by_id", Uuid, nullable=True),
    col("requested_at", DateTime(timezone=True)),
    col("resolved_at", DateTime(timezone=True), nullable=True),
    # Migration 0020: how many core attempts this handoff has staged, and the
    # minimal projection of Current's lasting source link. ``result_ref`` stays the
    # created item. No item state, availability or event position is stored here.
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    col("current_project_ref", nullable=True),
    col("last_current_project_ref", nullable=True),
    Column(
        "current_project_state",
        Text,
        nullable=False,
        server_default="never_assigned",
    ),
    col("current_link_revision", Integer, nullable=True),
)
UniqueConstraint(
    handoff.c.opportunity_id, handoff.c.idempotency_key, name="handoff_request_identity"
)
HANDOFF_STATES = (
    "unavailable",
    "pending",
    "succeeded",
    "failed",
    "refused",
    "cancelled",
    "unresolved",
)
for _name, _check in (
    (
        "handoff_state_known",
        "state IN (" + ",".join(f"'{s}'" for s in HANDOFF_STATES) + ")",
    ),
    (
        "handoff_attempts",
        "attempt_count >= 0 AND (attempt_count = 0) = (external_action_id IS NULL)",
    ),
    ("handoff_pending_staged", "state <> 'pending' OR external_action_id IS NOT NULL"),
    ("handoff_success_result", "state <> 'succeeded' OR result_ref IS NOT NULL"),
    (
        "handoff_project_state",
        "current_project_state IN ('never_assigned','linked','cleared','deleted')",
    ),
    (
        "handoff_project_live",
        "(current_project_state = 'linked') = (current_project_ref IS NOT NULL)",
    ),
    (
        "handoff_project_history",
        "current_project_state NOT IN ('cleared','deleted') "
        "OR last_current_project_ref IS NOT NULL",
    ),
    (
        "handoff_link_revision",
        "current_link_revision IS NULL OR current_link_revision >= 1",
    ),
    (
        "handoff_link_projection",
        "current_link_revision IS NOT NULL OR (current_project_state = "
        "'never_assigned' AND last_current_project_ref IS NULL)",
    ),
):
    handoff.append_constraint(CheckConstraint(_check, name=_name))
ACTION_ABSENT_DETAILS = ("restored_unresolved", "external_action_missing")
"""Details of an unresolved handoff whose core action is known to be absent (a
restore carries none, or a read proved it gone): rereading it can change nothing."""
NONTERMINAL_SCAN = or_(
    handoff.c.state == "pending",
    and_(
        handoff.c.state == "unresolved",
        handoff.c.external_action_id.is_not(None),
        func.coalesce(handoff.c.result_detail, "").not_in(ACTION_ABSENT_DETAILS),
    ),
)
"""The settlement scan's rows: pending handoffs, and unresolved ones whose core
action still exists, so a person's later ``core.operation.resolve`` reaches Leads."""
# The two repair scans page in (requested_at, id) order over their own class.
Index(
    "handoff_nonterminal_scan",
    handoff.c.requested_at,
    handoff.c.id,
    postgresql_where=NONTERMINAL_SCAN,
)
Index(
    "handoff_succeeded_scan",
    handoff.c.requested_at,
    handoff.c.id,
    postgresql_where=handoff.c.state == "succeeded",
)
handoff_snapshot = Table(
    "handoff_snapshot",
    metadata,
    Column(
        "handoff_id",
        Uuid,
        ForeignKey("leads.handoff.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    col("body", JSONB),
    col("byte_length", Integer),
    # Migration 0020: the latest qualification a request was made against, and
    # whether its evidence digest no longer matched. Null for older handoffs.
    col("qualification_ref", nullable=True),
    col("qualification_stale", Boolean, nullable=True),
    CheckConstraint(
        "(qualification_ref IS NULL) = (qualification_stale IS NULL)",
        name="handoff_snapshot_qualification",
    ),
)
current_link_cursor = Table(
    "current_link_cursor",
    metadata,
    # One row per repair scan; the complete last-examined (requested_at, id) key.
    Column("scan", Text, primary_key=True),
    col("after_requested_at", DateTime(timezone=True), nullable=True),
    col("after_id", Uuid, nullable=True),
    Column("cycles", Integer, nullable=False, server_default="0"),
    col("updated_at", DateTime(timezone=True)),
    CheckConstraint(
        "scan IN ('nonterminal','succeeded')", name="current_link_cursor_scan"
    ),
    CheckConstraint(
        "(after_requested_at IS NULL) = (after_id IS NULL)",
        name="current_link_cursor_key",
    ),
    CheckConstraint("cycles >= 0", name="current_link_cursor_cycles"),
)
contact_permission = Table(
    "contact_permission",
    metadata,
    idcol(),
    col("party_ref"),
    col("purpose"),
    col("channel", nullable=True),
    col("recipient_scope"),
    col("disclosure_version", nullable=True),
    Column("evidence_observation_id", Uuid, ForeignKey("leads.observation.id")),
    col("granted_at", DateTime(timezone=True), nullable=True),
    col("granted_by_kind", nullable=True),
    col("granted_by_id", Uuid, nullable=True),
    col("withdrawn_at", DateTime(timezone=True), nullable=True),
    col("withdrawn_reason", nullable=True),
    col("withdrawn_by_kind", nullable=True),
    col("withdrawn_by_id", Uuid, nullable=True),
)
contact_suppression = Table(
    "contact_suppression",
    metadata,
    Column("party_ref", Text, primary_key=True),
    col("suppressed_at", DateTime(timezone=True)),
    col("reason", nullable=True),
    col("suppressed_by_kind"),
    col("suppressed_by_id", Uuid, nullable=True),
)
