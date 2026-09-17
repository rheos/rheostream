"""DDL for the export records introduced by core revision 0005."""

from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

EXPORT_KINDS: Final = ("export", "restore")
EXPORT_STATES: Final = ("in_progress", "complete", "failed", "removed_by_deletion")

export_metadata = MetaData(schema=CORE_SCHEMA)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


export_record = Table(
    "export_record",
    export_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("artifact_path", Text, nullable=True),
    Column("source_digest", LargeBinary, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # An operator context has no account id, so this cannot be a required FK.
    Column("created_by_id", UUID(as_uuid=True), nullable=True),
    Column("state", Text, nullable=False),
    Column("removed_at", DateTime(timezone=True), nullable=True),
    # Phase two owns core.deletion_record. This is intentionally a bare column.
    Column("deletion_record_id", UUID(as_uuid=True), nullable=True),
    Column("byte_length", BigInteger, nullable=True),
    CheckConstraint(_in("kind", EXPORT_KINDS), name="export_record_kind"),
    CheckConstraint(_in("state", EXPORT_STATES), name="export_record_state"),
)

export_record_ref = Table(
    "export_record_ref",
    export_metadata,
    Column(
        "export_id",
        UUID(as_uuid=True),
        ForeignKey("core.export_record.id"),
        nullable=False,
    ),
    Column("record_ref", Text, nullable=False),
    PrimaryKeyConstraint("export_id", "record_ref", name="export_record_ref_pkey"),
)
