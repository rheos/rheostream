"""Frozen initial intake DDL; independent of future application metadata."""

from alembic import op

revision = "0002_intake_tables"
down_revision = "0001_schema"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE leads.field_mapping (
    id UUID NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (id, version),
    CONSTRAINT mapping_source_kind CHECK (source_kind IN ('json', 'csv')),
    CONSTRAINT mapping_positive_version CHECK (version > 0)
)""",
    """CREATE TABLE leads.funnel (
    id UUID NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    archived_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id)
)""",
    """CREATE TABLE leads.campaign (
    id UUID NOT NULL,
    funnel_id UUID NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    archived_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY(funnel_id) REFERENCES leads.funnel (id),
    CONSTRAINT campaign_funnel_identity UNIQUE (id, funnel_id)
)""",
    """CREATE TABLE leads.field_mapping_identity (
    mapping_id UUID NOT NULL,
    version INTEGER NOT NULL,
    event_id_path TEXT,
    occurred_at_path TEXT,
    subject_id_path TEXT,
    verified_email_path TEXT,
    PRIMARY KEY (mapping_id, version),
    FOREIGN KEY(mapping_id, version) REFERENCES leads.field_mapping (id,
        version)
)""",
    """CREATE TABLE leads.field_mapping_rule (
    mapping_id UUID NOT NULL,
    version INTEGER NOT NULL,
    target TEXT NOT NULL,
    source_path TEXT NOT NULL,
    transform TEXT,
    transform_arg TEXT,
    required BOOLEAN NOT NULL,
    clear_on_null BOOLEAN NOT NULL,
    PRIMARY KEY (mapping_id, version, target),
    FOREIGN KEY(mapping_id, version) REFERENCES leads.field_mapping (id,
        version),
    CONSTRAINT mapping_transform CHECK (transform IN
        ('trim','lower','email_normalize','phone_normalize','datetime','const'))
)""",
    """CREATE TABLE leads.intake_connection (
    id UUID NOT NULL,
    name TEXT NOT NULL,
    transport TEXT NOT NULL,
    source_namespace TEXT NOT NULL,
    mapping_id UUID NOT NULL,
    mapping_version INTEGER NOT NULL,
    funnel_id UUID,
    campaign_id UUID,
    priority INTEGER NOT NULL,
    subject_authenticated BOOLEAN NOT NULL,
    email_verified BOOLEAN NOT NULL,
    signing_secret_ref TEXT,
    signing_key_generation INTEGER DEFAULT '1' NOT NULL,
    previous_secret_ref TEXT,
    previous_valid_until TIMESTAMP WITH TIME ZONE,
    state TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY(mapping_id, mapping_version) REFERENCES leads.field_mapping
        (id, version),
    CONSTRAINT connection_transport CHECK (transport IN
        ('webhook','import','manual')),
    CONSTRAINT connection_state CHECK (state IN
        ('active','needs_credential','revoked')),
    CONSTRAINT connection_funnel CHECK (transport = 'manual' OR funnel_id IS
        NOT NULL),
    UNIQUE (source_namespace),
    FOREIGN KEY(funnel_id) REFERENCES leads.funnel (id),
    CONSTRAINT intake_connection_campaign_funnel FOREIGN KEY(campaign_id,
        funnel_id) REFERENCES leads.campaign (id, funnel_id)
)""",
    """CREATE TABLE leads.connection_health (
    connection_id UUID NOT NULL,
    last_accepted_at TIMESTAMP WITH TIME ZONE,
    last_processed_at TIMESTAMP WITH TIME ZONE,
    pending_count INTEGER DEFAULT '0' NOT NULL,
    unresolved_failures INTEGER DEFAULT '0' NOT NULL,
    last_error TEXT,
    last_error_at TIMESTAMP WITH TIME ZONE,
    last_unauthenticated_write_at TIMESTAMP WITH TIME ZONE,
    lag_seconds INTEGER DEFAULT '0' NOT NULL,
    PRIMARY KEY (connection_id),
    FOREIGN KEY(connection_id) REFERENCES leads.intake_connection (id)
)""",
    """CREATE TABLE leads.delivery_receipt (
    id UUID NOT NULL,
    connection_id UUID NOT NULL,
    source_event_id TEXT NOT NULL,
    content_digest BYTEA NOT NULL,
    byte_length INTEGER NOT NULL,
    transport TEXT NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_occurred_at TIMESTAMP WITH TIME ZONE,
    mapping_id UUID NOT NULL,
    mapping_version INTEGER NOT NULL,
    funnel_id UUID NOT NULL,
    campaign_id UUID,
    signing_key_generation INTEGER,
    state TEXT NOT NULL,
    state_detail TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY(mapping_id, mapping_version) REFERENCES leads.field_mapping
        (id, version),
    CONSTRAINT receipt_event_identity UNIQUE (connection_id, source_event_id),
    CONSTRAINT receipt_state CHECK (state IN
        ('pending','processed','refused')),
    FOREIGN KEY(connection_id) REFERENCES leads.intake_connection (id),
    FOREIGN KEY(funnel_id) REFERENCES leads.funnel (id),
    CONSTRAINT delivery_receipt_campaign_funnel FOREIGN KEY(campaign_id,
        funnel_id) REFERENCES leads.campaign (id, funnel_id)
)""",
    """CREATE TABLE leads.import_batch (
    id UUID NOT NULL,
    connection_id UUID NOT NULL,
    file_ref TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    refused INTEGER NOT NULL,
    state TEXT NOT NULL,
    state_detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT import_state CHECK (state IN
        ('queued','running','completed','refused','failed')),
    FOREIGN KEY(connection_id) REFERENCES leads.intake_connection (id)
)""",
    """CREATE TABLE leads.delivery_conflict (
    id UUID NOT NULL,
    receipt_id UUID NOT NULL,
    kind TEXT NOT NULL,
    content_digest BYTEA NOT NULL,
    byte_length INTEGER NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    body BYTEA,
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by_id UUID,
    resolution_note TEXT,
    PRIMARY KEY (id),
    CONSTRAINT conflict_kind CHECK (kind IN
        ('digest_differs','deleted_observation')),
    CONSTRAINT erased_conflict_no_body CHECK (kind <> 'deleted_observation' OR
        body IS NULL),
    FOREIGN KEY(receipt_id) REFERENCES leads.delivery_receipt (id)
)""",
    """CREATE TABLE leads.delivery_payload (
    receipt_id UUID NOT NULL,
    body BYTEA NOT NULL,
    content_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    PRIMARY KEY (receipt_id),
    FOREIGN KEY(receipt_id) REFERENCES leads.delivery_receipt (id)
)""",
    """CREATE TABLE leads.observation (
    id UUID NOT NULL,
    receipt_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    funnel_id UUID NOT NULL,
    campaign_id UUID,
    transport TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    external_subject_id TEXT,
    verified_email TEXT,
    source_occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    mapping_version INTEGER NOT NULL,
    completeness INTEGER NOT NULL,
    party_ref TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT observation_receipt_identity CHECK (id = receipt_id),
    UNIQUE (receipt_id),
    FOREIGN KEY(receipt_id) REFERENCES leads.delivery_receipt (id),
    FOREIGN KEY(connection_id) REFERENCES leads.intake_connection (id),
    FOREIGN KEY(funnel_id) REFERENCES leads.funnel (id),
    CONSTRAINT observation_campaign_funnel FOREIGN KEY(campaign_id, funnel_id)
        REFERENCES leads.campaign (id, funnel_id)
)""",
    """CREATE TABLE leads.observation_field (
    observation_id UUID NOT NULL,
    target TEXT NOT NULL,
    value_kind TEXT NOT NULL,
    value_text TEXT,
    source_path TEXT NOT NULL,
    transform TEXT,
    mapping_version INTEGER NOT NULL,
    PRIMARY KEY (observation_id, target),
    CONSTRAINT fact_value_kind CHECK (value_kind IN ('value','cleared')),
    FOREIGN KEY(observation_id) REFERENCES leads.observation (id)
)""",
    """CREATE UNIQUE INDEX one_manual_connection ON leads.intake_connection
        (transport) WHERE transport = 'manual'""",
    """CREATE INDEX import_connection_created ON leads.import_batch (connection_id,
        created_at DESC)""",
    """CREATE INDEX conflict_receipt ON leads.delivery_conflict (receipt_id)""",
    """CREATE INDEX observation_connection ON leads.observation (connection_id)""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
