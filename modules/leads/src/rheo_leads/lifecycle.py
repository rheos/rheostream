"""Erase intake content while keeping the delivery identity tombstone."""

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.deletion import NOTHING_REMOVED, Disposition, RemovedMemories
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, select, update

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
