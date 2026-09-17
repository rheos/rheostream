"""``core.approval`` and ``core.approval_payload``, as SQLAlchemy Core ``Table``
objects in schema ``core``.

Columns are verbatim from ``docs/architecture/confirmation-and-safety.md`` § The
approval record, with three deviations recorded at their own columns below
(``operation_id`` carries no foreign key, ``purpose`` is nullable, and
``destination_ref`` has no writer in release one).

**Why a third ``MetaData`` and not two more tables in ``core_tables.py`` or
``work_tables.py``.** ``0001_core_schema`` and ``0002_durable_work`` each call their
own metadata's ``create_all(checkfirst=False)``, and both revisions state they are
never edited again. Attaching these two to either object would make a frozen revision
create them by side effect on a fresh database. One ``MetaData`` per revision keeps
every ``create_all`` exact: ``0003_approvals`` creates exactly the two below. That is
``work_tables.py``'s own reasoning, applied a second time rather than restated.

**Schema only.** This module defines DDL and executes none of it; the lifecycle over
this shape is ``approvals/records.py`` (the repository), ``approvals/gate.py`` (the
dispatcher's upper-class path) and ``approvals/operations.py``
(``core.approval.approve`` / ``.refuse``).
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

PENDING: Final = "pending"
APPROVED: Final = "approved"
EXECUTED: Final = "executed"
EXPIRED: Final = "expired"
INVALIDATED: Final = "invalidated"
REFUSED: Final = "refused"
REQUIRES_REAPPROVAL: Final = "requires_reapproval"

APPROVAL_STATES: Final = (
    PENDING,
    APPROVED,
    EXECUTED,
    EXPIRED,
    INVALIDATED,
    REFUSED,
    REQUIRES_REAPPROVAL,
)
"""The seven ratified states, declared as the check constraint's whole set even
though this chunk only ever writes the first four and ``refused``.

``expired`` is written by the window sweep a later run adds, ``invalidated`` by the
deletion cascade, and ``requires_reapproval`` by R4's restore. Declaring all seven
now costs nothing — the constraint is a list of strings — and the alternative is a
migration that widens a check constraint for each, which is three migrations to say
what one already knew."""

approval_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


# ``operation_id`` is a plain column with **no** foreign key, which is the mirror of
# the decision ``work_tables.operation.approval_id`` already records: that column has
# no foreign key because ``approval`` was a later chain step's table, and this one has
# none because ``operation`` is an earlier step's table living in a *different*
# ``MetaData``. A cross-metadata foreign key would make this revision's
# ``create_all`` depend on a table it does not own, for a reference nothing joins on:
# every read here is by ``approval.id`` or by ``operation.id``, never across.
#
# ``destination_ref`` is written null by every path in release one, and that is a
# recorded gap rather than an oversight: ``OperationDeclaration`` declares no
# destination field (``rheo_contracts.manifest``), so there is nothing for the
# dispatcher to read one *from*. A destination therefore lives inside the payload,
# where a differing destination is a differing ``payload_digest`` and the binding
# tuple still refuses it. The column's first real writer is the phase-five execution
# operation, which has a registered destination to name.
#
# ``purpose`` is nullable for the same kind of reason: the purpose vocabulary it draws
# from is a Leads concern (``confirmation-and-safety.md`` § Opportunities), and no
# module in release one declares one, so there is no value any caller could supply.
approval = Table(
    "approval",
    approval_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=True),
    Column("operation_name", Text, nullable=False),
    Column("operation_id", UUID(as_uuid=True), nullable=False),
    Column("destination_ref", Text, nullable=True),
    Column("subject_ref", Text, nullable=True),
    Column("subject_revision", Integer, nullable=True),
    Column("payload_digest", LargeBinary, nullable=False),
    # The snapshot row's key, which is this row's own id while a snapshot exists and
    # null once the deletion cascade has removed it (§ The approval record: the
    # payload row "is deleted when its approval is invalidated"). So this column is
    # not a second copy of ``id``: it is the one place a reader learns whether the
    # snapshot is still there without joining to find out.
    Column("payload_ref", UUID(as_uuid=True), nullable=True),
    Column("purpose", Text, nullable=True),
    Column("window_start", _timestamptz(), nullable=False),
    Column("window_end", _timestamptz(), nullable=False),
    Column("state", Text, nullable=False),
    Column("approved_by_kind", Text, nullable=True),
    Column("approved_by_id", UUID(as_uuid=True), nullable=True),
    Column("approved_at", _timestamptz(), nullable=True),
    Column("approved_entry", Text, nullable=True),
    Column("executed_at", _timestamptz(), nullable=True),
    Column("invalidated_reason", Text, nullable=True),
    CheckConstraint(_in("state", APPROVAL_STATES), name="approval_state"),
)

# ``body`` is the operation input as it will execute, serialised; it is never queried
# — only shown at the point of confirmation — which is the same exemption
# ``work_tables``'s ``outbox_event.data`` and ``job.input`` take. ``byte_length`` is
# the measured size the ``approvals.max_payload_bytes`` bound was checked against, so
# a reader can see what was accepted without loading the document to measure it.
approval_payload = Table(
    "approval_payload",
    approval_metadata,
    Column(
        "approval_id",
        UUID(as_uuid=True),
        ForeignKey("core.approval.id"),
        primary_key=True,
    ),
    Column("body", JSONB, nullable=False),
    Column("byte_length", Integer, nullable=False),
)
