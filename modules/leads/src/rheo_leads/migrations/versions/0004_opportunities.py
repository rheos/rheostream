"""Frozen opportunity schema and package preset; no account-bound pipeline is seeded."""

from alembic import op

revision = "0004_opportunities"
down_revision = "0003_seed_manual_capture"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE leads.contact_suppression (
    party_ref TEXT NOT NULL,
    suppressed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason TEXT,
    suppressed_by_kind TEXT NOT NULL,
    suppressed_by_id UUID,
    PRIMARY KEY (party_ref)
)""",
    """CREATE TABLE leads.pipeline_preset (
    id UUID NOT NULL,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id)
)""",
    """CREATE TABLE leads.pipeline (
    id UUID NOT NULL,
    preset_id UUID NOT NULL,
    name TEXT NOT NULL,
    owner_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (id, preset_id),
    FOREIGN KEY(preset_id) REFERENCES leads.pipeline_preset (id)
)""",
    """CREATE TABLE leads.preset_version (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_by_id UUID,
    note TEXT NOT NULL,
    PRIMARY KEY (preset_id, version),
    FOREIGN KEY(preset_id) REFERENCES leads.pipeline_preset (id)
)""",
    """CREATE TABLE leads.preset_field (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    target TEXT NOT NULL,
    type TEXT NOT NULL,
    required BOOLEAN NOT NULL,
    sensitivity TEXT NOT NULL,
    enum_values TEXT[] NOT NULL,
    PRIMARY KEY (preset_id, version, target),
    FOREIGN KEY(preset_id, version) REFERENCES leads.preset_version (preset_id, version)
)""",
    """CREATE TABLE leads.preset_rubric (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    rubric_slug TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    PRIMARY KEY (preset_id, version),
    FOREIGN KEY(preset_id, version) REFERENCES leads.preset_version (preset_id, version)
)""",
    """CREATE TABLE leads.preset_stage (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    stage_id TEXT NOT NULL,
    label TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT,
    PRIMARY KEY (preset_id, version, stage_id),
    FOREIGN KEY(preset_id, version)
    REFERENCES leads.preset_version (preset_id, version),
        CHECK ((kind = 'open' AND outcome IS NULL) OR (kind = 'terminal' AND outcome
IN ('succeeded','lost','disqualified')))
)""",
    """CREATE TABLE leads.preset_template (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (preset_id, version, kind),
    FOREIGN KEY(preset_id, version) REFERENCES leads.preset_version (preset_id, version)
)""",
    """CREATE TABLE leads.preset_requirement (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    to_stage_id TEXT NOT NULL,
    field TEXT NOT NULL,
    PRIMARY KEY (preset_id, version, to_stage_id, field),
        FOREIGN KEY(preset_id, version, to_stage_id) REFERENCES leads.preset_stage
(preset_id, version, stage_id)
)""",
    """CREATE TABLE leads.preset_transition (
    preset_id UUID NOT NULL,
    version INTEGER NOT NULL,
    from_stage_id TEXT NOT NULL,
    to_stage_id TEXT NOT NULL,
    PRIMARY KEY (preset_id, version, from_stage_id, to_stage_id),
        FOREIGN KEY(preset_id, version, from_stage_id) REFERENCES leads.preset_stage
(preset_id, version, stage_id),
        FOREIGN KEY(preset_id, version, to_stage_id) REFERENCES leads.preset_stage
(preset_id, version, stage_id)
)""",
    """CREATE TABLE leads.routing_rule (
    id UUID NOT NULL,
    connection_id UUID NOT NULL,
    ordinal INTEGER NOT NULL,
    action TEXT NOT NULL,
    pipeline_id UUID,
    enabled BOOLEAN NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (connection_id, ordinal),
    CHECK (action IN
    ('record_only','create_opportunity','attach_to_open_opportunity')),
    CHECK (action = 'record_only' OR pipeline_id IS NOT NULL),
    FOREIGN KEY(connection_id) REFERENCES leads.intake_connection (id),
    FOREIGN KEY(pipeline_id) REFERENCES leads.pipeline (id)
)""",
    """CREATE TABLE leads.routing_condition (
    rule_id UUID NOT NULL,
    ordinal INTEGER NOT NULL,
    field TEXT NOT NULL,
    op TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (rule_id, ordinal),
    CHECK (op IN ('present','absent','equals','contains')),
    FOREIGN KEY(rule_id) REFERENCES leads.routing_rule (id)
)""",
    """CREATE TABLE leads.contact_permission (
    id UUID NOT NULL,
    party_ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    channel TEXT,
    recipient_scope TEXT NOT NULL,
    disclosure_version TEXT,
    evidence_observation_id UUID,
    granted_at TIMESTAMP WITH TIME ZONE,
    granted_by_kind TEXT,
    granted_by_id UUID,
    withdrawn_at TIMESTAMP WITH TIME ZONE,
    withdrawn_reason TEXT,
    withdrawn_by_kind TEXT,
    withdrawn_by_id UUID,
    PRIMARY KEY (id),
    FOREIGN KEY(evidence_observation_id) REFERENCES leads.observation (id)
)""",
    """CREATE TABLE leads.opportunity (
    id UUID NOT NULL,
    pipeline_id UUID NOT NULL,
    preset_id UUID NOT NULL,
    preset_version INTEGER NOT NULL,
    stage_id TEXT NOT NULL,
    title TEXT NOT NULL,
    owner_id UUID NOT NULL,
    funnel_id UUID NOT NULL,
    campaign_id UUID,
    source_observation_id UUID,
    value_amount NUMERIC,
    value_currency TEXT,
    value_basis TEXT,
    disposition_outcome TEXT,
    disposition_at TIMESTAMP WITH TIME ZONE,
    revision INTEGER NOT NULL,
    created_by_kind TEXT NOT NULL,
    created_by_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(pipeline_id, preset_id) REFERENCES leads.pipeline (id, preset_id),
        FOREIGN KEY(preset_id, preset_version, stage_id) REFERENCES leads.preset_stage
(preset_id, version, stage_id),
    FOREIGN KEY(campaign_id, funnel_id) REFERENCES leads.campaign (id, funnel_id),
    CHECK (revision > 0),
    FOREIGN KEY(funnel_id) REFERENCES leads.funnel (id),
    FOREIGN KEY(source_observation_id) REFERENCES leads.observation (id)
)""",
    """CREATE TABLE leads.draft (
    id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    template_version INTEGER,
    runtime_request_id UUID,
    created_by_kind TEXT NOT NULL,
    created_by_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE
)""",
    """CREATE TABLE leads.handoff (
    id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    purpose TEXT NOT NULL,
    destination_ref TEXT,
    idempotency_key TEXT NOT NULL,
    snapshot_digest BYTEA NOT NULL,
    state TEXT NOT NULL,
    result_ref TEXT,
    result_detail TEXT,
    approval_id UUID,
    external_action_id UUID,
    requested_by_kind TEXT NOT NULL,
    requested_by_id UUID,
    requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
    CONSTRAINT handoff_request_identity UNIQUE (opportunity_id, idempotency_key)
)""",
    """CREATE TABLE leads.opportunity_field_state (
    opportunity_id UUID NOT NULL,
    target TEXT NOT NULL,
    value_kind TEXT NOT NULL,
    value_text TEXT,
    owner TEXT NOT NULL,
    observation_id UUID,
    occurred_at TIMESTAMP WITH TIME ZONE,
    completeness INTEGER,
    priority INTEGER,
    orphaned BOOLEAN NOT NULL,
    PRIMARY KEY (opportunity_id, target),
    CHECK (owner IN ('source','user')),
    CHECK (value_kind IN ('value','cleared')),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
    FOREIGN KEY(observation_id) REFERENCES leads.observation (id)
)""",
    """CREATE TABLE leads.opportunity_note (
    id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    body TEXT NOT NULL,
    stage_id TEXT,
    created_by_kind TEXT NOT NULL,
    created_by_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE
)""",
    """CREATE TABLE leads.opportunity_observation (
    opportunity_id UUID NOT NULL,
    observation_id UUID NOT NULL,
    decision TEXT NOT NULL,
    decided_by_kind TEXT NOT NULL,
    decided_by_id UUID,
    decided_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (opportunity_id, observation_id),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE,
    FOREIGN KEY(observation_id) REFERENCES leads.observation (id)
)""",
    """CREATE TABLE leads.opportunity_party (
    opportunity_id UUID NOT NULL,
    party_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    added_at TIMESTAMP WITH TIME ZONE NOT NULL,
    added_by_kind TEXT NOT NULL,
    added_by_id UUID,
    removed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (opportunity_id, party_ref, role),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE
)""",
    """CREATE TABLE leads.qualification (
    id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    preset_version INTEGER NOT NULL,
    rubric_slug TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    objective TEXT NOT NULL,
    evidence_refs TEXT[] NOT NULL,
    input_revision INTEGER NOT NULL,
    fit TEXT NOT NULL,
    intent TEXT NOT NULL,
    evidence_completeness TEXT NOT NULL,
    urgency TEXT NOT NULL,
    explanation TEXT NOT NULL,
    uncertainty TEXT NOT NULL,
    model_id TEXT,
    prompt_version TEXT,
    created_by_kind TEXT NOT NULL,
    created_by_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(opportunity_id) REFERENCES leads.opportunity (id) ON DELETE CASCADE
)""",
    """CREATE TABLE leads.handoff_snapshot (
    handoff_id UUID NOT NULL,
    body JSONB NOT NULL,
    byte_length INTEGER NOT NULL,
    PRIMARY KEY (handoff_id),
    FOREIGN KEY(handoff_id) REFERENCES leads.handoff (id) ON DELETE CASCADE
)""",
    """INSERT INTO leads.pipeline_preset VALUES
