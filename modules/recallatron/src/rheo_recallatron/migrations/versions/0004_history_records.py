"""Owner-only searchable predecessor history, distinct from curated memory.

Revision ID: 0004_history_records
Revises: 0003_dense_retrieval
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Index,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

revision: str = "0004_history_records"
down_revision: str | None = "0003_dense_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen at this revision: runtime table evolution must never rewrite old DDL.
history_metadata = MetaData(schema="recallatron")
Table(
    "history_record",
    history_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("source_namespace", Text, nullable=False),
    Column("external_source_key", Text, nullable=False),
    Column("source_reference", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column(
        "search_tsv",
        TSVECTOR,
        Computed(
            "to_tsvector('english'::regconfig, title || ' ' || body)", persisted=True
        ),
        nullable=False,
    ),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    Column("session_key", Text, nullable=True),
    Column("chat_key", Text, nullable=True),
    Column("source_role", Text, nullable=True),
    Column("source_category", Text, nullable=True),
    Column("source_created_at", DateTime(timezone=True), nullable=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("superseded_by_source_key", Text, nullable=True),
    Column("erased_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "kind IN ('conversation', 'procedural_note', 'session_digest', 'memory_item', "
        "'graph_entity', 'graph_event', 'profile_item')",
        name="history_record_kind",
    ),
    CheckConstraint(
        "external_source_key ~ '^sha256:[0-9a-f]{64}$'",
        name="history_record_opaque_source_key",
    ),
    CheckConstraint(
        "status IN ('turn', 'unconfirmed', 'confirmed', 'superseded', 'current', "
        "'conflicted', 'summary', 'event')",
        name="history_record_status",
    ),
    CheckConstraint(
        "erased_at IS NULL OR (title = '' AND body = '' AND session_key IS NULL "
        "AND chat_key IS NULL AND source_role IS NULL AND source_category IS NULL "
        "AND source_reference IS NULL AND source_created_at IS NULL "
        "AND confirmed_at IS NULL "
        "AND superseded_by_source_key IS NULL)",
        name="history_record_erased_content",
    ),
    Index(
        "history_record_source_key",
        "source_namespace",
        "external_source_key",
        unique=True,
    ),
    Index("history_record_search_tsv_gin", "search_tsv", postgresql_using="gin"),
    Index("history_record_time_id", "occurred_at", "id"),
)


def upgrade() -> None:
    history_metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
