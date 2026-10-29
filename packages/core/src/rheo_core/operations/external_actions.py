"""Repository writes/reads for durable external actions staged by core."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from sqlalchemy import Connection, RowMapping, exists, insert, select, update

from rheo_core.operations.idempotency_tables import external_action
from rheo_core.refs import uuid7
from rheo_core.storage.backend import UnitOfWork


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
    provider_request_id: str | None = None
    failure_class: str | None = None
    attempts: int = 0
    result_ref: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    terminal_at: datetime | None = None


def _text(value: object) -> str | None:
    return None if value is None else str(value)


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
        provider_request_id=_text(row["provider_request_id"]),
        failure_class=_text(row["failure_class"]),
        attempts=int(row["attempts"]),
        result_ref=_text(row["result_ref"]),
        error_code=_text(row["error_code"]),
        error_detail=_text(row["error_detail"]),
        terminal_at=row["terminal_at"],
    )


def get(
    conn: Connection, *, action_id: UUID, for_update: bool = False
) -> ExternalActionRow | None:
    """One action by id; ``for_update`` takes its row lock in this transaction."""
    query = select(external_action).where(external_action.c.id == action_id)
    if for_update:
        query = query.with_for_update()
    found = conn.execute(query).mappings().one_or_none()
    return None if found is None else _row(found)


def get_for_approval(
    conn: Connection, *, approval_id: UUID
) -> ExternalActionRow | None:
    found = (
        conn.execute(
            select(external_action).where(external_action.c.approval_id == approval_id)
        )
        .mappings()
        .one_or_none()
    )
    return None if found is None else _row(found)


def get_for_operation(
    conn: Connection, *, operation_id: UUID
) -> ExternalActionRow | None:
    found = (
        conn.execute(
            select(external_action).where(
                external_action.c.operation_id == operation_id
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if found is None else _row(found)


OPEN_STATES: Final = ("pending", "executing")


def attach_job(
    conn: Connection, *, action_id: UUID, job_id: UUID, now: datetime
) -> bool:
    """Bind the one continuation job to a pending action; at most once."""
    return (
        conn.execute(
            update(external_action)
            .where(
                external_action.c.id == action_id,
                external_action.c.state == "pending",
                external_action.c.execution_job_id.is_(None),
            )
            .values(execution_job_id=job_id, updated_at=now)
        ).rowcount
        == 1
    )


def update_action(
    conn: Connection,
    *,
    action_id: UUID,
    from_states: tuple[str, ...],
    **values: object,
) -> bool:
    """One predicated write of an action; answers whether exactly one row moved."""
    return (
        conn.execute(
            update(external_action)
            .where(
                external_action.c.id == action_id,
                external_action.c.state.in_(from_states),
            )
            .values(**values)
        ).rowcount
        == 1
    )


# --- the supported public read ------------------------------------------------------


EXTERNAL_ACTION_GET: Final = "core.external_action.get"


class ExternalActionRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_action_id: UUID


class ExternalActionRecord(BaseModel):
    """One durable external action as core publishes it.

    ``restage_eligible`` is core's answer to "may the same intent be staged again
    under a new approval": true only for a ``failed`` action (always proven no
    effect) and for a destination-unavailable refusal. The idempotency key is not
    published; the caller already holds it.
    """

    model_config = ConfigDict(frozen=True)

    external_action_id: UUID
    operation_id: UUID
    approval_id: UUID
    operation_name: str
    subject_ref: str | None
    source_ref: str | None
    destination_ref: str
    state: str
    failure_class: str | None
    attempts: int
    result_ref: str | None
    error_code: str | None
    error_detail: str | None
    execution_job_id: UUID | None
    restage_eligible: bool
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


def published(row: ExternalActionRow) -> ExternalActionRecord:
    return ExternalActionRecord(
        external_action_id=row.id,
        operation_id=row.operation_id,
        approval_id=row.approval_id,
        operation_name=row.operation_name,
        subject_ref=row.subject_ref,
        source_ref=row.source_ref,
        destination_ref=row.destination_ref,
        state=row.state,
        failure_class=row.failure_class,
        attempts=row.attempts,
        result_ref=row.result_ref,
        error_code=row.error_code,
        error_detail=row.error_detail,
        execution_job_id=row.execution_job_id,
        restage_eligible=(
            row.state == "failed"
            or (
                row.state == "refused"
                and row.failure_class == "destination_unavailable_no_effect"
            )
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def get_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ExternalActionRef
) -> ExternalActionRecord:
    """The context workspace's action, or ``not_found``."""
    from rheo_core.operations.refusals import OperationRefused
    from rheo_core.refs.resolver import NOT_FOUND

    row = get(uow.connection, action_id=model_input.external_action_id)
    if row is None:
        raise OperationRefused(
            NOT_FOUND,
            f"no external action {model_input.external_action_id} in this workspace",
        )
    return published(row)


EXTERNAL_ACTION_GET_DECLARATION: Final = OperationDeclaration(
    name=EXTERNAL_ACTION_GET,
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    input_model=ExternalActionRef,
    output=ExternalActionRecord,
    idempotency=Idempotency.NONE,
    audit=None,
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


def has_action_for_key(
    conn: Connection, *, operation_name: str, idempotency_key: str
) -> bool:
    """Whether any action, settled or not, was ever staged under this exact key.

    Terminal actions outlive their swept keyed envelope as history, so this answers
    for keys the idempotency table no longer remembers.
    """
    return bool(
        conn.execute(
            select(
                exists().where(
                    external_action.c.operation_name == operation_name,
                    external_action.c.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
    )
