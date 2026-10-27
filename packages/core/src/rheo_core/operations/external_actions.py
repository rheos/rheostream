"""Repository writes/reads for durable external actions staged by core."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, RowMapping, insert, select

from rheo_core.operations.idempotency_tables import external_action
from rheo_core.refs import uuid7


@dataclass(frozen=True, slots=True)
class ExternalActionRow:
    id: UUID
    operation_id: UUID
    approval_id: UUID
    operation_name: str
    subject_ref: str | None
    source_ref: str | None
    destination_ref: str
    approval_payload_ref: UUID
    approval_payload_digest: bytes
    idempotency_key: str
    request_schema_version: int
    result_schema_version: int
    execution_job_id: UUID | None
    state: str
    created_at: datetime
    updated_at: datetime


def _row(row: RowMapping) -> ExternalActionRow:
    return ExternalActionRow(
        id=row["id"],
        operation_id=row["operation_id"],
        approval_id=row["approval_id"],
        operation_name=str(row["operation_name"]),
        subject_ref=(None if row["subject_ref"] is None else str(row["subject_ref"])),
        source_ref=(None if row["source_ref"] is None else str(row["source_ref"])),
        destination_ref=str(row["destination_ref"]),
        approval_payload_ref=row["approval_payload_ref"],
        approval_payload_digest=bytes(row["approval_payload_digest"]),
        idempotency_key=str(row["idempotency_key"]),
        request_schema_version=int(row["request_schema_version"]),
        result_schema_version=int(row["result_schema_version"]),
        execution_job_id=row["execution_job_id"],
        state=str(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_pending(
    conn: Connection,
    *,
    operation_id: UUID,
    approval_id: UUID,
    operation_name: str,
    subject_ref: str | None,
    source_ref: str | None,
    destination_ref: str,
    approval_payload_digest: bytes,
    idempotency_key: str,
    request_schema_version: int,
    result_schema_version: int,
    now: datetime,
) -> ExternalActionRow:
    action_id = uuid7()
    created = (
        conn.execute(
            insert(external_action)
            .values(
                id=action_id,
                operation_id=operation_id,
                approval_id=approval_id,
                operation_name=operation_name,
                subject_ref=subject_ref,
                source_ref=source_ref,
                destination_ref=destination_ref,
                approval_payload_ref=approval_id,
                approval_payload_digest=approval_payload_digest,
                idempotency_key=idempotency_key,
                request_schema_version=request_schema_version,
                result_schema_version=result_schema_version,
                provider_request_id=None,
                execution_job_id=None,
                state="pending",
                failure_class=None,
                attempts=0,
                result_ref=None,
                result_detail=None,
                error_code=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
                terminal_at=None,
            )
            .returning(*external_action.c)
        )
        .mappings()
        .one()
    )
    return _row(created)


def get_for_key(
    conn: Connection, *, operation_name: str, idempotency_key: str
) -> ExternalActionRow | None:
    found = (
        conn.execute(
            select(external_action).where(
                external_action.c.operation_name == operation_name,
                external_action.c.idempotency_key == idempotency_key,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if found is None else _row(found)
