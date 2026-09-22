"""DDL for ``core.deletion_record``, the content-free deletion ledger (core 0007).

**Nothing here can hold content from the deleted record**, and that is the table's
whole design rather than a property to test for afterwards
(``docs/architecture/deletion-export-migration.md`` § The deletion record). The record
is named by identifier only — a qualified record type and a uuid — the participants
are module ids, and the four outcome columns are counts. There is no title column, no
body column, no payload column, and no json column a caller could put one in.

``retained_successor_ref`` is the one text column that names something other than the
target, and it is constrained rather than trusted: a canonical record reference,
allowed only on a ``retention_expiry`` row. A ``user_erasure`` row carrying one would
be a pointer from an erasure to a replacement the erasure was supposed to remove, so
the check constraint refuses it in the database rather than in a reviewer's memory.

**``record_type`` holds the *qualified* type — ``harness.probe``, ``recallatron.
memory`` — not the bare second segment.** A reference is ``<module>.<record_type>:
<uuid>``, and the resolver that answers ``deleted`` for a reference from this table
has to rebuild the whole reference from the row; a bare ``memory`` would name a type
in no particular module and two modules could own the same one.
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

USER_ERASURE: Final = "user_erasure"
RETENTION_EXPIRY: Final = "retention_expiry"
DELETION_CAUSES: Final = (USER_ERASURE, RETENTION_EXPIRY)
"""The two causes a deletion can have.

``user_erasure`` is ``core.record.delete`` under a per-action approval.
``retention_expiry`` is the scheduled expiry of a superseded record, which is R5's one
permitted deletion without an approval — it has no writer in this prompt, and the
``approval_id`` column is nullable for it rather than for a caller's convenience.
"""

deletion_metadata = MetaData(schema=CORE_SCHEMA)
"""This revision's own ``MetaData``, so 0001-0006 still create exactly their own sets.

The same split ``exports/tables.py`` and ``storage/runtime_tables.py`` already use: a
frozen revision's ``create_all`` must not start creating a table a later revision
added to a shared collection.
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


deletion_record = Table(
    "deletion_record",
    deletion_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("deleted_at", DateTime(timezone=True), nullable=False),
    # The qualified ``<module>.<record_type>`` and the record's own id: what was
    # deleted, by identifier only. See the module docstring on the qualified form.
    Column("record_type", Text, nullable=False),
    Column("record_id", PG_UUID(as_uuid=True), nullable=False),
    # Who. Nullable id because an operator context has no account id, exactly as
    # ``export_record.created_by_id`` is nullable for the same reason.
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", PG_UUID(as_uuid=True), nullable=True),
    # The confirmation. Null only for a scheduled expiry, which has no approval to
    # name; every ``user_erasure`` row carries one.
    Column("approval_id", PG_UUID(as_uuid=True), nullable=True),
    # Module ids only. Not a foreign key and not a join: a module uninstalled after a
    # deletion does not make the ledger's account of what ran untrue.
    Column("participants", ARRAY(Text), nullable=False),
    Column("cancelled_job_count", Integer, nullable=False),
    Column("cancelled_action_count", Integer, nullable=False),
    Column("removed_export_count", Integer, nullable=False),
    Column("invalidated_memory_count", Integer, nullable=False),
    Column("cause", Text, nullable=False),
    Column("retained_successor_ref", Text, nullable=True),
    CheckConstraint(_in("cause", DELETION_CAUSES), name="deletion_record_cause"),
    CheckConstraint(
        f"retained_successor_ref IS NULL OR cause = '{RETENTION_EXPIRY}'",
        name="deletion_record_successor_is_expiry_only",
    ),
    CheckConstraint(
        "cancelled_job_count >= 0 AND cancelled_action_count >= 0 AND "
        "removed_export_count >= 0 AND invalidated_memory_count >= 0",
        name="deletion_record_counts_are_non_negative",
    ),
)
