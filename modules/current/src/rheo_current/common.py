"""Shared lookups, the write lock, compare-and-set and event publication.

Every write takes the workspace lifecycle lock first, as Relationships and Leads do.
That orders source dedup and the dependency cycle check against each other and
against approved deletion, so neither needs its own retry loop. Reads never take it.
No handler commits: dispatch owns the transaction and the audit boundary.
"""

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from rheo_contracts import ALL_OPERATIONS, StaleRecord, WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.events import NewEvent, publish
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations import CONSUMERS_MISSING
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import Table, select, update

from rheo_current.contracts import ItemEvent
from rheo_current.references import ref
from rheo_current.storage import tables as t

READ_ITEM: Final = "current.item.get"


def now() -> datetime:
    return datetime.now(UTC)


def refuse(state: str, detail: str | None = None) -> OperationRefused:
    return OperationRefused(state, detail or state.replace("_", " "))


def lock(uow: UnitOfWork) -> None:
    lock_workspace_lifecycle(uow.connection)


def reads_items(ctx: WorkspaceContext) -> bool:
    """Whether the caller may also run ``current.item.get``, so a write may hand the
    item back to it.

    The operation set is the only check needed: get's roles include every role that
    may create or close, and it lives in this same module. A writer token's set holds
    create and close by source without get, so its results carry no item content and
    close by source does not say which item, if any, holds a pair.
    """
    permitted = ctx.operation_set
    return permitted is ALL_OPERATIONS or (
        isinstance(permitted, frozenset) and READ_ITEM in permitted
    )


def row(uow: UnitOfWork, table: Table, identifier: UUID) -> dict[str, Any]:
    found = (
        uow.connection.execute(select(table).where(table.c.id == identifier))
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise refuse("not_found", f"{table.name} unavailable")
    return dict(found)


def checked(
    uow: UnitOfWork, table: Table, identifier: UUID, revision: int
) -> dict[str, Any]:
    found = row(uow, table, identifier)
    if found["revision"] != revision:
        raise StaleRecord(f"{table.name} changed")
    return found


def write(
    uow: UnitOfWork, table: Table, record: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    """Compare-and-set on the revision the caller read; bumps it and updated_at."""
    values = {**changes, "revision": record["revision"] + 1, "updated_at": now()}
    found = uow.connection.execute(
        update(table)
        .where(table.c.id == record["id"], table.c.revision == record["revision"])
        .values(**values)
        .returning(table.c.id)
    ).scalar_one_or_none()
    if found is None:
        raise StaleRecord(f"{table.name} changed")
    return {**record, **values}


def usable_area(uow: UnitOfWork, key: str | None, current: str | None = None) -> None:
    """A new value must be a live area. Keeping the value already held is allowed even
    after that area was retired, so a retirement never blocks an unrelated edit."""
    if key is None or key == current:
        return
    area = (
        uow.connection.execute(select(t.area).where(t.area.c.key == key))
        .mappings()
        .one_or_none()
    )
    if area is None:
        raise refuse("area_unknown", "no such area")
    if area["retired_at"] is not None:
        raise refuse("area_retired", "area is retired")


def actor(ctx: WorkspaceContext, prefix: str) -> dict[str, Any]:
    return {f"{prefix}_kind": ctx.actor.kind.value, f"{prefix}_id": ctx.actor.id}


def publish_item_event(
    ctx: WorkspaceContext, uow: UnitOfWork, verb: str, record: dict[str, Any]
) -> None:
    if not isinstance(uow, HandlerUnitOfWork) or uow.consumers is None:
        raise OperationRefused(CONSUMERS_MISSING, "consumer registry is required")
    reference = ref("item", record["id"])
    publish(
        ctx,
        uow,
        NewEvent(
            type="current.item." + verb,
            schema_version=1,
            subject_ref=reference,
            subject_revision=record["revision"],
            data=ItemEvent(ref=reference, revision=record["revision"]).model_dump(
                mode="json"
            ),
        ),
        now=now(),
        consumers=uow.consumers,
    )