('019bf0b0-0000-7000-8000-000000000001', 'Inbound services', 1, CURRENT_TIMESTAMP)""",
    """INSERT INTO leads.preset_version VALUES
('019bf0b0-0000-7000-8000-000000000001', 1, CURRENT_TIMESTAMP, NULL, 'Package
preset')""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'triage','Triage',0,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'discovery','Discovery',1,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'qualified','Qualified',2,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'proposal','Proposal',3,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'decision','Decision',4,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'nurture','Nurture',5,'open',NULL)""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'won','Won',6,'terminal','succeeded')""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'lost','Lost',7,'terminal','lost')""",
    """INSERT INTO leads.preset_stage VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'disqualified','Disqualified',8,'terminal','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'decision','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'decision','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'decision','nurture')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'decision','won')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'discovery','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'discovery','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'discovery','nurture')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'discovery','qualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'nurture','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'nurture','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'nurture','triage')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'proposal','decision')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'proposal','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'proposal','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'proposal','nurture')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'qualified','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'qualified','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'qualified','nurture')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'qualified','proposal')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'triage','discovery')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'triage','disqualified')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'triage','lost')""",
    """INSERT INTO leads.preset_transition VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'triage','nurture')""",
    """INSERT INTO leads.preset_rubric VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'inbound_services',1)""",
    """INSERT INTO leads.preset_template VALUES
('019bf0b0-0000-7000-8000-000000000001',1,'followup','Thanks for your inquiry.')""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
