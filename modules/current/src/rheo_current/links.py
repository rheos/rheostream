"""The lasting source link: its lifecycle, its one change event and its public read.

A link row exists only for an item the approved external destination made (see
:mod:`rheo_current.external`). Every later change arrives through a path Current
already owns, each under the workspace lifecycle lock its caller took:

- ``current.item.update`` (:func:`rheo_current.items.edit`) assigns, moves or clears
  the item's project, the one place a project association changes;
- approved item deletion (:func:`rheo_current.lifecycle.delete_owned`) marks the item
  deleted and leaves its project as it was;
- approved project deletion moves the project to history on every link holding it.

Each real change advances the link's single ``revision`` by one and publishes one
``current.source_item_link.changed`` event carrying the full link. Repeating the same
assignment, or clearing a project that is already empty, changes nothing. No path
here rewrites ``item_ref``. Nothing here names or imports another module.
"""

from typing import Any, Final

from rheo_contracts import WorkspaceContext
from rheo_core.events import NewEvent, publish
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations import CONSUMERS_MISSING
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import select, update

from rheo_current import common as h
from rheo_current import contracts as c
from rheo_current.storage import tables as t

CHANGED: Final = "current.source_item_link.changed"
CHANGED_SCHEMA_VERSION: Final = 1
link = t.source_item_link


def output(row: Any) -> c.SourceItemLink:
    return c.SourceItemLink(
        source_ref=row["source_ref"],
        item_ref=row["item_ref"],
        project_ref=row["project_ref"],
        last_project_ref=row["last_project_ref"],
        item_state=row["item_state"],
        project_state=row["project_state"],
        revision=row["revision"],
        changed_at=row["changed_at"],
    )


def _for_item(uow: UnitOfWork, item_ref: str) -> dict[str, Any] | None:
    found = (
        uow.connection.execute(
            select(link).where(link.c.item_ref == item_ref).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    return None if found is None else dict(found)


def _advance(
    ctx: WorkspaceContext, uow: UnitOfWork, row: dict[str, Any], **changes: Any
) -> None:
    """Write one change at the next revision and publish it, in the caller's
    transaction. The revision compare is a backstop: every caller holds the lock."""
    if not isinstance(uow, HandlerUnitOfWork) or uow.consumers is None:
        raise OperationRefused(CONSUMERS_MISSING, "consumer registry is required")
    values = {**changes, "revision": row["revision"] + 1, "changed_at": h.now()}
    written = (
        uow.connection.execute(
            update(link)
            .where(
                link.c.source_ref == row["source_ref"],
                link.c.revision == row["revision"],
            )
            .values(**values)
            .returning(link)
        )
        .mappings()
        .one_or_none()
    )
    if written is None:  # pragma: no cover - the lifecycle lock already orders writers
        raise OperationRefused("record_stale", "source link changed")
    changed = output(written)
    publish(
        ctx,
        uow,
        NewEvent(
            type=CHANGED,
            schema_version=CHANGED_SCHEMA_VERSION,
            # The item reference is fixed for the link's life, so one subject keeps
            # every consumer's deliveries for this link in revision order.
            subject_ref=changed.item_ref,
            subject_revision=changed.revision,
            data=changed.model_dump(mode="json"),
        ),
        now=h.now(),
        consumers=uow.consumers,
    )


def item_project_set(
    ctx: WorkspaceContext, uow: UnitOfWork, item_ref: str, project_ref: str | None
) -> None:
    """The item's project is now ``project_ref``: assign, move, clear or nothing."""
    row = _for_item(uow, item_ref)
    if row is None:
        return
    live = row["project_ref"]
    if project_ref == live:
        return  # the same assignment, or a clear of an already-empty project
    if project_ref is None:
        _advance(
            ctx,
            uow,
            row,
            project_ref=None,
            last_project_ref=live,
            project_state="cleared",
        )
        return
    # Assign keeps the history it had; a move records the project it left.
    _advance(
        ctx,
        uow,
        row,
        project_ref=project_ref,
        last_project_ref=row["last_project_ref"] if live is None else live,
        project_state="linked",
    )


def item_deleted(ctx: WorkspaceContext, uow: UnitOfWork, item_ref: str) -> None:
    """Only the item's state changes: a linked project stays live and readable."""
    row = _for_item(uow, item_ref)
    if row is None or row["item_state"] == "deleted":
        return
    _advance(ctx, uow, row, item_state="deleted")


def project_deleted(ctx: WorkspaceContext, uow: UnitOfWork, project_ref: str) -> None:
    """Every link still holding the project keeps it as history, deleted.

    Links whose item is already deleted are included: that is how a project that
    outlived its item is recorded when it goes too.
    """
    rows = [
        dict(row)
        for row in uow.connection.execute(
            select(link)
            .where(link.c.project_ref == project_ref)
            .order_by(link.c.source_ref)
            .with_for_update()
        ).mappings()
    ]
    for row in rows:
        _advance(
            ctx,
            uow,
            row,
            project_ref=None,
            last_project_ref=project_ref,
            project_state="deleted",
        )


def state(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.SourceItemLinkInput
) -> c.SourceItemLink:
    """``current.item.link_state``: the link alone, never the item or project.

    It reads no item or project row, so it answers the same whether the item exists,
    was deleted, or is one the caller may not read. Dispatch has already applied the
    caller's own workspace, role, operation set and module readiness to this one
    operation; item and project content stay behind their own reads.
    """
    found = (
        uow.connection.execute(select(link).where(link.c.source_ref == data.source_ref))
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise h.refuse("not_found", "source link unavailable")
    return output(found)
