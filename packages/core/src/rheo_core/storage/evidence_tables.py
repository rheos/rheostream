"""DDL for ``core.evidence_unit``, introduced by core revision ``0010_evidence_unit``.

Own ``MetaData(schema="core")``, not added to frozen ``core_tables.py:CORE_TABLES`` or
to ``runtime_tables.py``: an evidence unit is a turn of human conversation waiting to be
turned into memory, a sibling of the runtime tables rather than a new
``runtime_transcript.kind``. It is transient working state, like ``core.job``: never
exported, held for at most ``automatic_memory.max_pending_hours``.

Each vocabulary is a ``Final`` tuple checked in the database, so an insert outside it is
refused there and not only in code. ``purpose`` is built from ``ContextPurpose`` itself.
"""

from typing import Final

from rheo_contracts import ContextPurpose
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

EVIDENCE_PRODUCER_KINDS: Final = ("rheo_runtime",)
"""Frozen at revision ``0010_evidence_unit``'s set; do not edit this tuple.

0010 builds ``core.evidence_unit`` from this module via ``create_all``, so changing the
tuple would change what 0010 creates. A later producer (1a4b) is admitted through its
own migration plus a live-shape sibling object, the ``runtime_tables.py`` pattern
(``runtime_session_0006`` frozen, ``runtime_session`` live, revision 0009). The same
holds for ``EVIDENCE_PURPOSES`` below, derived from ``ContextPurpose``: a new purpose
needs its own migration, not an edit that changes what 0010 creates.
"""
EVIDENCE_AUDIENCE_KINDS: Final = ("workspace", "member")
EVIDENCE_PURPOSES: Final = tuple(purpose.value for purpose in ContextPurpose)
EVIDENCE_STATES: Final = ("pending", "settled", "gap")
NATIVE_KEY_MAX_LENGTH: Final = 256

evidence_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


evidence_unit = Table(
    "evidence_unit",
    evidence_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("producer_kind", Text, nullable=False),
    Column("authority_id", UUID(as_uuid=True), nullable=False),
    Column("native_key", Text, nullable=False),
    Column("speaker_account_id", UUID(as_uuid=True), nullable=False),
    Column("audience_kind", Text, nullable=False),
    Column("audience_id", UUID(as_uuid=True), nullable=True),
    Column("purpose", Text, nullable=False),
    Column("source_recorded_at", _timestamptz(), nullable=False),
    Column("source_expires_at", _timestamptz(), nullable=False),
    Column("body", Text, nullable=True),
    Column("extraction_attempts", Integer, nullable=False, server_default=text("0")),
    Column("retry_after", _timestamptz(), nullable=True),
    Column("state", Text, nullable=False),
    Column("outcome", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("settled_at", _timestamptz(), nullable=True),
    CheckConstraint(
        _in("producer_kind", EVIDENCE_PRODUCER_KINDS),
        name="evidence_unit_producer_kind",
    ),
    CheckConstraint(
        f"char_length(native_key) <= {NATIVE_KEY_MAX_LENGTH}",
        name="evidence_unit_native_key_length",
    ),
    CheckConstraint(
        _in("audience_kind", EVIDENCE_AUDIENCE_KINDS),
        name="evidence_unit_audience_kind",
    ),
    CheckConstraint(
        "(audience_kind = 'workspace') = (audience_id IS NULL)",
        name="evidence_unit_audience_pairing",
    ),
    CheckConstraint(_in("purpose", EVIDENCE_PURPOSES), name="evidence_unit_purpose"),
    CheckConstraint(_in("state", EVIDENCE_STATES), name="evidence_unit_state"),
    CheckConstraint(
        "(state = 'pending') = (body IS NOT NULL)",
        name="evidence_unit_body_pairing",
    ),
    CheckConstraint(
        "(state = 'pending') = (outcome IS NULL)",
        name="evidence_unit_outcome_pairing",
    ),
    # The unit identity, and the ON CONFLICT target the recorder names.
    Index(
        "evidence_unit_native_key",
        "producer_kind",
        "authority_id",
        "native_key",
        unique=True,
    ),
    # The claim order over claimable rows: ORDER BY extraction_attempts,
    # source_recorded_at WHERE state = 'pending'. Keep the two in step.
    Index(
        "evidence_unit_pending_claim",
        "extraction_attempts",
        "source_recorded_at",
        postgresql_where=text("state = 'pending'"),
    ),
)

EVIDENCE_TABLES: Final[tuple[Table, ...]] = (evidence_unit,)
"""The one evidence table, exactly what revision ``0010_evidence_unit`` creates."""
