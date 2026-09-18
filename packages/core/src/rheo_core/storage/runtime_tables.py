"""DDL for the four runtime tables introduced by core revision 0006.

Own ``MetaData(schema="core")``, not added to frozen ``core_tables.py:CORE_TABLES``.
Column lists are verbatim from ``docs/architecture/runtime-and-mcp.md`` § What the
core records and § Native session handles, with ``runtime_session.native_handle``
required (``Text, nullable=False``).
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

TRANSCRIPT_KINDS: Final = ("model_output", "tool_result_summary", "progress")

runtime_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


runtime_request = Table(
    "runtime_request",
    runtime_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operation_id", UUID(as_uuid=True), nullable=False),
    Column("runtime_id", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("context_digest", LargeBinary, nullable=False),
    Column("permitted_tools", ARRAY(Text), nullable=False),
    Column("requirements", ARRAY(Text), nullable=False),
    Column("deadline_seconds", Integer, nullable=False),
    Column("max_iterations", Integer, nullable=False),
    Column("max_output_bytes", Integer, nullable=False),
    Column("terminal_event", Text, nullable=True),
    Column("failure_kind", Text, nullable=True),
    Column("usage_input_tokens", Integer, nullable=True),
    Column("usage_output_tokens", Integer, nullable=True),
    Column("usage_cost", Float, nullable=True),
    Column("usage_kind", Text, nullable=True),
    Column("started_at", _timestamptz(), nullable=False),
    Column("ended_at", _timestamptz(), nullable=True),
)

runtime_request_context = Table(
    "runtime_request_context",
    runtime_metadata,
    Column(
        "request_id",
        UUID(as_uuid=True),
        ForeignKey("core.runtime_request.id"),
        nullable=False,
    ),
    Column("ref", Text, nullable=False),
    PrimaryKeyConstraint("request_id", "ref", name="runtime_request_context_pkey"),
)

runtime_transcript = Table(
    "runtime_transcript",
    runtime_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "request_id",
        UUID(as_uuid=True),
        ForeignKey("core.runtime_request.id"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("recorded_at", _timestamptz(), nullable=False),
    Column("retention_until", _timestamptz(), nullable=False),
    CheckConstraint(_in("kind", TRANSCRIPT_KINDS), name="runtime_transcript_kind"),
)

runtime_session = Table(
    "runtime_session",
    runtime_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("runtime_id", Text, nullable=False),
    Column("credential_scope", Text, nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=False),
    Column("audience_kind", Text, nullable=False),
    Column("audience_id", UUID(as_uuid=True), nullable=False),
    Column("native_handle", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    Column("last_used_at", _timestamptz(), nullable=False),
    Column("expires_at", _timestamptz(), nullable=False),
)

RUNTIME_TABLES: Final[tuple[Table, ...]] = (
    runtime_request,
    runtime_request_context,
    runtime_transcript,
    runtime_session,
)
"""The four tables revision ``0006_runtime`` creates, in dependency order."""
