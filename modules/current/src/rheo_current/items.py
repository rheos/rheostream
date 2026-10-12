"""Work items: create with source dedup, edit, status, comments and dependencies."""

from datetime import datetime
from typing import Any
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)

from rheo_current import common as h
from rheo_current import contracts as c
from rheo_current.references import ref
from rheo_current.storage import tables as t

PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
TRANSITIONS = {
    "open": {"waiting", "snoozed", "done"},
    "waiting": {"open", "snoozed", "done"},
    "snoozed": {"open", "waiting", "done"},
    "done": {"open"},
}
blocker_item = t.item.alias("blocker_item")


def search_vector(*columns: ColumnElement[str]) -> ColumnElement[Any]:
    text_value: ColumnElement[Any] = columns[0]
    for column in columns[1:]:
        text_value = text_value.op("||")(t.SPACE).op("||")(column)
    return func.to_tsvector(t.ENGLISH, text_value)


def unfinished_blocker(item_id: ColumnElement[Any]) -> ColumnElement[bool]:
    return exists(
        select(t.dependency.c.blocker_id)
        .join(blocker_item, blocker_item.c.id == t.dependency.c.blocker_id)
        .where(t.dependency.c.item_id == item_id, blocker_item.c.status != "done")
    )


def active(moment: datetime) -> ColumnElement[bool]:
    """Open and waiting items, and snoozed items whose time has come."""
    return or_(
        t.item.c.status.in_(("open", "waiting")),
        and_(t.item.c.status == "snoozed", t.item.c.snoozed_until <= moment),
    )


def summary_query() -> Select[Any]:
    return select(t.item, unfinished_blocker(t.item.c.id).label("blocked"))


def summary(row: Any, moment: datetime) -> c.ItemSummary:
    return c.ItemSummary(
        ref=ref("item", row["id"]),
        title=row["title"],
        status=row["status"],
        priority=row["priority"],
        area=row["area"],
        project_ref=ref("project", row["project_id"]) if row["project_id"] else None,
        due_on=row["due_on"],
        snoozed_until=row["snoozed_until"],
        woke=row["status"] == "snoozed" and row["snoozed_until"] <= moment,
        overdue=row["status"] != "done"
        and row["due_on"] is not None
        and row["due_on"] < moment.date(),
        blocked=bool(row["blocked"]),
        event_count=row["event_count"],
        revision=row["revision"],
        updated_at=row["updated_at"],
    )


def neighbours(uow: UnitOfWork, column: Any, other: Any, identifier: UUID) -> list[Any]:
    rows = uow.connection.execute(
        select(t.item.c.id, t.item.c.title, t.item.c.status)
        .join(t.dependency, other == t.item.c.id)
        .where(column == identifier, t.item.c.status != "done")
        .order_by(t.item.c.id)
    ).mappings()
    return [
        c.Blocker(ref=ref("item", r["id"]), title=r["title"], status=r["status"])
        for r in rows
    ]


def detail(uow: UnitOfWork, identifier: UUID) -> c.ItemDetail:
    found = (
        uow.connection.execute(summary_query().where(t.item.c.id == identifier))
        .mappings()
        .one_or_none()
    )
    if found is None:
        raise h.refuse("not_found", "item unavailable")
    moment = h.now()
    project = None
    if found["project_id"] is not None:
        p = h.row(uow, t.project, found["project_id"])
        project = c.ProjectSummary(
            ref=ref("project", p["id"]), title=p["title"], status=p["status"]
        )
    comments = [
        c.Comment(**dict(r))
        for r in uow.connection.execute(
            select(
                t.comment.c.id,
                t.comment.c.kind,
                t.comment.c.body,
                t.comment.c.author_kind,
                t.comment.c.author_id,
                t.comment.c.created_at,
            )
            .where(t.comment.c.item_id == identifier)
            .order_by(t.comment.c.created_at, t.comment.c.id)
        ).mappings()
    ]
    return c.ItemDetail(
        **summary(found, moment).model_dump(),
        body=found["body"],
        provenance=found["provenance"],
        source_kind=found["source_kind"],
        source_ref=found["source_ref"],
        created_at=found["created_at"],
        closed_at=found["closed_at"],
        project=project,
        blockers=neighbours(
            uow, t.dependency.c.item_id, t.dependency.c.blocker_id, identifier
        ),
        blocking=neighbours(
            uow, t.dependency.c.blocker_id, t.dependency.c.item_id, identifier
        ),
        comments=comments,
    )


