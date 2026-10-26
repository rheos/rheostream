"""The durable keyed/external schema and its post-0014 runtime shape.

``external_action_metadata`` is the frozen revision-0014 shape, including its original
composite foreign key. Revision 0015 removes that database constraint so a terminal
action can remain as history after its expired keyed envelope is swept. The runtime
tables below mirror the 0015 head: operation/key text remains immutable history, while
``idempotency.py`` explicitly protects pending, executing, and unresolved actions.
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

IDEMPOTENCY_STATES: Final = (
    "pending",
    "succeeded",
    "failed",
    "refused",
    "unresolved",
    "cancelled",
)
EXTERNAL_ACTION_STATES: Final = (
    "pending",
    "executing",
    "succeeded",
    "failed",
    "refused",
    "unresolved",
    "cancelled",
)
EXTERNAL_FAILURE_CLASSES: Final = (
    "destination_unavailable_no_effect",
    "permanent_refusal_no_effect",
    "retryable_no_effect",
    "outcome_unknown",
    "cancelled_no_effect",
)

external_action_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


_idempotency_result_0014 = Table(
    "idempotency_result",
    external_action_metadata,
    Column("operation_name", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_identity", JSONB, nullable=False),
    Column("request_digest", LargeBinary, nullable=False),
    Column("request_schema_version", Integer, nullable=False),
    Column("state", Text, nullable=False),
    # Logical reference to core.operation.id. The referenced table belongs to the
    # frozen 0002 metadata, so this later metadata cannot carry a cross-metadata FK
    # without making create_all resolve a table this revision does not own.
    Column("operation_id", UUID(as_uuid=True), nullable=True),
    Column("result", JSONB, nullable=True),
    Column("result_schema_version", Integer, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_detail", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("updated_at", _timestamptz(), nullable=False),
    Column("retained_until", _timestamptz(), nullable=False),
    PrimaryKeyConstraint(
        "operation_name", "idempotency_key", name="idempotency_result_pkey"
    ),
    CheckConstraint(
        "octet_length(idempotency_key) BETWEEN 1 AND 256",
        name="idempotency_result_key_bytes",
    ),
    CheckConstraint(
        "request_schema_version > 0",
        name="idempotency_result_request_version_positive",
    ),
    CheckConstraint(
        "result_schema_version IS NULL OR result_schema_version > 0",
        name="idempotency_result_result_version_positive",
    ),
    CheckConstraint(_in("state", IDEMPOTENCY_STATES), name="idempotency_result_state"),
    CheckConstraint(
        "retained_until >= created_at",
        name="idempotency_result_retention_order",
    ),
    CheckConstraint(
        "(state = 'succeeded' AND result IS NOT NULL "
        "AND result_schema_version IS NOT NULL AND error_code IS NULL) OR "
        "(state = 'pending' AND result IS NULL AND result_schema_version IS NULL "
        "AND error_code IS NULL AND error_detail IS NULL) OR "
        "(state IN ('failed', 'refused', 'unresolved', 'cancelled') "
        "AND result IS NULL AND result_schema_version IS NULL "
        "AND error_code IS NOT NULL)",
        name="idempotency_result_terminal_shape",
    ),
    Index("idempotency_result_retained_until", "retained_until"),
)


_external_action_0014 = Table(
    "external_action",
    external_action_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    # Logical references to rows owned by earlier immutable migration metadata. The
    # one-to-one bindings are enforced by the unique constraints below; continuation
    # validates the complete binding under locks before any effect.
    Column("operation_id", UUID(as_uuid=True), nullable=False),
    Column("approval_id", UUID(as_uuid=True), nullable=False),
    Column("operation_name", Text, nullable=False),
    Column("subject_ref", Text, nullable=True),
    Column("source_ref", Text, nullable=True),
    Column("destination_ref", Text, nullable=False),
    Column("approval_payload_ref", UUID(as_uuid=True), nullable=False),
    Column("approval_payload_digest", LargeBinary, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_schema_version", Integer, nullable=False),
    Column("result_schema_version", Integer, nullable=False),
    Column("provider_request_id", Text, nullable=True),
    Column("execution_job_id", UUID(as_uuid=True), nullable=True),
    Column("state", Text, nullable=False),
    Column("failure_class", Text, nullable=True),
    Column("attempts", Integer, nullable=False),
    Column("result_ref", Text, nullable=True),
    Column("result_detail", JSONB, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_detail", Text, nullable=True),
    Column("created_at", _timestamptz(), nullable=False),
    Column("updated_at", _timestamptz(), nullable=False),
    Column("terminal_at", _timestamptz(), nullable=True),
    ForeignKeyConstraint(
        ["operation_name", "idempotency_key"],
        [
            "core.idempotency_result.operation_name",
            "core.idempotency_result.idempotency_key",
        ],
        name="external_action_idempotency_result_fkey",
    ),
    UniqueConstraint("operation_id", name="external_action_operation_key"),
    UniqueConstraint("approval_id", name="external_action_approval_key"),
    UniqueConstraint("execution_job_id", name="external_action_job_key"),
    CheckConstraint(
        "octet_length(idempotency_key) BETWEEN 1 AND 256",
        name="external_action_key_bytes",
    ),
    CheckConstraint(
        "request_schema_version > 0 AND result_schema_version > 0",
        name="external_action_schema_versions_positive",
    ),
    CheckConstraint("attempts >= 0", name="external_action_attempts_nonnegative"),
    CheckConstraint(_in("state", EXTERNAL_ACTION_STATES), name="external_action_state"),
    CheckConstraint(
        "failure_class IS NULL OR " + _in("failure_class", EXTERNAL_FAILURE_CLASSES),
        name="external_action_failure_class",
    ),
    CheckConstraint(
        "(state IN ('pending', 'executing') AND terminal_at IS NULL) OR "
        "(state IN ('succeeded', 'failed', 'refused', 'unresolved', 'cancelled') "
        "AND terminal_at IS NOT NULL)",
        name="external_action_terminal_time",
    ),
    CheckConstraint(
        "state <> 'succeeded' OR (result_ref IS NOT NULL AND failure_class IS NULL)",
        name="external_action_success_result",
    ),
    CheckConstraint(
        "state NOT IN ('failed', 'refused', 'unresolved', 'cancelled') "
        "OR (failure_class IS NOT NULL AND error_code IS NOT NULL)",
        name="external_action_failure_shape",
    ),
    Index(
        "external_action_provider_request",
        "destination_ref",
        "provider_request_id",
        unique=True,
        postgresql_where=text("provider_request_id IS NOT NULL"),
    ),
)


# Runtime metadata at the 0015 head. Keep the 0014 metadata above unchanged so a
# fresh migration chain still creates the original constraint before 0015 drops it.
external_action_runtime_metadata = MetaData(schema=CORE_SCHEMA)
idempotency_result = _idempotency_result_0014.to_metadata(
    external_action_runtime_metadata
)
external_action = _external_action_0014.to_metadata(external_action_runtime_metadata)
for _constraint in tuple(external_action.constraints):
    if _constraint.name != "external_action_idempotency_result_fkey":
        continue
    external_action.constraints.remove(_constraint)
    assert isinstance(_constraint, ForeignKeyConstraint)
    for _foreign_key in _constraint.elements:
        _foreign_key.parent.foreign_keys.discard(_foreign_key)
        external_action.foreign_keys.discard(_foreign_key)
