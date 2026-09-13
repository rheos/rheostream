"""The spike's two tables, in schema ``spike``, and the narrow read/write helpers
the handlers, the resolver and the audit sink use.

Both tables are created by the module's own ``create_schema`` — through a
``UnitOfWork`` connection, never by a shipped migration chain — exactly as
``tests/harness/records.py`` already does for ``harness.note``. Nothing under
``rheo_core`` knows either table exists.

**``spike.audit_probe`` is not ``core.audit_record``**, and the difference is the
point of the 0c boundary rather than a naming accident: it is in the module's own
schema, it is created by the module's own install step, and it carries only the
columns needed to prove *who wrote the row* — the operation name, the subject the
declaration's ``AuditSpec`` named, the actor, and the request. 0c2 builds the real
table, with the real columns, in ``core``.

``note.revision`` is written once, as ``1``, and never updated: this run ships no
mutating operation on a note, so the column stands for the ratified shape without
pretending to implement optimistic concurrency (run 0v's finding **F7**).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_core.refs import uuid7
from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

SPIKE_SCHEMA: Final = "spike"
NOTE_TABLE: Final = "note"
AUDIT_PROBE_TABLE: Final = "audit_probe"

spike_metadata = MetaData(schema=SPIKE_SCHEMA)

note = Table(
    NOTE_TABLE,
    spike_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revision", Integer, nullable=False, server_default=text("1")),
)

audit_probe = Table(
    AUDIT_PROBE_TABLE,
    spike_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("operation", Text, nullable=False),
    Column("subject_ref", Text, nullable=True),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", PG_UUID(as_uuid=True), nullable=True),
    Column("request_id", PG_UUID(as_uuid=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class NoteRow:
    id: UUID
    body: str
    created_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class AuditProbeRow:
    id: UUID
    operation: str
    subject_ref: str | None
    actor_kind: str
    actor_id: UUID | None
    request_id: UUID
    recorded_at: datetime


def create_schema(connection: Connection) -> None:
    """Create schema ``spike`` and both tables in the connection's database.

    Idempotent (``CREATE SCHEMA IF NOT EXISTS`` plus ``checkfirst``), because the
    install command is idempotent and a test fixture calls it once per workspace it
    wants the module in.
    """
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SPIKE_SCHEMA}"))
    note.create(connection, checkfirst=True)
    audit_probe.create(connection, checkfirst=True)


def has_note_table(connection: Connection) -> bool:
    """Whether this database has ``spike.note`` at all.

    A workspace the module was never installed into has no schema, and the resolver
    must answer ``not_found`` for it rather than raising — which is also what a
    reference minted in another workspace gets.
    """
    return bool(inspect(connection).has_table(NOTE_TABLE, schema=SPIKE_SCHEMA))


def write_note(connection: Connection, *, body: str) -> NoteRow:
    row = NoteRow(uuid7(), body, datetime.now(UTC), 1)
    connection.execute(
        insert(note).values(
            id=row.id,
            body=row.body,
            created_at=row.created_at,
            revision=row.revision,
        )
    )
    return row


def get_note(connection: Connection, note_id: UUID) -> NoteRow | None:
    found = connection.execute(select(note).where(note.c.id == note_id)).mappings()
    row = found.first()
    if row is None:
        return None
    return NoteRow(
        id=row["id"],
        body=row["body"],
        created_at=row["created_at"],
        revision=row["revision"],
    )


def list_notes(connection: Connection, *, limit: int) -> tuple[NoteRow, ...]:
    """The newest ``limit`` notes in the connection's own database.

    ``id`` is a UUIDv7, so ordering by it is ordering by mint time; ``created_at``
    leads the key anyway so the ordering reads as what it means.
    """
    rows = connection.execute(
        select(note).order_by(note.c.created_at.desc(), note.c.id.desc()).limit(limit)
    ).mappings()
    return tuple(
        NoteRow(
            id=row["id"],
            body=row["body"],
            created_at=row["created_at"],
            revision=row["revision"],
        )
        for row in rows
    )


def write_audit_probe(
    connection: Connection,
    *,
    operation: str,
    subject_ref: str | None,
    actor_kind: str,
    actor_id: UUID | None,
    request_id: UUID,
) -> AuditProbeRow:
    row = AuditProbeRow(
        id=uuid7(),
        operation=operation,
        subject_ref=subject_ref,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=request_id,
        recorded_at=datetime.now(UTC),
    )
    connection.execute(
        insert(audit_probe).values(
            id=row.id,
            operation=row.operation,
            subject_ref=row.subject_ref,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            request_id=row.request_id,
            recorded_at=row.recorded_at,
        )
    )
    return row


def list_audit_probes(connection: Connection) -> tuple[AuditProbeRow, ...]:
    rows = connection.execute(
        select(audit_probe).order_by(audit_probe.c.recorded_at, audit_probe.c.id)
    ).mappings()
    return tuple(
        AuditProbeRow(
            id=row["id"],
            operation=row["operation"],
            subject_ref=row["subject_ref"],
            actor_kind=row["actor_kind"],
            actor_id=row["actor_id"],
            request_id=row["request_id"],
            recorded_at=row["recorded_at"],
        )
        for row in rows
    )
