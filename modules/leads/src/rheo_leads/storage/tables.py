"""The intake schema; all foreign keys stay inside Leads."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

metadata = MetaData(schema="leads")

funnel = Table(
    "funnel",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("archived_at", DateTime(timezone=True)),
)
campaign = Table(
    "campaign",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("funnel_id", Uuid, ForeignKey("leads.funnel.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("archived_at", DateTime(timezone=True)),
)
# A composite reference makes campaign/funnel consistency a database invariant.
UniqueConstraint(campaign.c.id, campaign.c.funnel_id, name="campaign_funnel_identity")

field_mapping = Table(
    "field_mapping",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    CheckConstraint("source_kind IN ('json', 'csv')", name="mapping_source_kind"),
    CheckConstraint("version > 0", name="mapping_positive_version"),
)
field_mapping_identity = Table(
    "field_mapping_identity",
    metadata,
    Column("mapping_id", Uuid, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("event_id_path", Text),
    Column("occurred_at_path", Text),
    Column("subject_id_path", Text),
    Column("verified_email_path", Text),
    ForeignKeyConstraint(
        ["mapping_id", "version"],
        ["leads.field_mapping.id", "leads.field_mapping.version"],
    ),
)
field_mapping_rule = Table(
    "field_mapping_rule",
    metadata,
    Column("mapping_id", Uuid, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("target", Text, primary_key=True),
    Column("source_path", Text, nullable=False),
    Column("transform", Text),
    Column("transform_arg", Text),
    Column("required", Boolean, nullable=False),
    Column("clear_on_null", Boolean, nullable=False),
    ForeignKeyConstraint(
        ["mapping_id", "version"],
        ["leads.field_mapping.id", "leads.field_mapping.version"],
    ),
    CheckConstraint(
        "transform IN ('trim','lower','email_normalize','phone_normalize',"
        "'datetime','const')",
        name="mapping_transform",
    ),
)
intake_connection = Table(
    "intake_connection",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False),
    Column("transport", Text, nullable=False),
    Column("source_namespace", Text, nullable=False, unique=True),
    Column("mapping_id", Uuid, nullable=False),
    Column("mapping_version", Integer, nullable=False),
    Column("funnel_id", Uuid, ForeignKey("leads.funnel.id")),
    Column("campaign_id", Uuid),
    Column("priority", Integer, nullable=False),
    Column("subject_authenticated", Boolean, nullable=False),
    Column("email_verified", Boolean, nullable=False),
    Column("signing_secret_ref", Text),
    Column("signing_key_generation", Integer, nullable=False, server_default="1"),
    Column("previous_secret_ref", Text),
    Column("previous_valid_until", DateTime(timezone=True)),
    Column("state", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["mapping_id", "mapping_version"],
        ["leads.field_mapping.id", "leads.field_mapping.version"],
    ),
    CheckConstraint(
        "transport IN ('webhook','import','manual')", name="connection_transport"
    ),
    CheckConstraint(
        "state IN ('active','needs_credential','revoked')", name="connection_state"
    ),
    CheckConstraint(
        "transport = 'manual' OR funnel_id IS NOT NULL", name="connection_funnel"
    ),
)
Index(
    "one_manual_connection",
    intake_connection.c.transport,
    unique=True,
    postgresql_where=text("transport = 'manual'"),
)
connection_health = Table(
    "connection_health",
    metadata,
    Column(
        "connection_id",
        Uuid,
        ForeignKey("leads.intake_connection.id"),
        primary_key=True,
    ),
    Column("last_accepted_at", DateTime(timezone=True)),
    Column("last_processed_at", DateTime(timezone=True)),
    Column("pending_count", Integer, nullable=False, server_default="0"),
    Column("unresolved_failures", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("last_error_at", DateTime(timezone=True)),
    Column("last_unauthenticated_write_at", DateTime(timezone=True)),
    Column("lag_seconds", Integer, nullable=False, server_default="0"),
)
delivery_receipt = Table(
    "delivery_receipt",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "connection_id", Uuid, ForeignKey("leads.intake_connection.id"), nullable=False
    ),
    Column("source_event_id", Text, nullable=False),
    Column("content_digest", LargeBinary, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("transport", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("source_occurred_at", DateTime(timezone=True)),
    Column("mapping_id", Uuid, nullable=False),
    Column("mapping_version", Integer, nullable=False),
    Column("funnel_id", Uuid, ForeignKey("leads.funnel.id"), nullable=False),
    Column("campaign_id", Uuid),
    Column("signing_key_generation", Integer),
    Column("state", Text, nullable=False),
    Column("state_detail", Text),
    Column("processed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["mapping_id", "mapping_version"],
        ["leads.field_mapping.id", "leads.field_mapping.version"],
    ),
    UniqueConstraint("connection_id", "source_event_id", name="receipt_event_identity"),
    CheckConstraint("state IN ('pending','processed','refused')", name="receipt_state"),
)
delivery_payload = Table(
    "delivery_payload",
    metadata,
    Column(
        "receipt_id", Uuid, ForeignKey("leads.delivery_receipt.id"), primary_key=True
    ),
    Column("body", LargeBinary, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("byte_length", Integer, nullable=False),
)
delivery_conflict = Table(
    "delivery_conflict",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("receipt_id", Uuid, ForeignKey("leads.delivery_receipt.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("content_digest", LargeBinary, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("body", LargeBinary),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolved_by_id", Uuid),
    Column("resolution_note", Text),
    CheckConstraint(
        "kind IN ('digest_differs','deleted_observation')", name="conflict_kind"
    ),
    CheckConstraint(
        "kind <> 'deleted_observation' OR body IS NULL", name="erased_conflict_no_body"
    ),
)
Index("conflict_receipt", delivery_conflict.c.receipt_id)
observation = Table(
    "observation",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "receipt_id",
        Uuid,
        ForeignKey("leads.delivery_receipt.id"),
        nullable=False,
        unique=True,
    ),
    Column(
        "connection_id", Uuid, ForeignKey("leads.intake_connection.id"), nullable=False
    ),
    Column("funnel_id", Uuid, ForeignKey("leads.funnel.id"), nullable=False),
    Column("campaign_id", Uuid),
    Column("transport", Text, nullable=False),
    Column("source_event_id", Text, nullable=False),
    Column("external_subject_id", Text),
    Column("verified_email", Text),
    Column("source_occurred_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("mapping_version", Integer, nullable=False),
    Column("completeness", Integer, nullable=False),
    Column("party_ref", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = receipt_id", name="observation_receipt_identity"),
)
Index("observation_connection", observation.c.connection_id)
observation_field = Table(
    "observation_field",
    metadata,
    Column(
        "observation_id", Uuid, ForeignKey("leads.observation.id"), primary_key=True
    ),
    Column("target", Text, primary_key=True),
    Column("value_kind", Text, nullable=False),
    Column("value_text", Text),
    Column("source_path", Text, nullable=False),
    Column("transform", Text),
    Column("mapping_version", Integer, nullable=False),
    CheckConstraint("value_kind IN ('value','cleared')", name="fact_value_kind"),
)
import_batch = Table(
    "import_batch",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "connection_id", Uuid, ForeignKey("leads.intake_connection.id"), nullable=False
    ),
    Column("file_ref", Text, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("accepted", Integer, nullable=False),
    Column("refused", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("state_detail", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint(
        "state IN ('queued','running','completed','refused','failed')",
        name="import_state",
    ),
)
for attributed in (intake_connection, delivery_receipt, observation):
    attributed.append_constraint(
        ForeignKeyConstraint(
            ["campaign_id", "funnel_id"],
            ["leads.campaign.id", "leads.campaign.funnel_id"],
            name=f"{attributed.name}_campaign_funnel",
        )
    )

Index(
    "import_connection_created",
    import_batch.c.connection_id,
    import_batch.c.created_at.desc(),
)
