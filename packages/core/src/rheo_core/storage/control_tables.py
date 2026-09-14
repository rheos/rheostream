"""The ten control-plane tables (A1), as SQLAlchemy Core ``Table`` objects.

Columns are verbatim from ``docs/architecture/storage-and-workspaces.md`` § The control
plane; the constraints are the ones that document and the run spec pin. Core, not ORM:
no declarative base and no mapped class, so the repositories in ``control_plane.py``
are the only place control-plane SQL lives and ``text(`` stays greppable. The
``control`` migration chain imports these objects rather than re-declaring the columns,
and its first revision creates all ten tables whole and is never edited afterwards.

The control plane's eleventh table, ``workspace_work_due``, is deliberately **not**
here: it is declared in ``work_index_tables.py`` under its own ``MetaData`` and created
by the chain's second revision, ``0002_work_index``. Adding it to this module would
silently change what that frozen first revision creates.

Six of the ten (``session``, ``session_secret``, ``session_grant``, ``access_token``,
``access_token_operation``, ``identity_provider``) are DDL-only at this run's merge
SHA: their repository functions arrive in 0b2 with their callers and tests.
"""

from enum import StrEnum
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

CONTROL_SCHEMA: Final = "control"
CONTROL_VERSION_TABLE: Final = "alembic_version_control"


class WorkspaceState(StrEnum):
    """The closed set of ``workspace.state`` values.

    The check constraint below is built from this enum, so the enum and the
    database cannot drift; ``control_plane.py`` reads and writes it by member.
    """

    PROVISIONING = "provisioning"
    MIGRATING = "migrating"
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    RESTORING = "restoring"


WORKSPACE_STATES: Final[tuple[str, ...]] = tuple(
    state.value for state in WorkspaceState
)
MEMBERSHIP_ROLES: Final = ("owner", "member")
TOKEN_KINDS: Final = ("cli", "mcp", "runtime")
TOKEN_ISSUERS: Final = ("session", "operator", "runtime")

control_metadata = MetaData(schema=CONTROL_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


account = Table(
    "account",
    control_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    Column("disabled_at", _timestamptz(), nullable=True),
)

identity = Table(
    "identity",
    control_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("account_id", UUID(as_uuid=True), ForeignKey(account.c.id), nullable=False),
    Column("provider_id", Text, nullable=False),
    Column("provider_subject", Text, nullable=False),
    Column("email", Text, nullable=True),
    Column("email_verified", Boolean, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    UniqueConstraint(
        "provider_id", "provider_subject", name="identity_provider_subject"
    ),
)

workspace = Table(
    "workspace",
    control_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("database_name", Text, nullable=False, unique=True),
    Column("state", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    Column("state_changed_at", _timestamptz(), nullable=False),
    Column("state_detail", Text, nullable=True),
    CheckConstraint(_in("state", WORKSPACE_STATES), name="workspace_state"),
)

membership = Table(
    "membership",
    control_metadata,
    Column("account_id", UUID(as_uuid=True), ForeignKey(account.c.id), nullable=False),
    Column(
        "workspace_id", UUID(as_uuid=True), ForeignKey(workspace.c.id), nullable=False
    ),
    Column("role", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    PrimaryKeyConstraint("account_id", "workspace_id", name="membership_pkey"),
    CheckConstraint(_in("role", MEMBERSHIP_ROLES), name="membership_role"),
)

session = Table(
    "session",
    control_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("account_id", UUID(as_uuid=True), ForeignKey(account.c.id), nullable=False),
    Column(
        "active_workspace_id",
        UUID(as_uuid=True),
        ForeignKey(workspace.c.id),
        nullable=True,
    ),
    Column("active_workspace_changed_at", _timestamptz(), nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("last_seen_at", _timestamptz(), nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
    Column("revoked_at", _timestamptz(), nullable=True),
)

session_secret = Table(
    "session_secret",
    control_metadata,
    Column("secret_hash", LargeBinary, primary_key=True),
    Column("session_id", UUID(as_uuid=True), ForeignKey(session.c.id), nullable=False),
    Column("host", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    UniqueConstraint("session_id", "host", name="session_secret_session_host"),
)

session_grant = Table(
    "session_grant",
    control_metadata,
    Column("code_hash", LargeBinary, primary_key=True),
    Column("session_id", UUID(as_uuid=True), ForeignKey(session.c.id), nullable=False),
    Column("target_host", Text, nullable=False),
    Column("nonce_hash", LargeBinary, nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
    Column("used_at", _timestamptz(), nullable=True),
)

access_token = Table(
    "access_token",
    control_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("account_id", UUID(as_uuid=True), ForeignKey(account.c.id), nullable=False),
    Column(
        "workspace_id", UUID(as_uuid=True), ForeignKey(workspace.c.id), nullable=False
    ),
    Column("kind", Text, nullable=False),
    Column("issued_from", Text, nullable=False),
    Column("set_name", Text, nullable=True),
    Column("purpose", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
    Column("revoked_at", _timestamptz(), nullable=True),
    Column("last_used_at", _timestamptz(), nullable=True),
    CheckConstraint(_in("kind", TOKEN_KINDS), name="access_token_kind"),
    CheckConstraint(_in("issued_from", TOKEN_ISSUERS), name="access_token_issued_from"),
    CheckConstraint(
        "(kind = 'runtime') = (issued_from = 'runtime')",
        name="access_token_runtime_pairing",
    ),
)

access_token_operation = Table(
    "access_token_operation",
    control_metadata,
    Column(
        "token_id",
        UUID(as_uuid=True),
        ForeignKey(access_token.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("operation_name", Text, nullable=False),
    PrimaryKeyConstraint(
        "token_id", "operation_name", name="access_token_operation_pkey"
    ),
)

identity_provider = Table(
    "identity_provider",
    control_metadata,
    Column("provider_id", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("client_id", Text, nullable=False),
    Column("client_secret_ref", Text, nullable=False),
)

CONTROL_TABLES: Final[tuple[Table, ...]] = (
    account,
    identity,
    workspace,
    membership,
    session,
    session_secret,
    session_grant,
    access_token,
    access_token_operation,
    identity_provider,
)
