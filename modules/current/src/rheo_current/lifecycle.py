"""Approved erasure of an item or a project. Nothing outside Current refers to
either yet, so there are no deletion participants."""

from rheo_contracts import RecordRef, Role, StaleRecord, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.deletion import (
    NOTHING_REMOVED,
    DeleteAuthorization,
    Disposition,
    RemovedMemories,
)
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, or_, select, update

from rheo_current import common as h
from rheo_current.references import DELETABLE, MODULE_ID
from rheo_current.storage import tables as t

TABLES = {"item": t.item, "project": t.project}


def authorize_delete(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, *, disposition: Disposition
) -> DeleteAuthorization | Refusal:
    if (
        ref.module != MODULE_ID
        or ref.record_type not in DELETABLE
        or MODULE_ID not in ctx.enabled_modules
        or ctx.role is not Role.OWNER
        or disposition is not Disposition.USER_ERASURE
    ):
        return Refusal("not_found", "record unavailable")
    table = TABLES[ref.record_type]
    revision = uow.connection.execute(
        select(table.c.revision).where(table.c.id == ref.id)
    ).scalar_one_or_none()
    if revision is None:
        return Refusal("not_found", "record unavailable")
    return DeleteAuthorization(ref, revision)


def delete_owned(
    ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
) -> RemovedMemories:
    h.lock(uow)
    reference = authorization.ref
    table = TABLES[reference.record_type]
    current = uow.connection.execute(
        select(table.c.revision).where(table.c.id == reference.id).with_for_update()
    ).scalar_one_or_none()
    if current != authorization.revision:
        raise StaleRecord(f"{reference.record_type} changed since approval")
    if reference.record_type == "item":
        for child in (t.comment, t.signal):
            uow.connection.execute(delete(child).where(child.c.item_id == reference.id))
        uow.connection.execute(
            delete(t.dependency).where(
                or_(
                    t.dependency.c.item_id == reference.id,
                    t.dependency.c.blocker_id == reference.id,
                )
            )
        )
    else:
        # Items outlive their project: each is detached and its revision moves on.
        uow.connection.execute(
            update(t.item)
            .where(t.item.c.project_id == reference.id)
            .values(project_id=None, revision=t.item.c.revision + 1, updated_at=h.now())
        )
    removed = uow.connection.execute(
        delete(table)
        .where(table.c.id == reference.id, table.c.revision == authorization.revision)
        .returning(table.c.id)
    ).scalar_one_or_none()
    if removed is None:
        raise StaleRecord(f"{reference.record_type} changed since approval")
    return NOTHING_REMOVED
