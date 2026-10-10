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
)
UniqueConstraint(
    handoff.c.opportunity_id, handoff.c.idempotency_key, name="handoff_request_identity"
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
