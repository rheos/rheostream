"""Export format 1: every Current table, restored into a freshly installed module.

A fresh install already holds the seeded areas, so restore replaces the area list
with the exported one; every other table must be empty.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Date, DateTime, Integer, Text, Uuid, delete, select

from rheo_current.storage import tables as t

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot

TABLES = (t.area, t.project, t.item, t.comment, t.signal, t.dependency)
"""Parents first, which is also the insert order."""


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
