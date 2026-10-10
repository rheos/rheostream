"""Version-three intake and pipeline snapshot; restore preserves identities.

Signing secret handles are deployment-local and are never exported. Imported
webhook connections need new credentials. Restore replaces only the fresh seed;
existing intake records cause a refusal, never a destructive merge.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID

from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    Uuid,
    delete,
    func,
    insert,
    select,
)

from rheo_leads.storage import pipeline as _pipeline  # noqa: F401
from rheo_leads.storage import tables as t

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot

TABLES = tuple(t.metadata.sorted_tables)
SECRET_COLUMNS = {"signing_secret_ref", "previous_secret_ref", "previous_valid_until"}


def export_records(snapshot: "ExportSnapshot") -> Sequence[Mapping[str, object]]:
    """Export all owned tables in foreign-key order."""
    rows: list[Mapping[str, object]] = []
    for table in TABLES:
        for row in snapshot.connection.execute(
            select(table).order_by(*table.primary_key.columns)
        ).mappings():
            values = dict(row)
            for column in table.columns:
                value = values[column.name]
                if column.name in SECRET_COLUMNS:
                    values[column.name] = None
                elif isinstance(value, bytes):
                    values[column.name] = value.hex()
            rows.append({"record_type": f"leads.{table.name}", **values})
    return rows


def _decode(rows: Sequence[Mapping[str, object]]) -> dict[str, list[dict[str, Any]]]:
    """Reject unknown fields and validate scalar types before issuing any write."""
    grouped: dict[str, list[dict[str, Any]]] = {table.name: [] for table in TABLES}
    by_type = {f"leads.{table.name}": table for table in TABLES}
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
                elif isinstance(column.type, LargeBinary):
                    value = bytes.fromhex(str(value))
                elif isinstance(column.type, Numeric):
                    value = Decimal(str(value))
                    if not value.is_finite():
                        raise ValueError
                elif isinstance(column.type, ARRAY):
                    if not isinstance(value, list) or any(
                        not isinstance(v, str) for v in value
                    ):
                        raise ValueError
                elif isinstance(column.type, JSON):
                    if not isinstance(value, dict):
                        raise ValueError
                elif isinstance(column.type, Boolean):
                    if not isinstance(value, bool):
                        raise ValueError
                elif isinstance(column.type, Integer):
                    if type(value) is not int:
                        raise ValueError
                elif not isinstance(value, str):
                    raise ValueError
                if column.name in SECRET_COLUMNS:
                    if value is not None:
                        raise ValueError
                values[column.name] = value
            if (
                table is t.intake_connection
                and values["transport"] == "webhook"
                and values["state"] == "active"
            ):
                values["state"] = "needs_credential"
            grouped[table.name].append(values)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        raise OperationRefused("artifact_invalid", "invalid Leads intake row") from None
    connections = grouped["intake_connection"]
    if sum(row["transport"] == "manual" for row in connections) != 1:
        raise OperationRefused(
            "artifact_invalid", "exactly one manual connection is required"
        )
    # The deletion participant locates the retained receipt after owner deletion.
    if any(row["id"] != row["receipt_id"] for row in grouped["observation"]):
        raise OperationRefused(
            "artifact_invalid", "observation identity differs from receipt"
        )
    erased = {
        row["id"]
        for row in grouped["delivery_receipt"]
        if row["state_detail"] == "observation_deleted"
    }
    if any(row["receipt_id"] in erased for row in grouped["delivery_payload"]) or any(
        row["receipt_id"] in erased and row["body"] is not None
        for row in grouped["delivery_conflict"]
    ):
        raise OperationRefused("artifact_invalid", "erased intake contains payload")
    return grouped


def import_records(
    uow: UnitOfWork, rows: Sequence[Mapping[str, object]]
) -> Callable[[], None]:
    """Reconcile a fresh seed before inserting the preserved source identities."""
    grouped = _decode(rows)
    counts = {
        table.name: uow.connection.execute(
            select(func.count()).select_from(table)
        ).scalar_one()
        for table in TABLES
    }
    expected = {table.name: 0 for table in TABLES}
    expected.update(
        funnel=1,
        field_mapping=1,
        field_mapping_identity=1,
        field_mapping_rule=13,
        intake_connection=1,
        connection_health=1,
        pipeline_preset=1,
        preset_version=1,
        preset_stage=9,
        preset_transition=23,
        preset_requirement=0,
        preset_field=0,
        preset_rubric=1,
        preset_template=1,
    )
    if counts != expected:
        raise OperationRefused(
            "restore_target_not_empty", "Leads restore requires a fresh seed"
        )
    manual = uow.connection.execute(select(t.intake_connection)).mappings().one()
    if (
        manual["transport"] != "manual"
        or manual["source_namespace"] != "manual_capture"
    ):
        raise OperationRefused(
            "restore_target_not_empty", "Leads restore requires a fresh seed"
        )
    # All content tables were proved empty; only the generated seed is removed.
    for table in reversed(TABLES):
        uow.connection.execute(delete(table))
    for table in TABLES:
        if grouped[table.name]:
            uow.connection.execute(insert(table), grouped[table.name])

    def finish() -> None:
        """No cross-module references are materialized in the intake-only release."""

    return finish
