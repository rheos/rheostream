"""Distribution, contract shapes, export schema and boundary checks for Current."""

import ast
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rheo_contracts import SafetyClass
from rheo_current import MANIFEST
from rheo_current.areas import DEFAULT_AREAS
from rheo_current.contracts import (
    DependencyInput,
    ItemCreate,
    ItemInput,
    ItemStatusInput,
    ItemUpdate,
)
from rheo_current.export import TABLES
from sqlalchemy import Date, DateTime, Integer, Table, Text, Uuid

SOURCE = Path(__file__).parents[1] / "src" / "rheo_current"
ITEM = "current.item:00000000-0000-7000-8000-000000000001"


def column_schema(column: Any) -> dict[str, Any]:
    if isinstance(column.type, Uuid):
        schema: dict[str, Any] = {"type": "string", "format": "uuid"}
    elif isinstance(column.type, DateTime):
        schema = {"type": "string", "format": "date-time"}
    elif isinstance(column.type, Date):
        schema = {"type": "string", "format": "date"}
    elif isinstance(column.type, Integer):
        schema = {"type": "integer"}
    elif isinstance(column.type, Text):
        schema = {"type": "string"}
    else:  # pragma: no cover - a new column type must be described here first
        raise AssertionError(column.type)
    return {"anyOf": [schema, {"type": "null"}]} if column.nullable else schema


def export_schema(tables: tuple[Table, ...]) -> dict[str, Any]:
    """The export schema the tables imply; the shipped file must equal it."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "record_type": {"const": f"current.{table.name}"},
                    **{c.name: column_schema(c) for c in table.columns},
                },
                "required": ["record_type", *table.columns.keys()],
            }
            for table in tables
        ],
    }


def test_current_entry_point_and_independence() -> None:
    found = next(
        ep for ep in entry_points(group="rheo.modules") if ep.name == "current"
    )
    assert found.load() is MANIFEST
    assert MANIFEST.dependencies == ()
    assert MANIFEST.web is None
    assert MANIFEST.deletion_participants == ()
    assert MANIFEST.export.format_version == 1
    assert {e.type for e in MANIFEST.events} == {
        "current.item.created",
        "current.item.completed",
        "current.item.reopened",
    }


def test_current_declares_no_external_or_financial_operation() -> None:
    classes = {d.safety_class for d, _ in MANIFEST.operations}
    assert classes <= {SafetyClass.READ, SafetyClass.MUTATE}
    assert all(t.name.startswith("current_") for t in MANIFEST.tools)
    deletes = {t.name for t in MANIFEST.tools if t.operation == "core.record.delete"}
    assert deletes == {"current_delete_item", "current_delete_project"}


def test_export_schema_matches_tables() -> None:
    shipped = json.loads((SOURCE / "export.schema.json").read_text(encoding="utf-8"))
    assert shipped == export_schema(TABLES)


def test_migration_seeds_the_default_areas() -> None:
    migration = (SOURCE / "migrations/versions/0001_current.py").read_text()
    for position, (key, label) in enumerate(DEFAULT_AREAS, 1):
        assert f"('{key}', '{label}', {position}, now(), now())" in migration


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "x", "source_ref": "a"},
        {"title": "x", "source_kind": "email"},
        {"title": "x", "fingerprint": "f"},
        {"title": "x", "due_on": "2026-1-5"},
        {"title": "x", "area": "Not A Key"},
        {"title": ""},
    ],
)
def test_create_input_refuses_half_sources_and_bad_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ItemCreate.model_validate(payload)


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "current.project:00000000-0000-7000-8000-000000000001",
        "leads.opportunity:00000000-0000-7000-8000-000000000001",
    ],
)
def test_item_refs_refuse_wrong_targets(value: str) -> None:
    with pytest.raises(ValidationError):
        ItemInput.model_validate({"ref": value})
    with pytest.raises(ValidationError):
        DependencyInput.model_validate({"item_ref": ITEM, "blocker_ref": value})


def test_update_clears_only_named_fields_and_status_needs_aware_time() -> None:
    update = ItemUpdate.model_validate(
        {"ref": ITEM, "revision": 1, "clear": ["due_on"]}
    )
    assert update.clear == ["due_on"] and update.due_on is None
    with pytest.raises(ValidationError):
        ItemUpdate.model_validate(
            {"ref": ITEM, "revision": 1, "area": "work", "clear": ["area"]}
        )
    with pytest.raises(ValidationError):
        ItemStatusInput.model_validate(
            {
                "ref": ITEM,
                "revision": 1,
                "status": "snoozed",
                "snoozed_until": "2030-01-01T09:00:00",
            }
        )


def test_no_input_names_an_author() -> None:
    for declaration, _ in MANIFEST.operations:
        fields = set(declaration.input_model.model_fields)
        assert not fields & {"author", "author_id", "author_kind"}, declaration.name


def test_current_has_no_other_domain_import() -> None:
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name.startswith(
                    ("rheo_leads", "rheo_recallatron", "rheo_relationships")
                )
                for name in names
            ), path
