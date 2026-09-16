"""The seven durable-work tables of a workspace database, as SQLAlchemy Core ``Table``
objects in schema ``core``.

Columns are verbatim from ``docs/architecture/intake-and-events.md`` § Events and the
outbox (FR 15), § Jobs and the worker (FR 16), § Operations (FR 17) and § Audit (FR 18).

**Why a second ``MetaData`` and not seven more tables in ``core_tables.py``.**
``0001_core_schema`` calls ``core_metadata.create_all(checkfirst=False)``, and both that
revision and ``core_tables.py`` state it is never edited again. Attaching these seven to
``core_metadata`` would make revision 0001 create thirteen tables on a fresh database
and edit a frozen revision by side effect. One ``MetaData`` per revision keeps every
``create_all`` exact: ``0002_durable_work`` creates exactly the seven below.

**Schema only, deliberately.** This module defines DDL and executes none of it:
``lease_owner``, ``lease_until``, ``cancel_requested`` and ``attempts`` are columns
here and nothing more.

The behaviour over this shape lives elsewhere, and is named rather than deferred
because a sentence about what does not exist yet goes false the moment it does. The
job lease is ``work/jobs.py``'s ``acquire_lease``; the retry budget is
``work/backoff.py`` with ``work/loop.py``; cancellation is ``cancel_requested``'s
checkpoint in ``work/loop.py``; the outbox writer and its fan-out are
``events/publish.py``; delivery is ``work/loop.py`` with ``storage/deliveries.py``;
the operation record's lifecycle beyond this DDL is ``operations/dispatch.py``; and
the audit write is ``audit/`` reached from that same dispatcher.
"""

from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

DELIVERY_STATES: Final = ("pending", "leased", "delivered", "failed", "skipped")
JOB_STATES: Final = ("queued", "leased", "succeeded", "failed", "cancelled")
OPERATION_STATES: Final = (
    "pending",
    "running",
    "approval_required",
    "succeeded",
    "failed",
    "cancelled",
    "unresolved",
)
AUDIT_OUTCOMES: Final = ("succeeded", "failed", "refused")

work_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


