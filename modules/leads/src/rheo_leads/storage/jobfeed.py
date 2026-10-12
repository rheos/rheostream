"""The job feed collector's tables. Every queryable value is a column; none is JSON.

``jobfeed_connection`` and ``jobfeed_authorization`` hold at most one row each per
workspace. ``token_ref``, ``client_secret_ref`` and ``registration_token_ref`` are
secret-store references, never secret values. The pending authorization's
``connection_id`` has no foreign key: a first connect allocates it before the
connection row exists.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Table,
    Text,
    Uuid,
)

from rheo_leads.storage.tables import metadata

AUTH_METHODS = "('none','client_secret_basic','client_secret_post')"
REPORT_COUNTS = (
    "fetched",
    "imported",
    "skipped_dup",
    "refreshed",
    "filtered",
    "skipped_untracked",
    "errors",
    "detail_reads",
    "pages",
)


def _text(name: str, *, nullable: bool = False) -> Column:  # type: ignore[type-arg]
    return Column(name, Text, nullable=nullable)


def _time(name: str, *, nullable: bool = False) -> Column:  # type: ignore[type-arg]
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _count(name: str) -> Column:  # type: ignore[type-arg]
    return Column(name, Integer, nullable=False, server_default="0")


def _endpoints() -> list[Column]:  # type: ignore[type-arg]
    return [
        *(
            _text(name)
            for name in (
                "resource_url",
                "issuer",
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
            )
        ),
        _text("revocation_endpoint", nullable=True),
    ]


jobfeed_connection = Table(
    "jobfeed_connection",
    metadata,
    Column(
        "connection_id",
        Uuid,
        ForeignKey("leads.intake_connection.id"),
        primary_key=True,
    ),
    Column("singleton", Boolean, nullable=False, server_default="true", unique=True),
    Column("pipeline_id", Uuid, ForeignKey("leads.pipeline.id"), nullable=False),
    _text("authorization_state"),
    _text("token_ref", nullable=True),
    _count("credential_generation"),
    *_endpoints(),
    _text("client_id", nullable=True),
    _text("client_auth_method", nullable=True),
    _text("client_redirect_uri", nullable=True),
    _time("client_secret_expires_at", nullable=True),
    _text("client_secret_ref", nullable=True),
    _text("registration_token_ref", nullable=True),
    _text("organization_id", nullable=True),
    _time("run_claimed_at", nullable=True),
    _time("last_completed_at", nullable=True),
    _count("consecutive_failures"),
    _time("not_before", nullable=True),
    *(_count(name) for name in REPORT_COUNTS),
    _time("created_at"),
    _time("updated_at"),
    CheckConstraint("singleton", name="jobfeed_connection_singleton_true"),
    CheckConstraint(
        "authorization_state IN ('connected','authorization_required','revoked')",
        name="jobfeed_connection_authorization_state",
    ),
    CheckConstraint(
        f"client_auth_method IN {AUTH_METHODS}",
        name="jobfeed_connection_client_auth_method",
    ),
    CheckConstraint(
        "(client_id IS NULL) = (client_auth_method IS NULL) "
        "AND (client_id IS NULL) = (client_redirect_uri IS NULL)",
        name="jobfeed_connection_client_complete",
    ),
)
jobfeed_authorization = Table(
    "jobfeed_authorization",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("singleton", Boolean, nullable=False, unique=True),
    Column("state_hash", LargeBinary, nullable=False, unique=True),
    _text("code_verifier"),
    Column("account_id", Uuid, nullable=False),
    Column("connection_id", Uuid, nullable=False),
    Column("pipeline_id", Uuid, nullable=False),
    _text("mode"),
    *_endpoints(),
    _text("redirect_uri"),
    _text("client_id"),
    _text("client_auth_method"),
    _time("client_secret_expires_at", nullable=True),
    _text("client_secret_ref", nullable=True),
    _text("registration_token_ref", nullable=True),
    Column("client_registered", Boolean, nullable=False),
    _time("created_at"),
    _time("expires_at"),
    CheckConstraint("singleton", name="jobfeed_authorization_singleton_true"),
    CheckConstraint(
        "mode IN ('connect','reconnect')", name="jobfeed_authorization_mode"
    ),
    CheckConstraint(
        f"client_auth_method IN {AUTH_METHODS}",
        name="jobfeed_authorization_client_auth_method",
    ),
)
jobfeed_posting = Table(
    "jobfeed_posting",
    metadata,
    Column(
        "connection_id",
        Uuid,
        ForeignKey("leads.intake_connection.id"),
        primary_key=True,
    ),
    Column("posting_id", Text, primary_key=True),
    Column("seq", Integer, nullable=False),
    _text("field_digests"),
    _text("last_event_id"),
    _time("first_delivered_at"),
    _time("last_delivered_at"),
)
jobfeed_account_choice = Table(
    "jobfeed_account_choice",
    metadata,
    Column(
        "connection_id",
        Uuid,
        ForeignKey("leads.intake_connection.id"),
        primary_key=True,
    ),
    Column("organization_id", Text, primary_key=True),
    _text("label", nullable=True),
    Column("ordinal", Integer, nullable=False),
    _time("seen_at"),
)
TABLES = (
    jobfeed_connection,
    jobfeed_authorization,
    jobfeed_posting,
    jobfeed_account_choice,
)
