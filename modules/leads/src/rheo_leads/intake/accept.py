"""Durable acceptance: unique receipt, payload, event, and health share one UOW."""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rheo_contracts import ActorKind, Entry, Role, WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.repositories import list_module_states
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rheo_leads.contracts import AcceptDeliveryInput, AcceptedDelivery, DeliveryReceived
from rheo_leads.events import DELIVERY_RECEIVED, publish_event
from rheo_leads.references import identifier, ref
from rheo_leads.storage import tables as t


def health_write(
    uow: UnitOfWork,
    connection_id: UUID,
    *,
    error: str | None = None,
    accepted: datetime | None = None,
    processed: datetime | None = None,
) -> None:
    values: dict[str, Any] = {"connection_id": connection_id}
    updates: dict[str, Any] = {}
    if accepted is not None:
        values["last_accepted_at"] = updates["last_accepted_at"] = accepted
    if processed is not None:
        values["last_processed_at"] = updates["last_processed_at"] = processed
    if error is not None:
        values.update(
            unresolved_failures=1, last_error=error, last_error_at=datetime.now(UTC)
        )
        updates.update(
            last_error=error,
            last_error_at=values["last_error_at"],
            unresolved_failures=t.connection_health.c.unresolved_failures + 1,
        )
    uow.connection.execute(
        insert(t.connection_health)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[t.connection_health.c.connection_id], set_=updates
        )
    )


def attribution(
    uow: UnitOfWork, funnel_id: UUID | None, campaign_id: UUID | None
) -> None:
    if funnel_id is None:
        raise OperationRefused("funnel_required", "choose a funnel")
    found = uow.connection.execute(
        select(t.funnel.c.id).where(
            t.funnel.c.id == funnel_id, t.funnel.c.archived_at.is_(None)
        )
    ).scalar_one_or_none()
    if found is None:
        raise OperationRefused("not_found", "funnel unavailable")
    if (
        campaign_id is not None
        and uow.connection.execute(
            select(t.campaign.c.id).where(
                t.campaign.c.id == campaign_id,
                t.campaign.c.funnel_id == funnel_id,
                t.campaign.c.archived_at.is_(None),
            )
        ).scalar_one_or_none()
        is None
    ):
        raise OperationRefused("not_found", "campaign unavailable")


def accept_delivery(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: AcceptDeliveryInput
) -> AcceptedDelivery:
    # The UTF-8 bytes are the complete accepted payload; no routing fields come from it.
    body = model_input.body.encode("utf-8")
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    )
    if len(body) > settings.get_int("leads.intake.max_payload_bytes"):
        raise OperationRefused("payload_too_large", "payload exceeds configured limit")
    connection_id = identifier(model_input.connection_ref, "intake_connection")
    lock_workspace_lifecycle(uow.connection)
    connection = (
        uow.connection.execute(
            select(t.intake_connection)
            .where(t.intake_connection.c.id == connection_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if connection is None:
        raise OperationRefused("not_found", "connection unavailable")
    if connection["state"] != "active":
        raise OperationRefused("connection_revoked", "connection is inactive")
    if connection["transport"] == "webhook":
        if not any(
            state.module_id == "leads" and state.state == "enabled"
            for state in list_module_states(uow.connection)
        ):
            raise OperationRefused(
                "module_unavailable", "connection module unavailable"
            )
        if (
            ctx.actor.kind is not ActorKind.CONNECTION
            or ctx.actor.id != connection_id
            or ctx.entry is not Entry.INTAKE
        ):
            raise OperationRefused(
                "role_not_permitted", "authenticated connection required"
            )
        generation = model_input.signing_key_generation
        until = connection["previous_valid_until"]
        previous = (
            generation == connection["signing_key_generation"] - 1
            and connection["previous_secret_ref"] is not None
            and until is not None
            and until > datetime.now(UTC)
        )
        if generation is None or (
            generation != connection["signing_key_generation"] and not previous
        ):
            raise OperationRefused(
                "connection_revoked", "signing generation unavailable"
            )
    if connection["transport"] == "manual":
        if model_input.funnel_ref is None:
            raise OperationRefused("funnel_required", "choose a funnel")
        funnel_id = identifier(model_input.funnel_ref, "funnel")
        campaign_id = (
            identifier(model_input.campaign_ref, "campaign")
            if model_input.campaign_ref
            else None
        )
    else:
        if ctx.role is not Role.SERVICE:
            raise OperationRefused("role_not_permitted", "connector transport only")
        if model_input.funnel_ref is not None or model_input.campaign_ref is not None:
            raise OperationRefused("input_invalid", "connection owns attribution")
        funnel_id, campaign_id = connection["funnel_id"], connection["campaign_id"]
    attribution(uow, funnel_id, campaign_id)
    now, receipt_id = datetime.now(UTC), uuid7()
    digest = hashlib.sha256(body).digest()
    inserted = uow.connection.execute(
        insert(t.delivery_receipt)
        .values(
            id=receipt_id,
            connection_id=connection_id,
            source_event_id=model_input.source_event_id,
            content_digest=digest,
            byte_length=len(body),
            transport=connection["transport"],
            received_at=now,
            source_occurred_at=model_input.source_occurred_at,
            mapping_id=connection["mapping_id"],
            mapping_version=connection["mapping_version"],
            funnel_id=funnel_id,
            campaign_id=campaign_id,
            signing_key_generation=model_input.signing_key_generation,
            state="pending",
        )
        .on_conflict_do_nothing(constraint="receipt_event_identity")
        .returning(t.delivery_receipt.c.id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = (
            uow.connection.execute(
                select(t.delivery_receipt)
                .where(
                    t.delivery_receipt.c.connection_id == connection_id,
                    t.delivery_receipt.c.source_event_id == model_input.source_event_id,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        receipt_id = existing["id"]
        erased = existing["state_detail"] == "observation_deleted"
        if not erased and existing["content_digest"] == digest:
            return AcceptedDelivery(
                receipt_ref=ref("delivery_receipt", receipt_id), outcome="duplicate"
            )
        kind = "deleted_observation" if erased else "digest_differs"
        uow.connection.execute(
            insert(t.delivery_conflict).values(
                id=uuid7(),
                receipt_id=receipt_id,
                kind=kind,
                content_digest=digest,
                byte_length=len(body),
                received_at=now,
                body=None if erased else body,
            )
        )
        health_write(uow, connection_id, error=kind)
        return AcceptedDelivery(
            receipt_ref=ref("delivery_receipt", receipt_id), outcome="conflict"
        )
    uow.connection.execute(
        insert(t.delivery_payload).values(
            receipt_id=receipt_id,
            body=body,
            content_type=model_input.content_type,
            byte_length=len(body),
        )
    )
    receipt_ref = ref("delivery_receipt", receipt_id)
    publish_event(
        ctx,
        uow,
        event_type=DELIVERY_RECEIVED,
        subject=receipt_ref,
        data=DeliveryReceived(
            receipt_ref=receipt_ref,
            connection_ref=model_input.connection_ref,
            transport=connection["transport"],
        ),
    )
    health_write(uow, connection_id, accepted=now)
    return AcceptedDelivery(receipt_ref=receipt_ref, outcome="accepted")