def add_comment(
    ctx: WorkspaceContext, uow: UnitOfWork, item_id: UUID, body: str, kind: str
) -> None:
    """The author is always the authenticated actor; no input names one."""
    uow.connection.execute(
        insert(t.comment).values(
            id=uuid7(),
            item_id=item_id,
            kind=kind,
            body=body,
            created_at=h.now(),
            **h.actor(ctx, "author"),
        )
    )


def project_id(uow: UnitOfWork, reference: Any) -> UUID | None:
    if reference is None:
        return None
    identifier: UUID = h.row(uow, t.project, reference.id)["id"]
    return identifier


def higher(current: str, offered: str) -> str:
    return offered if PRIORITY_RANK[offered] < PRIORITY_RANK[current] else current


# --- create and source dedup -----------------------------------------------------


def repeat_signal(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    record: dict[str, Any],
    data: c.ItemCreate,
) -> c.ItemCreated:
    """Fold a repeat source signal into the item already holding that source.

    Title, body, area and project are never touched: the item is the person's now.
    """
    identifier = record["id"]
    if data.fingerprint is not None:
        seen = uow.connection.execute(
            select(t.signal.c.fingerprint).where(
                t.signal.c.item_id == identifier,
                t.signal.c.fingerprint == data.fingerprint,
            )
        ).first()
        if seen is not None:
            uow.connection.execute(
                update(t.item)
                .where(t.item.c.id == identifier)
                .values(updated_at=h.now())
            )
            return c.ItemCreated(action="grouped", item=detail(uow, identifier))
        uow.connection.execute(
            insert(t.signal).values(
                item_id=identifier, fingerprint=data.fingerprint, seen_at=h.now()
            )
        )
    count = record["event_count"] + 1
    changes: dict[str, Any] = {
        "event_count": count,
        "priority": higher(record["priority"], data.priority),
    }
    action: c.CreateAction
    if record["status"] == "done":
        action, wording = "reopened", "Reopened by"
        changes.update(status="open", closed_at=None)
    elif record["status"] == "snoozed":
        action, wording = "woke", "Woken by"
        changes.update(status="open", snoozed_until=None)
    else:
        action, wording = "grouped", "Grouped"
    updated = h.write(uow, t.item, record, **changes)
    add_comment(
        ctx,
        uow,
        identifier,
        f"{wording} a repeat {record['source_kind']} signal (event {count}).",
        "system",
    )
    if action == "reopened":
        h.publish_item_event(ctx, uow, "reopened", updated)
    return c.ItemCreated(action=action, item=detail(uow, identifier))


