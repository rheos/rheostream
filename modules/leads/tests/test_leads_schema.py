"""Intake's schema contract before the install chain is introduced."""

from rheo_leads.storage import tables as t
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_intake_owns_only_its_thirteen_tables() -> None:
    assert {table.name for table in t.metadata.tables.values()} == {
        "funnel",
        "campaign",
        "intake_connection",
        "connection_health",
        "field_mapping",
        "field_mapping_identity",
        "field_mapping_rule",
        "delivery_receipt",
        "delivery_payload",
        "delivery_conflict",
        "observation",
        "observation_field",
        "import_batch",
    }
    for table in t.metadata.tables.values():
        assert table.schema == "leads"
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.schema == "leads"


def test_receipt_and_observation_uniqueness_are_database_constraints() -> None:
    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys()) == {"connection_id", "source_event_id"}
        for constraint in t.delivery_receipt.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and list(constraint.columns.keys()) == ["receipt_id"]
        for constraint in t.observation.constraints
    )
    assert list(t.delivery_payload.primary_key.columns.keys()) == ["receipt_id"]


def test_erased_conflicts_cannot_hold_bodies() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in t.delivery_conflict.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert (
        checks["erased_conflict_no_body"]
        == "kind <> 'deleted_observation' OR body IS NULL"
    )
