"""The control-plane OAuth tables (issue #287), as SQLAlchemy Core ``Table`` objects in
schema ``control``.

**Own ``MetaData``, as ``work_index_tables.py`` explains.** Revision ``0001`` creates
exactly its ten tables and ``0002`` exactly its one; ``0003_oauth`` creates exactly the
six below from ``oauth_metadata``. Attaching them to ``control_metadata`` would change
what a frozen revision creates.

**A grant is one ``access_token`` row.** ``oauth_grant`` and ``oauth_refresh_token``
key on that row's id; the access token value is rotated in place on refresh.

**Delete rules are the backstop.** ``RESTRICT`` on ``oauth_grant.token_id``,
``oauth_grant.client_id`` and ``oauth_refresh_token.token_id``: a cleanup that ever
selected a client with a grant, or a ``delete_access_token`` on a connector row, fails
its transaction instead of erasing the grant. ``CASCADE`` runs only from
``oauth_client`` to its redirect URIs and authorization rows, which matters only for an
abandoned client (one that never had a grant). ``oauth_event`` carries no foreign key
at all, so the audit trail outlives client cleanup and never blocks a delete.

Secrets (request nonce, code, refresh token) are stored as SHA-256 only.
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.control_tables import (
    CONTROL_SCHEMA,
    TOKEN_ISSUERS,
    access_token,
    account,
    workspace,
)

# The frozen 0001 tuple plus the connector issuer. ``0003_oauth`` rebuilds the
# ``access_token_issued_from`` CHECK from this; ``control_tables.TOKEN_ISSUERS`` stays
# what 0001 created.
TOKEN_ISSUERS_0003: Final[tuple[str, ...]] = (*TOKEN_ISSUERS, "connector")

OAUTH_EVENTS: Final[tuple[str, ...]] = (
    "client_registered",
    "authorization_granted",
    "authorization_denied",
    "code_redeemed",
    "refresh_used",
    "refresh_reuse_revoked",
    "grant_revoked",
)
OAUTH_OUTCOMES: Final[tuple[str, ...]] = ("succeeded", "refused")

oauth_metadata = MetaData(schema=CONTROL_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def in_check(column: str, values: tuple[str, ...]) -> str:
    """``<column> IN ('a', 'b', ...)``, the CHECK text shape ``control_tables`` uses."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


oauth_client = Table(
    "oauth_client",
    oauth_metadata,
    Column("client_id", Text, primary_key=True),
    Column("client_name", Text, nullable=False),
    Column("registered_from", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
)

oauth_client_redirect_uri = Table(
    "oauth_client_redirect_uri",
    oauth_metadata,
    Column(
        "client_id",
        Text,
        ForeignKey(oauth_client.c.client_id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("redirect_uri", Text, nullable=False),
    PrimaryKeyConstraint(
        "client_id", "redirect_uri", name="oauth_client_redirect_uri_pkey"
    ),
)

oauth_authorization = Table(
    "oauth_authorization",
    oauth_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("request_hash", LargeBinary, nullable=False, unique=True),
    Column(
        "client_id",
        Text,
        ForeignKey(oauth_client.c.client_id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("redirect_uri", Text, nullable=False),
    Column("code_challenge", Text, nullable=False),
    Column("state", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
    Column("account_id", UUID(as_uuid=True), ForeignKey(account.c.id), nullable=True),
    Column(
        "workspace_id", UUID(as_uuid=True), ForeignKey(workspace.c.id), nullable=True
    ),
    Column("decided_at", _timestamptz(), nullable=True),
    Column("code_hash", LargeBinary, nullable=True, unique=True),
    Column("code_expires_at", _timestamptz(), nullable=True),
    Column("code_used_at", _timestamptz(), nullable=True),
    # NO ACTION: set at redemption so a replay can revoke what the code issued.
    Column(
        "token_id", UUID(as_uuid=True), ForeignKey(access_token.c.id), nullable=True
    ),
)

oauth_grant = Table(
    "oauth_grant",
    oauth_metadata,
    Column(
        "token_id",
        UUID(as_uuid=True),
        ForeignKey(access_token.c.id, ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "client_id",
        Text,
        ForeignKey(oauth_client.c.client_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", _timestamptz(), nullable=False),
    Column("grant_expires_at", _timestamptz(), nullable=False),
)

oauth_refresh_token = Table(
    "oauth_refresh_token",
    oauth_metadata,
    Column("token_hash", LargeBinary, primary_key=True),
    Column(
        "token_id",
        UUID(as_uuid=True),
        ForeignKey(oauth_grant.c.token_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", _timestamptz(), nullable=False),
    Column("rotated_at", _timestamptz(), nullable=True),
    Index("oauth_refresh_token_token_id", "token_id"),
)

# Content-free by design: no secret, no client-supplied text, no URL. No index beyond
# the PK: the only read is newest-first with a small LIMIT over single-owner volume.
oauth_event = Table(
    "oauth_event",
    oauth_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("occurred_at", _timestamptz(), nullable=False),
    Column("event", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("client_id", Text, nullable=True),
    Column("account_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=True),
    Column("token_id", UUID(as_uuid=True), nullable=True),
    CheckConstraint(in_check("event", OAUTH_EVENTS), name="oauth_event_event"),
    CheckConstraint(in_check("outcome", OAUTH_OUTCOMES), name="oauth_event_outcome"),
)
