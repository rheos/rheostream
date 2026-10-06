"""Ordered intake steps, with finalization after the extension seam."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from rheo_contracts import EventEnvelope, WorkspaceContext
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import insert, select, update
from sqlalchemy.exc import StatementError

from rheo_leads.contracts import ObservationAccepted
from rheo_leads.events import OBSERVATION_ACCEPTED, publish_event
from rheo_leads.intake.accept import health_write
from rheo_leads.intake.mapping import (
    MISSING,
    Fact,
    MappingRefused,
    apply_mapping,
    resolve_identity,
)
from rheo_leads.pipeline_common import lock
from rheo_leads.pipeline_intake import resolve_party, route
from rheo_leads.references import identifier, ref
from rheo_leads.storage import tables as t


@dataclass
class DeliveryState:
    receipt: Mapping[str, object]
    connection: Mapping[str, object]
    payload: object = None
    facts: list[Fact] = field(default_factory=list)
    identity: Mapping[str, object] = field(default_factory=dict)
    source_kind: str = "json"
    observation_id: UUID | None = None


class DeliveryRefused(ValueError):
    """A permanent, content-free processing refusal."""


def recheck_connection(
    ctx: WorkspaceContext, uow: UnitOfWork, state: DeliveryState
) -> None:
    connection, receipt = state.connection, state.receipt
    if connection["state"] != "active":
        raise DeliveryRefused("connection_revoked")
    generation = receipt["signing_key_generation"]
    if receipt["transport"] == "webhook":
        current = connection["signing_key_generation"]
        until = connection["previous_valid_until"]
        previous_valid = (
            isinstance(current, int)
            and generation == current - 1
            and connection["previous_secret_ref"] is not None
            and isinstance(until, datetime)
            and until > datetime.now(UTC)
        )
        if generation is None or (generation != current and not previous_valid):
            raise DeliveryRefused("connection_revoked")


def apply_pinned_mapping(
    ctx: WorkspaceContext, uow: UnitOfWork, state: DeliveryState
) -> None:
    receipt = state.receipt
    payload = uow.connection.execute(
        select(t.delivery_payload.c.body).where(
            t.delivery_payload.c.receipt_id == receipt["id"]
        )
    ).scalar_one_or_none()
    if payload is None:
        raise DeliveryRefused("payload_unavailable")
    try:
        state.payload = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise DeliveryRefused("payload_invalid") from None
    if not isinstance(state.payload, dict):
        raise DeliveryRefused("payload_invalid")
    mapping = (
        uow.connection.execute(
            select(t.field_mapping).where(
                t.field_mapping.c.id == receipt["mapping_id"],
                t.field_mapping.c.version == receipt["mapping_version"],
            )
        )
        .mappings()
        .one()
    )
    state.source_kind = mapping["source_kind"]
    rules = (
        uow.connection.execute(
            select(t.field_mapping_rule)
            .where(
                t.field_mapping_rule.c.mapping_id == receipt["mapping_id"],
                t.field_mapping_rule.c.version == receipt["mapping_version"],
            )
            .order_by(t.field_mapping_rule.c.target)
        )
        .mappings()
        .all()
    )
    try:
        state.facts = apply_mapping(rules, state.payload, source_kind=state.source_kind)
    except MappingRefused as exc:
        # Only the configured target is allowed into persisted diagnostics.
        raise DeliveryRefused("mapping_failed:" + str(exc)) from None
    identity_paths = (
        uow.connection.execute(
            select(t.field_mapping_identity).where(
                t.field_mapping_identity.c.mapping_id == receipt["mapping_id"],
                t.field_mapping_identity.c.version == receipt["mapping_version"],
            )
        )
        .mappings()
        .one()
    )
    try:
        state.identity = resolve_identity(
            dict(identity_paths), state.payload, source_kind=state.source_kind
        )
    except MappingRefused:
        raise DeliveryRefused("identity_invalid") from None


def _identity_text(state: DeliveryState, key: str) -> str | None:
    value = state.identity.get(key, MISSING)
    if value is MISSING or value is None:
        return None
    if not isinstance(value, str | int):
        raise DeliveryRefused("identity_invalid:" + key)
    return str(value)


def create_observation(
    ctx: WorkspaceContext, uow: UnitOfWork, state: DeliveryState
) -> None:
    receipt = state.receipt
    occurred = receipt["source_occurred_at"]
    if occurred is None:
        raw = _identity_text(state, "occurred_at_path")
        if raw is not None:
            try:
                occurred = datetime.fromisoformat(raw)
                if occurred.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise DeliveryRefused("identity_invalid:occurred_at_path") from None
    # Receipt and observation share the UUID, with distinct canonical types.
    # The receipt remains locatable after owned deletion removes the observation.
    observation_id = UUID(str(receipt["id"]))
    uow.connection.execute(
        insert(t.observation).values(
            id=observation_id,
            receipt_id=receipt["id"],
            connection_id=receipt["connection_id"],
            funnel_id=receipt["funnel_id"],
            campaign_id=receipt["campaign_id"],
            transport=receipt["transport"],
            source_event_id=receipt["source_event_id"],
            external_subject_id=_identity_text(state, "subject_id_path")
            if state.connection["subject_authenticated"]
            else None,
            verified_email=_identity_text(state, "verified_email_path")
            if state.connection["email_verified"]
            else None,
            source_occurred_at=occurred or receipt["received_at"],
            received_at=receipt["received_at"],
            mapping_version=receipt["mapping_version"],
            completeness=sum(f.value_kind == "value" for f in state.facts),
            party_ref=None,
            created_at=datetime.now(UTC),
        )
    )
    if state.facts:
        uow.connection.execute(
            insert(t.observation_field),
            [
                {
                    "observation_id": observation_id,
                    "target": f.target,
                    "value_kind": f.value_kind,
                    "value_text": f.value_text,
                    "source_path": f.source_path,
                    "transform": f.transform,
                    "mapping_version": receipt["mapping_version"],
                }
                for f in state.facts
            ],
        )
    state.observation_id = observation_id


Step = Callable[[WorkspaceContext, UnitOfWork, DeliveryState], None]
# Party resolution (4), routing (5), and derivation (6) extend this sequence.
STEPS: tuple[tuple[int, Step], ...] = (
    (1, recheck_connection),
    (2, apply_pinned_mapping),
    (3, create_observation),
    (4, resolve_party),
    (5, route),
)


def _process(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    ctx = uow.consumer_context
    if ctx is None or uow.consumers is None:
        raise RuntimeError("consumer publication context unavailable")
    lock(uow)
    receipt_id = identifier(envelope.subject_ref, "delivery_receipt")
    receipt = (
        uow.connection.execute(
            select(t.delivery_receipt).where(t.delivery_receipt.c.id == receipt_id)
        )
        .mappings()
        .one_or_none()
    )
    if (
        receipt is None
        or receipt["state"] != "pending"
        or receipt["state_detail"] == "observation_deleted"
    ):
        return
    # Same lock ordering as acceptance: connection, then receipt. Revocation and
    # erasure cannot commit between a successful recheck and finalization.
    connection = (
        uow.connection.execute(
            select(t.intake_connection)
            .where(t.intake_connection.c.id == receipt["connection_id"])
            .with_for_update()
        )
        .mappings()
        .one()
    )
    receipt = (
        uow.connection.execute(
            select(t.delivery_receipt)
            .where(t.delivery_receipt.c.id == receipt_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if (
        receipt["state"] != "pending"
        or receipt["state_detail"] == "observation_deleted"
    ):
        return
    state = DeliveryState(dict(receipt), dict(connection))
    try:
        for _, step in STEPS:
            step(ctx, uow, state)
    except DeliveryRefused as exc:
        detail = str(exc)
        uow.connection.execute(
            update(t.delivery_receipt)
            .where(t.delivery_receipt.c.id == receipt_id)
            .values(state="refused", state_detail=detail)
        )
        health_write(uow, connection["id"], error=detail)
        return
    assert state.observation_id is not None
    now = datetime.now(UTC)
    uow.connection.execute(
        update(t.delivery_receipt)
        .where(t.delivery_receipt.c.id == receipt_id)
        .values(state="processed", processed_at=now)
    )
    health_write(uow, connection["id"], processed=now)
    publish_event(
        ctx,
        uow,
        event_type=OBSERVATION_ACCEPTED,
        subject=ref("observation", state.observation_id),
        data=ObservationAccepted(
            observation_ref=ref("observation", state.observation_id),
            receipt_ref=ref("delivery_receipt", receipt_id),
            connection_ref=ref("intake_connection", connection["id"]),
            funnel_ref=ref("funnel", receipt["funnel_id"]),
            campaign_ref=ref("campaign", receipt["campaign_id"])
            if receipt["campaign_id"]
            else None,
            party_ref=uow.connection.execute(
                select(t.observation.c.party_ref).where(
                    t.observation.c.id == state.observation_id
                )
            ).scalar_one(),
            completeness=sum(f.value_kind == "value" for f in state.facts),
        ),
        causation_id=envelope.id,
    )


def process_delivery(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    try:
        _process(uow, envelope)
    except StatementError:
        # SQLAlchemy exceptions include bound payload/field values. The worker
        # persists exception text; keep its retry diagnostic content-free.
        raise RuntimeError("intake_storage_failed") from None
