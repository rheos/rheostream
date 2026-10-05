"""The intake service, manual capture tool operation, and owner health read."""

import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.events.deliveries import failed_delivery_count_for_subjects
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import select

from rheo_leads.contracts import (
    AcceptDeliveryInput,
    AcceptedDelivery,
    CaptureInput,
    ConnectionHealth,
    ConnectionInput,
)
from rheo_leads.events import CONSUMER_ID
from rheo_leads.intake.accept import accept_delivery
from rheo_leads.references import identifier, ref
from rheo_leads.storage import tables as t


def capture(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: CaptureInput
) -> AcceptedDelivery:
    connection_id = uow.connection.execute(
        select(t.intake_connection.c.id).where(
            t.intake_connection.c.transport == "manual"
        )
    ).scalar_one_or_none()
    if connection_id is None:
        raise OperationRefused("not_found", "manual connection unavailable")
    try:
        body = json.dumps(model_input.body, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise OperationRefused("input_invalid", "capture body must be JSON") from None
    return accept_delivery(
        ctx,
        uow,
        AcceptDeliveryInput(
            connection_ref=ref("intake_connection", connection_id),
            source_event_id=str(uuid7()),
            body=body,
            funnel_ref=model_input.funnel_ref,
            campaign_ref=model_input.campaign_ref,
        ),
    )


def health(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ConnectionInput
) -> ConnectionHealth:
    connection_id = identifier(model_input.connection_ref, "intake_connection")
    if (
        uow.connection.execute(
            select(t.intake_connection.c.id).where(
                t.intake_connection.c.id == connection_id
            )
        ).scalar_one_or_none()
        is None
    ):
        raise OperationRefused("not_found", "connection unavailable")
    row = (
        uow.connection.execute(
            select(t.connection_health).where(
                t.connection_health.c.connection_id == connection_id
            )
        )
        .mappings()
        .one_or_none()
    )
    accepted: datetime | None = row["last_accepted_at"] if row else None
    processed: datetime | None = row["last_processed_at"] if row else None
    receipts: Sequence[UUID] = (
        uow.connection.execute(
            select(t.delivery_receipt.c.id).where(
                t.delivery_receipt.c.connection_id == connection_id
            )
        )
        .scalars()
        .all()
    )
    # No invented timestamp for a connection that has never processed anything.
    lag = (
        max(0, int((accepted - processed).total_seconds()))
        if accepted and processed
        else 0
    )
    return ConnectionHealth(
        last_accepted_at=accepted,
        last_processed_at=processed,
        lag_seconds=lag,
        unresolved_failures=int(row["unresolved_failures"]) if row else 0,
        failed_delivery_count=failed_delivery_count_for_subjects(
            uow.connection,
            consumer_id=CONSUMER_ID,
            subject_refs=[ref("delivery_receipt", r) for r in receipts],
        ),
    )


ACCEPT_DECLARATION = OperationDeclaration(
    name="leads.intake.accept_delivery",
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE}),
    input_model=AcceptDeliveryInput,
    output=AcceptedDelivery,
    idempotency=Idempotency.NATURAL,
    audit=AuditSpec(subject_field="connection_ref"),
)
CAPTURE_DECLARATION = OperationDeclaration(
    name="leads.intake.capture",
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER, Role.MEMBER}),
    input_model=CaptureInput,
    output=AcceptedDelivery,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)
HEALTH_DECLARATION = OperationDeclaration(
    name="leads.connection.health",
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER}),
    input_model=ConnectionInput,
    output=ConnectionHealth,
    idempotency=Idempotency.NONE,
    audit=None,
)
OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (ACCEPT_DECLARATION, accept_delivery),
    (CAPTURE_DECLARATION, capture),
    (HEALTH_DECLARATION, health),
)
