"""Export format 2: every Current table, restored into a freshly installed module.

A fresh install already holds the seeded areas, so restore replaces the area list
with the exported one; every other table must be empty.

Format 2 adds ``source_item_link`` row for row, revision and change time included, so
a tombstone, a project that outlived its item, and a cleared or deleted project all
come back exactly. A handoff item and its link travel together: restore refuses the
whole artifact when a handoff-kind item has no link, a link names an item that is
missing (or present again after deletion), a live project is missing, or a link
disagrees with its item. A partial restore would either recreate a deleted source's
item or leave an item whose project history no reader could see.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, get_args
from uuid import UUID

from rheo_contracts import RecordRef
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Date, DateTime, Integer, Text, Uuid, delete, select

from rheo_current.contracts import HANDOFF_SOURCE_KIND, ItemState, LinkProjectState
from rheo_current.references import MODULE_ID, ref
from rheo_current.storage import tables as t

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot

TABLES = (
    t.area,
    t.project,
    t.item,
    t.comment,
    t.signal,
    t.dependency,
    t.source_item_link,
)
"""Parents first, which is also the insert order. The link names its item and
projects by reference, with no foreign key, so it can go last."""
ITEM_STATES: frozenset[str] = frozenset(get_args(ItemState))
PROJECT_STATES: frozenset[str] = frozenset(get_args(LinkProjectState))
"""The link contract's own state values, so restore accepts exactly what it does."""


def export_records(snapshot: "ExportSnapshot") -> Sequence[Mapping[str, object]]:
    return [
        {"record_type": f"current.{table.name}", **dict(row)}
        for table in TABLES
        for row in snapshot.connection.execute(
            select(table).order_by(*table.primary_key.columns)
        ).mappings()
    ]


def _value(column: Any, value: object) -> object:
    if value is None:
        if not column.nullable:
            raise ValueError
        return None
    if isinstance(column.type, Uuid):
        return UUID(str(value))
    if isinstance(column.type, DateTime):
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            raise ValueError
        return moment
    if isinstance(column.type, Date):
        return date.fromisoformat(str(value))
    if isinstance(column.type, Integer):
        if type(value) is not int:
            raise ValueError
        return value
    if isinstance(column.type, Text) and not isinstance(value, str):
        raise ValueError
    return value


def _check_references(grouped: dict[str, list[dict[str, Any]]]) -> None:
    areas = {row["key"] for row in grouped["area"]}
    projects = {row["id"] for row in grouped["project"]}
    items = {row["id"] for row in grouped["item"]}
    for row in grouped["project"] + grouped["item"]:
        if row["area"] is not None and row["area"] not in areas:
            raise ValueError
    for row in grouped["item"]:
        if row["project_id"] is not None and row["project_id"] not in projects:
            raise ValueError
    for row in grouped["comment"] + grouped["signal"]:
        if row["item_id"] not in items:
            raise ValueError
    for row in grouped["dependency"]:
        if row["item_id"] not in items or row["blocker_id"] not in items:
            raise ValueError
    _check_links(grouped, projects)


def _reference(value: str | None, kind: str) -> str | None:
    """A canonical Current reference of ``kind``, or a refusal."""
    if value is None:
        return None
    parsed = RecordRef.parse(value)  # canonical spelling only, or a ValueError
    if (parsed.module, parsed.record_type) != (MODULE_ID, kind):
        raise ValueError
    return value


def _check_links(grouped: dict[str, list[dict[str, Any]]], projects: set[UUID]) -> None:
    """The same rules the database checks hold, plus agreement with the items."""
    items = {ref("item", row["id"]): row for row in grouped["item"]}
    live_projects = {ref("project", identifier) for identifier in projects}
    linked_items: set[str] = set()
    for row in grouped["source_item_link"]:
        item_ref = _reference(row["item_ref"], "item")
        project = _reference(row["project_ref"], "project")
        last = _reference(row["last_project_ref"], "project")
        state = row["project_state"]
        if (
            item_ref is None
            or row["revision"] < 1
            or row["item_state"] not in ITEM_STATES
            or state not in PROJECT_STATES
            or (state == "linked") != (project is not None)
            or (state in {"cleared", "deleted"} and last is None)
            or (state == "never_assigned" and last is not None)
            or item_ref in linked_items
        ):
            raise ValueError
        linked_items.add(item_ref)
        if project is not None and project not in live_projects:
            raise ValueError
        item = items.get(item_ref)
        if row["item_state"] == "deleted":
            if item is not None:  # a tombstone's item never comes back
                raise ValueError
            continue
        if (
            item is None
            or item["source_kind"] != HANDOFF_SOURCE_KIND
            or item["source_ref"] != row["source_ref"]
        ):
            raise ValueError
        held = item["project_id"]
        if (None if held is None else ref("project", held)) != project:
            raise ValueError
    for item_ref, item in items.items():
        if item["source_kind"] == HANDOFF_SOURCE_KIND and item_ref not in linked_items:
            raise ValueError


def import_records(
    uow: UnitOfWork, rows: Sequence[Mapping[str, object]]
) -> Callable[[], None]:
    grouped: dict[str, list[dict[str, Any]]] = {table.name: [] for table in TABLES}
    by_type = {f"current.{table.name}": table for table in TABLES}
    try:
        for row in rows:
            table = by_type[str(row["record_type"])]
            if set(row) != {"record_type", *table.columns.keys()}:
                raise ValueError
            grouped[table.name].append(
                {
                    column.name: _value(column, row[column.name])
                    for column in table.columns
                }
            )
        _check_references(grouped)
    except (KeyError, TypeError, ValueError):
        raise OperationRefused("artifact_invalid", "invalid current export") from None
    for table in TABLES[1:]:
        if uow.connection.execute(select(table).limit(1)).first():
            raise OperationRefused(
                "restore_target_not_empty", "current restore requires an empty module"
            )
    uow.connection.execute(delete(t.area))
    for table in TABLES:
        if grouped[table.name]:
            uow.connection.execute(table.insert(), grouped[table.name])

    def finish() -> None:
        """No self-references: every row lands in the first pass."""

    return finish