# ``data`` is the declared event model serialised; it is never queried, only delivered,
# which is the one place the house rule allows a JSON column. ``position`` is the
# document's ``bigserial``: an auto-assigned monotonic bigint on a column that is not
# the primary key, which SQLAlchemy expresses as an identity column rather than the
# ``SERIAL`` shorthand it renders only for integer primary keys. ``always=False`` keeps
# an explicit value insertable, exactly as ``bigserial`` does.
outbox_event = Table(
    "outbox_event",
    work_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("position", BigInteger, Identity(always=False), nullable=False),
    Column("type", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("source", Text, nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("subject_revision", Integer, nullable=False),
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("causation_id", UUID(as_uuid=True), nullable=True),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=True),
    Column("occurred_at", _timestamptz(), nullable=False),
    Column("data", JSONB, nullable=False),
)

# ``subject_ref`` and ``position`` are copied from the event at fan-out so the ordering
# index is local to this table. No foreign key to ``outbox_event``: the document names
# none here, and the only two it names are the ``operation_id`` columns below.
event_delivery = Table(
    "event_delivery",
    work_metadata,
    Column("event_id", UUID(as_uuid=True), nullable=False),
    Column("consumer_id", Text, nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("position", BigInteger, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", _timestamptz(), nullable=False),
    Column("lease_owner", Text, nullable=True),
    Column("lease_until", _timestamptz(), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("completed_at", _timestamptz(), nullable=True),
    PrimaryKeyConstraint("event_id", "consumer_id", name="event_delivery_pkey"),
    CheckConstraint(_in("state", DELIVERY_STATES), name="event_delivery_state"),
    Index("event_delivery_order", "consumer_id", "subject_ref", "position"),
)

# Written inside the consumer's own transaction; the primary key is what makes a second
# delivery of the same event to the same consumer a refusal rather than a repeat.
consumer_processed = Table(
    "consumer_processed",
    work_metadata,
    Column("consumer_id", Text, nullable=False),
    Column("event_id", UUID(as_uuid=True), nullable=False),
    Column("processed_at", _timestamptz(), nullable=False),
    PrimaryKeyConstraint("consumer_id", "event_id", name="consumer_processed_pkey"),
)

# ``input`` is the job kind's declared model serialised and never queried, the second of
# the two JSON columns the document allows. ``depends_on_ref`` lets the deletion
# coordinator cancel dependents.
job = Table(
    "job",
    work_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("input", JSONB, nullable=False),
    Column(
        "operation_id",
        UUID(as_uuid=True),
        ForeignKey("core.operation.id"),
        nullable=True,
    ),
    Column("depends_on_ref", Text, nullable=True),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("next_run_at", _timestamptz(), nullable=False),
    Column("lease_owner", Text, nullable=True),
    Column("lease_until", _timestamptz(), nullable=True),
    Column("cancel_requested", Boolean, nullable=False),
    Column("last_error", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("finished_at", _timestamptz(), nullable=True),
    CheckConstraint(_in("state", JOB_STATES), name="job_state"),
)

# Created at module enable from the manifest.
schedule = Table(
    "schedule",
    work_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("module_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("job_kind", Text, nullable=False),
    Column("cron", Text, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("last_run_at", _timestamptz(), nullable=True),
    Column("next_run_at", _timestamptz(), nullable=False),
)

# ``approval_id`` is a plain column with **no** foreign key: ``approval`` is a later
# chain step's table and does not exist yet. ``actor_id`` and ``audience_id`` are
# nullable because ``rheo_contracts.context`` declares both as ``UUID | None`` (a
# ``system`` actor carries no id). The document gives the last rows names without types
# or nullability, so they are transcribed as the document's own state machine requires:
# ``progress_text`` and ``progress_fraction`` are null until the job that updates them
# runs, and ``started_at``/``terminal_at`` are null while the record is ``pending`` — a
# NOT NULL on either would make the document's first state unrepresentable.
operation = Table(
    "operation",
    work_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("safety_class", Text, nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=True),
    Column("entry", Text, nullable=False),
    Column("audience_kind", Text, nullable=False),
    Column("audience_id", UUID(as_uuid=True), nullable=True),
    Column("state", Text, nullable=False),
    Column("approval_id", UUID(as_uuid=True), nullable=True),
    Column("progress_text", Text, nullable=True),
    Column("progress_fraction", Float, nullable=True),
    Column("result_ref", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_text", Text, nullable=True),
    Column("terminal_check_kind", Text, nullable=True),
    Column("terminal_check_at", _timestamptz(), nullable=True),
    Column("provider_request_id", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("started_at", _timestamptz(), nullable=True),
    Column("terminal_at", _timestamptz(), nullable=True),
    CheckConstraint(_in("state", OPERATION_STATES), name="operation_state"),
    # Criterion 13's schema half: ``succeeded`` is unrepresentable without a recorded
    # terminal check. Scoped to that one state, so a ``pending`` record is unaffected.
    CheckConstraint(
        "state <> 'succeeded' OR "
        "(terminal_check_kind IS NOT NULL AND terminal_check_at IS NOT NULL)",
        name="operation_terminal_check_required",
    ),
)

# ``request_digest`` is the SHA-256 of the canonical input, so "what exactly was
# requested" is answerable without storing the input itself.
audit_record = Table(
    "audit_record",
    work_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("occurred_at", _timestamptz(), nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=True),
    Column("entry", Text, nullable=False),
    Column("operation_name", Text, nullable=False),
    Column("safety_class", Text, nullable=False),
    Column(
        "operation_id",
        UUID(as_uuid=True),
        ForeignKey("core.operation.id"),
        nullable=True,
    ),
    Column("subject_ref", Text, nullable=True),
    Column("request_digest", LargeBinary, nullable=False),
    Column("outcome", Text, nullable=False),
    CheckConstraint(_in("outcome", AUDIT_OUTCOMES), name="audit_record_outcome"),
)
