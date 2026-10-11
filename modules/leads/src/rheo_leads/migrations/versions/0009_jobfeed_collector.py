"""Frozen: the job feed collector's tables and the ``poll`` intake transport."""

from alembic import op

revision = "0009_jobfeed_collector"
down_revision = "0008_attach_by_field"
branch_labels = None
depends_on = None
# Widens one CHECK and creates four empty owned tables; touches no existing record.
non_destructive_upgrade = True

AUTH_METHODS = "('none','client_secret_basic','client_secret_post')"
ENDPOINTS = """
        resource_url TEXT NOT NULL,
        issuer TEXT NOT NULL,
        authorization_endpoint TEXT NOT NULL,
        token_endpoint TEXT NOT NULL,
        registration_endpoint TEXT NOT NULL,
        revocation_endpoint TEXT,"""


def upgrade() -> None:
    # Find the transport CHECK by its definition, not a guessed name; only it
    # mentions 'webhook'. Earlier values keep their meaning.
    op.execute("""DO $$
DECLARE name TEXT;
BEGIN
    SELECT conname INTO STRICT name FROM pg_constraint
    WHERE conrelid = 'leads.intake_connection'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%''webhook''%';
    EXECUTE format('ALTER TABLE leads.intake_connection DROP CONSTRAINT %I', name);
END $$""")
    op.execute("""ALTER TABLE leads.intake_connection
    ADD CONSTRAINT connection_transport
    CHECK (transport IN ('webhook','import','manual','poll'))""")
    op.execute(f"""CREATE TABLE leads.jobfeed_connection (
        connection_id UUID PRIMARY KEY REFERENCES leads.intake_connection(id),
        singleton BOOLEAN NOT NULL DEFAULT true UNIQUE,
        pipeline_id UUID NOT NULL REFERENCES leads.pipeline(id),
        authorization_state TEXT NOT NULL,
        token_ref TEXT,
        credential_generation INTEGER NOT NULL DEFAULT 0,{ENDPOINTS}
        client_id TEXT,
        client_auth_method TEXT,
        client_redirect_uri TEXT,
        client_secret_expires_at TIMESTAMP WITH TIME ZONE,
        client_secret_ref TEXT,
        registration_token_ref TEXT,
        organization_id TEXT,
        run_claimed_at TIMESTAMP WITH TIME ZONE,
        last_completed_at TIMESTAMP WITH TIME ZONE,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        not_before TIMESTAMP WITH TIME ZONE,
        fetched INTEGER NOT NULL DEFAULT 0,
        imported INTEGER NOT NULL DEFAULT 0,
        skipped_dup INTEGER NOT NULL DEFAULT 0,
        refreshed INTEGER NOT NULL DEFAULT 0,
        filtered INTEGER NOT NULL DEFAULT 0,
        skipped_untracked INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        detail_reads INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT jobfeed_connection_singleton_true CHECK (singleton),
        CONSTRAINT jobfeed_connection_authorization_state CHECK (authorization_state
            IN ('connected','authorization_required','revoked')),
        CONSTRAINT jobfeed_connection_client_auth_method
            CHECK (client_auth_method IN {AUTH_METHODS}),
        CONSTRAINT jobfeed_connection_client_complete CHECK (
            (client_id IS NULL) = (client_auth_method IS NULL)
            AND (client_id IS NULL) = (client_redirect_uri IS NULL))
    )""")
    # No foreign key on connection_id: a first connect allocates it before the
    # connection row exists.
    op.execute(f"""CREATE TABLE leads.jobfeed_authorization (
        id UUID PRIMARY KEY,
        singleton BOOLEAN NOT NULL UNIQUE,
        state_hash BYTEA NOT NULL UNIQUE,
        code_verifier TEXT NOT NULL,
        account_id UUID NOT NULL,
        connection_id UUID NOT NULL,
        pipeline_id UUID NOT NULL,
        mode TEXT NOT NULL,{ENDPOINTS}
        redirect_uri TEXT NOT NULL,
        client_id TEXT NOT NULL,
        client_auth_method TEXT NOT NULL,
        client_secret_expires_at TIMESTAMP WITH TIME ZONE,
        client_secret_ref TEXT,
        registration_token_ref TEXT,
        client_registered BOOLEAN NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT jobfeed_authorization_singleton_true CHECK (singleton),
        CONSTRAINT jobfeed_authorization_mode CHECK (mode IN ('connect','reconnect')),
        CONSTRAINT jobfeed_authorization_client_auth_method
            CHECK (client_auth_method IN {AUTH_METHODS})
    )""")
    op.execute("""CREATE TABLE leads.jobfeed_posting (
        connection_id UUID NOT NULL REFERENCES leads.intake_connection(id),
        posting_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        field_digests TEXT NOT NULL,
        last_event_id TEXT NOT NULL,
        first_delivered_at TIMESTAMP WITH TIME ZONE NOT NULL,
        last_delivered_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (connection_id, posting_id)
    )""")
    op.execute("""CREATE TABLE leads.jobfeed_account_choice (
        connection_id UUID NOT NULL REFERENCES leads.intake_connection(id),
        organization_id TEXT NOT NULL,
        label TEXT,
        ordinal INTEGER NOT NULL,
        seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (connection_id, organization_id)
    )""")


def downgrade() -> None:
    raise RuntimeError("restore a backup to reverse this data migration")
