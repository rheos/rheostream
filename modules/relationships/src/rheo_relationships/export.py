"""All six tables restore into an empty module, preserving merge ownership history."""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Date, DateTime, Integer, Numeric, Text, Uuid, select, update
from sqlalchemy.dialects.postgresql import ARRAY

from rheo_relationships.storage import tables as t

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot

TABLES = (
    t.party,
    t.contact_point,
    t.affiliation,
    t.merge_record,
    t.merge_record_item,
    t.review_candidate,
)


def export_records(snapshot: "ExportSnapshot") -> Sequence[Mapping[str, object]]:
    return [
        {"record_type": f"relationships.{table.name}", **dict(row)}
        for table in TABLES
        for row in snapshot.connection.execute(
            select(table).order_by(*table.primary_key.columns)
        ).mappings()
    ]


def import_records(
    uow: UnitOfWork, rows: Sequence[Mapping[str, object]]
) -> Callable[[], None]:
    grouped: dict[str, list[dict[str, Any]]] = {table.name: [] for table in TABLES}
    by_type = {f"relationships.{table.name}": table for table in TABLES}
    try:
        for row in rows:
            table = by_type[str(row["record_type"])]
            if set(row) != {"record_type", *table.columns.keys()}:
                raise ValueError
            values: dict[str, Any] = {}
            for column in table.columns:
                value = row[column.name]
                if value is None:
                    if not column.nullable:
                        raise ValueError
                elif isinstance(column.type, Uuid):
                    value = UUID(str(value))
                elif isinstance(column.type, DateTime):
                    value = datetime.fromisoformat(str(value))
                    if value.tzinfo is None:
                        raise ValueError
                elif isinstance(column.type, Date):
                    value = date.fromisoformat(str(value))
                elif isinstance(column.type, Integer):
                    if type(value) is not int:
                        raise ValueError
                elif isinstance(column.type, Numeric):
                    value = Decimal(str(value))
                    if not value.is_finite():
                        raise ValueError
                elif isinstance(column.type, ARRAY):
                    if not isinstance(value, (list, tuple)) or any(
                        not isinstance(v, str) for v in value
                    ):
                        raise ValueError
                elif isinstance(column.type, Text) and not isinstance(value, str):
                    raise ValueError
                values[column.name] = value
            grouped[table.name].append(values)
        parties = {row["id"]: row for row in grouped["party"]}
        for party in parties.values():
            target = party["merged_into_id"]
            if target and (
                target == party["id"]
                or target not in parties
                or parties[target]["merged_into_id"] is not None
                or parties[target]["kind"] != party["kind"]
            ):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OperationRefused(
            "artifact_invalid", "invalid relationships export"
        ) from None
    for table in TABLES:
        if uow.connection.execute(select(table).limit(1)).first():
            raise OperationRefused(
                "restore_target_not_empty",
                "relationships restore requires an empty module",
            )
    pointers = []
    for row in grouped["party"]:
        if row["merged_into_id"]:
            pointers.append((row["id"], row["merged_into_id"]))
            row["merged_into_id"] = None
    for table in TABLES:
        if grouped[table.name]:
            uow.connection.execute(table.insert(), grouped[table.name])

    def finish() -> None:
        for source, target in pointers:
            uow.connection.execute(
                update(t.party)
                .where(t.party.c.id == source)
                .values(merged_into_id=target)
            )

    return finish
