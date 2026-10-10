"""Erase intake content while keeping the delivery identity tombstone."""

from datetime import UTC, datetime
from uuid import UUID

from rheo_contracts import RecordRef, Role, StaleRecord, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    NOTHING_REMOVED,
    DeleteAuthorization,
    Disposition,
    RemovedMemories,
)
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, select, update

from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_evidence import derive
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t


def on_observation_deleted(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, *, disposition: Disposition
) -> RemovedMemories:
    """Works after owned deletion: the observation UUID is also its receipt UUID."""
    if ref.module != "leads" or ref.record_type != "observation":
        return NOTHING_REMOVED
    receipt_id = uow.connection.execute(
        select(t.delivery_receipt.c.id)
        .where(t.delivery_receipt.c.id == ref.id)
        .with_for_update()
    ).scalar_one_or_none()
    if receipt_id is None:
        return NOTHING_REMOVED
    uow.connection.execute(
        delete(t.delivery_payload).where(t.delivery_payload.c.receipt_id == receipt_id)
    )
    uow.connection.execute(
        update(t.delivery_conflict)
        .where(t.delivery_conflict.c.receipt_id == receipt_id)
        .values(body=None)
    )
    uow.connection.execute(
        update(t.delivery_receipt)
        .where(t.delivery_receipt.c.id == receipt_id)
        .values(state_detail="observation_deleted")
    )
    return NOTHING_REMOVED


def authorize_delete(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, *, disposition: Disposition
) -> DeleteAuthorization | Refusal:
    if (
        ref.module != "leads"
        or ref.record_type not in {"observation", "opportunity"}
        or "leads" not in ctx.enabled_modules
        or ctx.role is not Role.OWNER
        or disposition is not Disposition.USER_ERASURE
    ):
        return Refusal("not_found", "record unavailable")
    table = t.observation if ref.record_type == "observation" else p.opportunity
    row = (
        uow.connection.execute(select(table).where(table.c.id == ref.id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return Refusal("not_found", "record unavailable")
    return DeleteAuthorization(
        ref, 1 if ref.record_type == "observation" else row["revision"]
    )


def delete_owned(
    ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
) -> RemovedMemories:
    h.lock(uow)
    reference = authorization.ref
    if reference.record_type == "opportunity":
        deleted = uow.connection.execute(
            delete(p.opportunity)
            .where(
                p.opportunity.c.id == reference.id,
                p.opportunity.c.revision == authorization.revision,
            )
            .returning(p.opportunity.c.id)
        ).scalar_one_or_none()
        if deleted is None:
            raise StaleRecord("opportunity changed since approval")
        return NOTHING_REMOVED
    record = (
        uow.connection.execute(
            select(t.observation)
            .where(t.observation.c.id == reference.id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise StaleRecord("observation changed since approval")
    affected: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_observation.c.opportunity_id).where(
                p.opportunity_observation.c.observation_id == reference.id
            )
        ).scalars()
    )
    uow.connection.execute(
        delete(p.opportunity_observation).where(
            p.opportunity_observation.c.observation_id == reference.id
        )
    )
    uow.connection.execute(
        delete(p.opportunity_field_state).where(
            p.opportunity_field_state.c.observation_id == reference.id
        )
    )
    uow.connection.execute(
        update(p.opportunity)
        .where(p.opportunity.c.source_observation_id == reference.id)
        .values(source_observation_id=None)
    )
    uow.connection.execute(
        update(p.contact_permission)
        .where(p.contact_permission.c.evidence_observation_id == reference.id)
        .values(evidence_observation_id=None)
    )
    # Textual derivatives cannot safely survive loss of their source evidence.
    for table in (p.qualification, p.draft, p.handoff, p.followup_reminder):
        uow.connection.execute(
            delete(table).where(table.c.opportunity_id.in_(affected))
        )
    uow.connection.execute(
        delete(t.observation_field).where(
            t.observation_field.c.observation_id == reference.id
        )
    )
    uow.connection.execute(
        delete(t.observation).where(t.observation.c.id == reference.id)
    )
    for identifier in affected:
        opportunity = h.row(uow, p.opportunity, identifier)
        h.bump(uow, opportunity, **derive(uow, opportunity))
    on_observation_deleted(ctx, uow, reference, disposition=Disposition.USER_ERASURE)
    return NOTHING_REMOVED


def on_party_deleted(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, *, disposition: Disposition
) -> RemovedMemories:
    h.lock(uow)
    if ref.module != "relationships" or ref.record_type != "party":
        return NOTHING_REMOVED
    reference = ref.format()
    uow.connection.execute(
        update(t.observation)
        .where(t.observation.c.party_ref == reference)
        .values(party_ref=None)
    )
    affected: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_party.c.opportunity_id).where(
                p.opportunity_party.c.party_ref == reference,
                p.opportunity_party.c.removed_at.is_(None),
            )
        ).scalars()
    )
    uow.connection.execute(
        update(p.opportunity_party)
        .where(p.opportunity_party.c.party_ref == reference)
        .values(removed_at=datetime.now(UTC))
    )
    for identifier in set(affected):
        h.bump(uow, h.row(uow, p.opportunity, identifier))
    # Keep reference-only purpose suppression; erase free text and disclosures.
    uow.connection.execute(
        update(p.contact_permission)
        .where(p.contact_permission.c.party_ref == reference)
        .values(disclosure_version=None, withdrawn_reason=None)
    )
    uow.connection.execute(
        update(p.contact_suppression)
        .where(p.contact_suppression.c.party_ref == reference)
        .values(reason=None)
    )
    return NOTHING_REMOVED
