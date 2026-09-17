"""``core.standing_grant`` and ``core.standing_grant_operation``, as SQLAlchemy Core
``Table`` objects in schema ``core``.

Columns are verbatim from ``docs/architecture/confirmation-and-safety.md`` § Standing
grants.

**A fourth ``MetaData``, for the reason ``approvals/tables.py`` gives for the third.**
``0001_core_schema``, ``0002_durable_work`` and ``0003_approvals`` each call their own
metadata's ``create_all(checkfirst=False)`` and each states it is never edited again.
Attaching these two tables to any of those objects would make a frozen revision create
them by side effect on a fresh database. One ``MetaData`` per revision keeps every
``create_all`` exact: ``0004_standing_grants`` creates exactly the two below.

**Schema only.** This module defines DDL and executes none of it; the lifecycle over
this shape is ``approvals/grants.py`` (the repository) and
``approvals/grant_operations.py`` (``core.standing_grant.create`` / ``.revoke``).
"""

from typing import Final

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

grant_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


standing_grant = Table(
    "standing_grant",
    grant_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=False),
    Column("granted_by_id", UUID(as_uuid=True), nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
    Column("revoked_at", _timestamptz(), nullable=True),
)

# "Primary key both columns" (§ Standing grants), which is also what makes a repeated
# operation name in one grant's list a database error rather than a duplicate row; the
# handler de-duplicates before the insert so a caller never meets that error.
#
# The foreign key is inside this metadata, unlike ``approval_payload``'s cross-metadata
# one, so ``create_all`` orders the two tables itself.
standing_grant_operation = Table(
    "standing_grant_operation",
    grant_metadata,
    Column(
        "grant_id",
        UUID(as_uuid=True),
        ForeignKey("core.standing_grant.id"),
        nullable=False,
    ),
    Column("operation_name", Text, nullable=False),
    PrimaryKeyConstraint(
        "grant_id", "operation_name", name="standing_grant_operation_pkey"
    ),
)

GRANT_TABLES: Final[tuple[Table, ...]] = (standing_grant, standing_grant_operation)
"""The two tables revision ``0004_standing_grants`` creates, in dependency order."""