def create(ctx: WorkspaceContext, uow: UnitOfWork, data: c.ItemCreate) -> c.ItemCreated:
    h.lock(uow)
    if data.source_ref is not None:
        existing = (
            uow.connection.execute(
                select(t.item)
                .where(
                    t.item.c.source_kind == data.source_kind,
                    t.item.c.source_ref == data.source_ref,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return repeat_signal(ctx, uow, dict(existing), data)
    h.usable_area(uow, data.area)
    identifier = uuid7()
    moment = h.now()
    record = {
        "id": identifier,
        "title": data.title,
        "body": data.body,
        "status": "open",
        "priority": data.priority,
        "area": data.area,
        "project_id": project_id(uow, data.project_ref),
        "due_on": data.due_on,
        "snoozed_until": None,
        "provenance": data.provenance,
        "source_kind": data.source_kind,
        "source_ref": data.source_ref,
        "event_count": 1,
        "revision": 1,
        "created_at": moment,
        "updated_at": moment,
        "closed_at": None,
    }
    uow.connection.execute(insert(t.item).values(**record))
    if data.fingerprint is not None:
        uow.connection.execute(
            insert(t.signal).values(
                item_id=identifier, fingerprint=data.fingerprint, seen_at=moment
            )
        )
    h.publish_item_event(ctx, uow, "created", record)
    return c.ItemCreated(action="created", item=detail(uow, identifier))


# --- reads -----------------------------------------------------------------------


def get(ctx: WorkspaceContext, uow: UnitOfWork, data: c.ItemInput) -> c.ItemDetail:
    return detail(uow, data.ref.id)


def listing(ctx: WorkspaceContext, uow: UnitOfWork, data: c.ItemList) -> c.ItemPage:
    moment = h.now()
    today = moment.date()
    query = summary_query()
    if data.status == "active":
        query = query.where(active(moment))
    elif data.status != "all":
        query = query.where(t.item.c.status == data.status)
    if data.area is not None:
        query = query.where(t.item.c.area == data.area)
    if data.project_ref is not None:
        query = query.where(t.item.c.project_id == data.project_ref.id)
    if data.priority is not None:
        query = query.where(t.item.c.priority == data.priority)
    if data.due_before is not None:
        query = query.where(t.item.c.due_on <= data.due_before)
    if data.overdue is not None:
        # The null check keeps the predicate two-valued, so its negation keeps
        # unfinished items that have no due date.
        late = and_(
            t.item.c.status != "done",
            t.item.c.due_on.is_not(None),
            t.item.c.due_on < today,
        )
        query = query.where(late if data.overdue else ~late)
    if data.woke is not None:
        woke = and_(t.item.c.status == "snoozed", t.item.c.snoozed_until <= moment)
        query = query.where(woke if data.woke else ~woke)
    if data.blocked is not None:
        blocked = unfinished_blocker(t.item.c.id)
        query = query.where(blocked if data.blocked else ~blocked)
    if data.query is not None:
        terms = func.websearch_to_tsquery(t.ENGLISH, data.query)
        in_comments = exists(
            select(t.comment.c.id).where(
                t.comment.c.item_id == t.item.c.id,
                search_vector(t.comment.c.body).bool_op("@@")(terms),
            )
        )
        query = query.where(
            or_(
                search_vector(t.item.c.title, t.item.c.body).bool_op("@@")(terms),
                in_comments,
            )
        )
    rank = case(PRIORITY_RANK, value=t.item.c.priority)
    rows = list(
        uow.connection.execute(
            query.order_by(rank, t.item.c.updated_at.desc(), t.item.c.id)
            .limit(data.limit + 1)
            .offset(data.offset)
        ).mappings()
    )
    more = len(rows) > data.limit
    return c.ItemPage(
        items=[summary(r, moment) for r in rows[: data.limit]],
        next_offset=data.offset + data.limit if more else None,
    )


def summarize(ctx: WorkspaceContext, uow: UnitOfWork, data: c.Empty) -> c.Summary:
    moment = h.now()
    by_status = {status: 0 for status in TRANSITIONS}
    for status, n in uow.connection.execute(
        select(t.item.c.status, func.count()).group_by(t.item.c.status)
    ):
        by_status[status] = int(n)

    def count(*conditions: ColumnElement[bool]) -> int:
        return int(
            uow.connection.execute(
                select(func.count()).select_from(t.item).where(*conditions)
            ).scalar_one()
        )

    return c.Summary(
        by_status=by_status,
        active=count(active(moment)),
        woke=count(t.item.c.status == "snoozed", t.item.c.snoozed_until <= moment),
        overdue=count(t.item.c.status != "done", t.item.c.due_on < moment.date()),
        blocked=count(active(moment), unfinished_blocker(t.item.c.id)),
        by_area=[
            c.AreaCount(area=area, active=int(n))
            for area, n in uow.connection.execute(
                select(t.item.c.area, func.count())
                .where(active(moment))
                .group_by(t.item.c.area)
                .order_by(t.item.c.area.nulls_last())
            )
        ],
    )


# --- writes ----------------------------------------------------------------------


def edit(ctx: WorkspaceContext, uow: UnitOfWork, data: c.ItemUpdate) -> c.ItemDetail:
    h.lock(uow)
    record = h.checked(uow, t.item, data.ref.id, data.revision)
    changes: dict[str, Any] = {
        name: getattr(data, name)
        for name in ("title", "body", "priority", "area", "due_on")
        if getattr(data, name) is not None
    }
    if data.project_ref is not None:
        changes["project_id"] = project_id(uow, data.project_ref)
    if "area" in changes:
        h.usable_area(uow, data.area, record["area"])
    for name in data.clear:
        changes["project_id" if name == "project_ref" else name] = None
    if changes:
        h.write(uow, t.item, record, **changes)
    return detail(uow, data.ref.id)


def set_status(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.ItemStatusInput
) -> c.ItemDetail:
    h.lock(uow)
    record = h.checked(uow, t.item, data.ref.id, data.revision)
    moment = h.now()
    if data.status == "snoozed":
        if data.snoozed_until is None or data.snoozed_until <= moment:
            raise h.refuse("snooze_invalid", "snoozing needs a future snoozed_until")
    elif data.snoozed_until is not None:
        raise h.refuse("snooze_invalid", "snoozed_until applies only to snoozed")
    current = record["status"]
    if data.status == current and current != "snoozed":
        return detail(uow, record["id"])
    if data.status != current and data.status not in TRANSITIONS[current]:
        raise h.refuse(
            "transition_not_allowed", f"a {current} item cannot become {data.status}"
        )
    changes: dict[str, Any] = {
        "status": data.status,
        "snoozed_until": data.snoozed_until,
        "closed_at": moment if data.status == "done" else None,
    }
    updated = h.write(uow, t.item, record, **changes)
    if data.status == "done":
        h.publish_item_event(ctx, uow, "completed", updated)
    elif current == "done":
        h.publish_item_event(ctx, uow, "reopened", updated)
    return detail(uow, record["id"])


def comment(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.CommentInput
) -> c.ItemDetail:
    h.lock(uow)
    h.row(uow, t.item, data.ref.id)
    add_comment(ctx, uow, data.ref.id, data.body, "comment")
    uow.connection.execute(
        update(t.item).where(t.item.c.id == data.ref.id).values(updated_at=h.now())
    )
    return detail(uow, data.ref.id)


def reaches(uow: UnitOfWork, start: UUID, target: UUID) -> bool:
    """Whether ``start`` is already blocked, directly or transitively, by ``target``."""
    d = t.dependency
    chain = select(d.c.blocker_id.label("id")).where(d.c.item_id == start)
    chain_cte = chain.cte("chain", recursive=True)
    chain_cte = chain_cte.union(
        select(d.c.blocker_id).join(chain_cte, d.c.item_id == chain_cte.c.id)
    )
    found = uow.connection.execute(
        select(chain_cte.c.id).where(chain_cte.c.id == target).limit(1)
    ).first()
    return found is not None


def add_dependency(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.DependencyInput
) -> c.ItemDetail:
    h.lock(uow)
    blocked, blocker = data.item_ref.id, data.blocker_ref.id
    if blocked == blocker:
        raise h.refuse("dependency_cycle", "an item cannot block itself")
    h.row(uow, t.item, blocked)
    h.row(uow, t.item, blocker)
    existing = uow.connection.execute(
        select(t.dependency.c.item_id).where(
            t.dependency.c.item_id == blocked, t.dependency.c.blocker_id == blocker
        )
    ).first()
    if existing is None:
        # The new edge closes a loop when the blocker already waits on this item.
        if reaches(uow, blocker, blocked):
            raise h.refuse("dependency_cycle", "the blocker already waits on this item")
        uow.connection.execute(
            insert(t.dependency).values(
                item_id=blocked,
                blocker_id=blocker,
                created_at=h.now(),
                **h.actor(ctx, "created_by"),
            )
        )
    return detail(uow, blocked)


def remove_dependency(
    ctx: WorkspaceContext, uow: UnitOfWork, data: c.DependencyInput
) -> c.ItemDetail:
    h.lock(uow)
    removed = uow.connection.execute(
        delete(t.dependency)
        .where(
            t.dependency.c.item_id == data.item_ref.id,
            t.dependency.c.blocker_id == data.blocker_ref.id,
        )
        .returning(t.dependency.c.item_id)
    ).first()
    if removed is None:
        raise h.refuse("not_found", "dependency unavailable")
    return detail(uow, data.item_ref.id)
