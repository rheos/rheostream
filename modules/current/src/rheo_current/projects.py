"""Projects group work items; an item belongs to at most one."""

from typing import Any
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import case, func, insert, select

from rheo_current import common as h
from rheo_current import contracts as c
from rheo_current.references import ref
from rheo_current.storage import tables as t

STATUS_ORDER = {"active": 0, "paused": 1, "done": 2}
ITEM_STATUSES = ("open", "waiting", "snoozed", "done")


def counts(uow: UnitOfWork, identifiers: list[UUID]) -> dict[UUID, dict[str, int]]:
    result = {i: {status: 0 for status in ITEM_STATUSES} for i in identifiers}
    if identifiers:
        for project_id, status, n in uow.connection.execute(
            select(t.item.c.project_id, t.item.c.status, func.count())
            .where(t.item.c.project_id.in_(identifiers))
            .group_by(t.item.c.project_id, t.item.c.status)
        ):
            result[project_id][status] = int(n)
    return result


def output(record: Any, item_counts: dict[str, int]) -> c.ProjectOutput:
    return c.ProjectOutput(
        ref=ref("project", record["id"]),
        title=record["title"],
        description=record["description"],
        status=record["status"],
        area=record["area"],
        due_on=record["due_on"],
        revision=record["revision"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        item_counts=item_counts,
    )


def read(uow: UnitOfWork, identifier: UUID) -> c.ProjectOutput:
    record = h.row(uow, t.project, identifier)
    return output(record, counts(uow, [identifier])[identifier])


def create(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ProjectCreate
) -> c.ProjectOutput:
    h.lock(uow)
    h.usable_area(uow, data.area)
    identifier = uuid7()
    moment = h.now()
    uow.connection.execute(
        insert(t.project).values(
            id=identifier,
            title=data.title,
            description=data.description,
            status=data.status,
            area=data.area,
            due_on=data.due_on,
            revision=1,
            created_at=moment,
            updated_at=moment,
        )
    )
    return read(uow, identifier)


def get(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ProjectInput
) -> c.ProjectOutput:
    return read(uow, data.ref.id)


def listing(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ProjectList
) -> c.ProjectPage:
    query = select(t.project)
    if data.status is not None:
        query = query.where(t.project.c.status == data.status)
    if data.area is not None:
        query = query.where(t.project.c.area == data.area)
    rows = list(
        uow.connection.execute(
            query.order_by(
                case(STATUS_ORDER, value=t.project.c.status),
                t.project.c.title,
                t.project.c.id,
            ).limit(500)
        ).mappings()
    )
    by_project = counts(uow, [r["id"] for r in rows])
    return c.ProjectPage(items=[output(r, by_project[r["id"]]) for r in rows])


def edit(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ProjectUpdate
) -> c.ProjectOutput:
    h.lock(uow)
    record = h.checked(uow, t.project, data.ref.id, data.revision)
    changes: dict[str, Any] = {
        name: getattr(data, name)
        for name in ("title", "description", "status", "area", "due_on")
        if getattr(data, name) is not None
    }
    if "area" in changes:
        h.usable_area(uow, data.area, record["area"])
    for name in data.clear:
        changes[name] = None
    if changes:
        h.write(uow, t.project, record, **changes)
    return read(uow, data.ref.id)
