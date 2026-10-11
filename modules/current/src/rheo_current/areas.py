"""The workspace's area list: one area per item or project, edited by the owner.

A table rather than a settings key: the settings system stores strings, integers,
booleans and string lists, and an area needs a stable key, a label that can change,
an order and a retirement time. Items hold the key under a foreign key, so a rename
reaches every item at once and a retired area stays readable on the items that
carry it. Areas are retired, never deleted.
"""

from typing import Any

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import func, insert, select, update

from rheo_current import common as h
from rheo_current import contracts as c
from rheo_current.storage import tables as t

DEFAULT_AREAS = (
    ("work", "Work"),
    ("commerce", "Commerce"),
    ("bills", "Bills"),
    ("peace", "Peace"),
    ("education", "Education"),
    ("dev", "Dev"),
    ("interior", "Interior"),
    ("foreign", "Foreign affairs"),
)
"""Seeded by migration 0001; the migration holds its own frozen copy."""


def output(row: dict[str, Any]) -> c.Area:
    return c.Area(
        key=row["key"],
        label=row["label"],
        position=row["position"],
        retired=row["retired_at"] is not None,
    )


def read(uow: UnitOfWork, key: str) -> c.Area:
    found = (
        uow.connection.execute(select(t.area).where(t.area.c.key == key))
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise h.refuse("not_found", "area unavailable")
    return output(dict(found))


def listing(ctx: WorkspaceContext, uow: UnitOfWork, data: c.AreaList) -> c.AreaPage:
    query = select(t.area)
    if not data.include_retired:
        query = query.where(t.area.c.retired_at.is_(None))
    rows = uow.connection.execute(query.order_by(t.area.c.position, t.area.c.key))
    return c.AreaPage(items=[output(dict(r)) for r in rows.mappings()])


def create(ctx: WorkspaceContext, uow: UnitOfWork, data: c.AreaCreate) -> c.Area:
    h.lock(uow)
    if uow.connection.execute(
        select(t.area.c.key).where(t.area.c.key == data.key)
    ).first():
        raise h.refuse("area_exists", "an area with that key exists")
    position: int = uow.connection.execute(
        select(func.coalesce(func.max(t.area.c.position), 0))
    ).scalar_one()
    moment = h.now()
    uow.connection.execute(
        insert(t.area).values(
            key=data.key,
            label=data.label,
            position=position + 1,
            created_at=moment,
            updated_at=moment,
            retired_at=None,
        )
    )
    return read(uow, data.key)


def edit(ctx: WorkspaceContext, uow: UnitOfWork, data: c.AreaUpdate) -> c.Area:
    """Rename, retire or restore. Retiring keeps every item's value as it is."""
    h.lock(uow)
    current = read(uow, data.key)
    changes: dict[str, object] = {}
    if data.label is not None:
        changes["label"] = data.label
    if data.retired is not None and data.retired != current.retired:
        changes["retired_at"] = h.now() if data.retired else None
    if changes:
        uow.connection.execute(
            update(t.area)
            .where(t.area.c.key == data.key)
            .values(updated_at=h.now(), **changes)
        )
    return read(uow, data.key)
