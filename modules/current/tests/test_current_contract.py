"""Distribution, contract shapes, export schema and boundary checks for Current."""

import ast
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rheo_contracts import ExternalSpec, Idempotency, KeyedSpec, Role, SafetyClass
from rheo_core.modules.manifest import DestinationMode
from rheo_core.modules.production import PRODUCTION_EXTERNAL_ALLOWLIST
from rheo_core.operations.refusals import OperationRefused
from rheo_core.tokens.sets import ALERT_WRITER_OPERATIONS
from rheo_current import MANIFEST
from rheo_current.areas import DEFAULT_AREAS
from rheo_current.common import READ_ITEM
from rheo_current.contracts import (
    DependencyInput,
    ExternalItemCreate,
    ExternalItemCreated,
    ItemCloseBySource,
    ItemCreate,
    ItemCreated,
    ItemDetail,
    ItemInput,
    ItemStatusInput,
    ItemUpdate,
    SourceItemLink,
    SourceItemLinkInput,
)
from rheo_current.export import TABLES
from rheo_current.operations import OPERATIONS, OWN_REFUSALS, REFUSALS, SPECS
from rheo_current.storage import tables
from sqlalchemy import CheckConstraint, Date, DateTime, Integer, Table, Text, Uuid

SOURCE = Path(__file__).parents[1] / "src" / "rheo_current"
ITEM = "current.item:00000000-0000-7000-8000-000000000001"
CLOSE = "current.item.close_by_source"
CLOSE_TOOL = "current_close_by_source"


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
    assert MANIFEST.deletion_participants == ()
    assert MANIFEST.export.format_version == 2
    assert {e.type for e in MANIFEST.events} == {
        "current.item.created",
        "current.item.completed",
        "current.item.reopened",
        "current.source_item_link.changed",
    }


def test_source_item_link_read_and_event_share_one_shape() -> None:
    """The lifecycle read and the change event carry exactly the same fields: every
    reference, both states, one revision and the change time. Nothing names a
    module outside Current."""
    fields = {
        "source_ref",
        "item_ref",
        "project_ref",
        "last_project_ref",
        "item_state",
        "project_state",
        "revision",
        "changed_at",
    }
    assert set(SourceItemLink.model_fields) == fields
    [event] = [
        e for e in MANIFEST.events if e.type == "current.source_item_link.changed"
    ]
    assert (event.schema_version, event.data) == (1, SourceItemLink)
    [read] = [
        (d, handler)
        for d, handler in MANIFEST.operations
        if d.name == "current.item.link_state"
    ]
    declaration = read[0]
    assert declaration.safety_class is SafetyClass.READ
    assert declaration.input_model is SourceItemLinkInput
    assert declaration.output is SourceItemLink
    # Every reader role, the service role included: a lifecycle read for whoever
    # may run this one operation, separate from current.item.get.
    assert declaration.roles == frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE})
    assert declaration.audit is None
    assert declaration.name not in {t.operation for t in MANIFEST.tools}
    assert set(SourceItemLinkInput.model_fields) == {"source_ref"}
    for invalid in ({}, {"source_ref": ""}, {"source_ref": "x", "item_ref": ITEM}):
        with pytest.raises(ValidationError):
            SourceItemLinkInput.model_validate(invalid)
    good = {
        "source_ref": "synthetic:source",
        "item_ref": ITEM,
        "project_ref": None,
        "last_project_ref": None,
        "item_state": "available",
        "project_state": "never_assigned",
        "revision": 1,
        "changed_at": "2030-01-01T00:00:00+00:00",
    }
    assert SourceItemLink.model_validate(good).revision == 1
    for change in (
        {"item_state": "gone"},
        {"project_state": "lost"},
        {"revision": 0},
        {"changed_at": "2030-01-01T00:00:00"},
    ):
        with pytest.raises(ValidationError):
            SourceItemLink.model_validate({**good, **change})


def test_current_publishes_record_routes_for_items_and_projects() -> None:
    web = MANIFEST.web
    assert web is not None
    assert [(r.record_type, r.route_id, r.ref_query) for r in web.record_routes] == [
        ("item", "item", "ref"),
        ("project", "project", "ref"),
    ]
    paths = {r.id: r.path for r in web.routes}
    assert (paths["item"], paths["project"]) == ("/item", "/project")


def test_current_web_surface_and_forms() -> None:
    web = MANIFEST.web
    assert web is not None
    assert (web.surface.surface, web.surface.host, web.surface.path) == (
        "current",
        "current",
        "/current",
    )
    assert web.package_name == "@rheo-stream/current-web"
    assert [(r.id, r.path) for r in web.routes] == [
        ("queue", "/"),
        ("item", "/item"),
        ("new", "/new"),
        ("projects", "/projects"),
        ("project", "/project"),
        ("areas", "/areas"),
    ]
    # The screens submit every MUTATE operation and nothing else, except close by
    # source: only external writers call it, so it is no form.
    mutating = {
        d.name for d, _ in MANIFEST.operations if d.safety_class is SafetyClass.MUTATE
    }
    assert {f.operation for f in web.forms} == mutating - {CLOSE}


def test_current_declares_exactly_one_external_operation_and_no_financial() -> None:
    classes = {d.safety_class for d, _ in MANIFEST.operations}
    assert classes <= {SafetyClass.READ, SafetyClass.MUTATE, SafetyClass.EXTERNAL}
    external = [
        declaration
        for declaration, _ in MANIFEST.operations
        if declaration.safety_class is SafetyClass.EXTERNAL
    ]
    assert len(external) == 1
    declaration = external[0]
    assert declaration.name == "current.item.create_external"
    # The held person core rebuilds for the continuation; never a service context.
    assert declaration.roles == frozenset({Role.OWNER, Role.MEMBER})
    assert declaration.input_model is ExternalItemCreate
    assert declaration.output is ExternalItemCreated
    assert declaration.idempotency is Idempotency.KEYED
    assert declaration.keyed == KeyedSpec(
        key_field="idempotency_key",
        request_schema_version=1,
        result_schema_version=1,
    )
    assert declaration.external == ExternalSpec(
        destination_field="destination_ref",
        destination_ref="current",
        result_ref_field="item_ref",
    )
    # The exact production tuple core allowlists, compared to core's own constant.
    assert PRODUCTION_EXTERNAL_ALLOWLIST[declaration.name] == (
        MANIFEST.module_id,
        declaration.external,
    )
    # One in-process destination registered for exactly this operation; Current is
    # its own database, so there is no credential check and nothing to reconcile.
    assert len(MANIFEST.destinations) == 1
    destination = MANIFEST.destinations[0]
    assert destination.destination_ref == "current"
    assert destination.operation_name == declaration.name
    assert destination.mode is DestinationMode.IN_PROCESS
    assert destination.credentials_available is None
    assert destination.reconcile is None
    assert MANIFEST.dependencies == ()
    # No tool: a model, a token or a person cannot reach it except through approval.
    assert declaration.name not in {t.operation for t in MANIFEST.tools}
    assert all(t.name.startswith("current_") for t in MANIFEST.tools)
    deletes = {t.name for t in MANIFEST.tools if t.operation == "core.record.delete"}
    assert deletes == {"current_delete_item", "current_delete_project"}


EXTERNAL_PAYLOAD = {
    "destination_ref": "current",
    "source_ref": "leads.handoff:00000000-0000-7000-8000-000000000001",
    "title": "Prepare synthetic delivery",
    "body": "Public-safe fixture body",
    "priority": "high",
    "area": "work",
    "due_on": "2030-02-03",
    "idempotency_key": "handoff-1#1",
}


def test_external_item_input_is_exact_and_cannot_create_a_project() -> None:
    model = ExternalItemCreate.model_validate(EXTERNAL_PAYLOAD)
    assert model.destination_ref == "current"
    assert set(ExternalItemCreate.model_fields) == set(EXTERNAL_PAYLOAD)
    assert "project_ref" not in ExternalItemCreate.model_fields
    assert set(ExternalItemCreated.model_fields) == {"item_ref"}
    assert ExternalItemCreated(item_ref=ITEM).item_ref == ITEM
    minimal = {
        k: EXTERNAL_PAYLOAD[k]
        for k in ("destination_ref", "source_ref", "title", "idempotency_key")
    }
    defaults = ExternalItemCreate.model_validate(minimal)
    assert (defaults.body, defaults.priority, defaults.area, defaults.due_on) == (
        "",
        "normal",
        None,
        None,
    )
    for invalid in (
        {**EXTERNAL_PAYLOAD, "destination_ref": "other"},
        {k: v for k, v in EXTERNAL_PAYLOAD.items() if k != "destination_ref"},
        {
            **EXTERNAL_PAYLOAD,
            "project_ref": "current.project:00000000-0000-7000-8000-000000000002",
        },
        {**EXTERNAL_PAYLOAD, "idempotency_key": ""},
        {**EXTERNAL_PAYLOAD, "idempotency_key": "k" * 257},
        {**EXTERNAL_PAYLOAD, "source_ref": ""},
        {**EXTERNAL_PAYLOAD, "title": ""},
        {**EXTERNAL_PAYLOAD, "priority": "urgent"},
        {**EXTERNAL_PAYLOAD, "due_on": "2030-2-3"},
        {**EXTERNAL_PAYLOAD, "provenance": "manual"},
    ):
        with pytest.raises(ValidationError):
            ExternalItemCreate.model_validate(invalid)
    with pytest.raises(ValidationError):
        ExternalItemCreated.model_validate({"item_ref": ITEM, "project_ref": None})


def test_source_item_link_migration_is_append_only_and_matches_metadata() -> None:
    versions = SOURCE / "migrations" / "versions"
    assert sorted(p.name for p in versions.glob("0*.py")) == [
        "0001_current.py",
        "0002_source_item_link.py",
    ]
    tree = ast.parse((versions / "0002_source_item_link.py").read_text("utf-8"))
    assigned = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert ast.literal_eval(assigned["revision"]) == "0002_source_item_link"
    assert ast.literal_eval(assigned["down_revision"]) == "0001_current"
    statements = ast.literal_eval(assigned["STATEMENTS"])
    ddl = " ".join(" ".join(statements).split())
    # Only the one new table and its index: nothing 0001 created is altered.
    assert ddl.count("CREATE TABLE") == 1
    assert "current.source_item_link" in ddl
    assert not any(word in ddl for word in ("ALTER", "DROP", "current.item ("))
    link = tables.source_item_link
    assert list(link.primary_key.columns.keys()) == ["source_ref"]
    assert set(link.columns.keys()) == {
        "source_ref",
        "item_ref",
        "revision",
        "item_state",
        "project_ref",
        "last_project_ref",
        "project_state",
        "changed_at",
    }
    assert not link.foreign_keys
    checks = {
        constraint.name: " ".join(str(constraint.sqltext).split())
        for constraint in link.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert len(checks) == 6
    for name, text in checks.items():
        assert f"CONSTRAINT {name} CHECK" in ddl
        assert text.replace(" ", "") in ddl.replace(" ", ""), name
    assert {i.name for i in link.indexes} == {"uq_source_item_link_item"}
    assert all(i.unique for i in link.indexes)
    assert "CREATE UNIQUE INDEX uq_source_item_link_item" in ddl


def test_export_schema_matches_tables() -> None:
    shipped = json.loads((SOURCE / "export.schema.json").read_text(encoding="utf-8"))
    assert shipped == export_schema(TABLES)
    # Format 2 adds the source link, last because nothing references it.
    assert [table.name for table in TABLES] == [
        "area",
        "project",
        "item",
        "comment",
        "signal",
        "dependency",
        "source_item_link",
    ]


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


def test_close_by_source_is_a_member_mutate_with_its_tool() -> None:
    declaration = next(d for d, _ in MANIFEST.operations if d.name == CLOSE)
    assert declaration.safety_class is SafetyClass.MUTATE
    assert declaration.roles == frozenset({Role.OWNER, Role.MEMBER})
    assert declaration.audit is not None and declaration.audit.subject_field is None
    assert declaration.input_model is ItemCloseBySource
    tool = next(t for t in MANIFEST.tools if t.name == CLOSE_TOOL)
    assert (tool.operation, tool.safety_class) == (CLOSE, SafetyClass.MUTATE)
    assert {t.name for t in MANIFEST.tools if t.operation.startswith("current.")} == {
        tool for *_, tool, _ in SPECS if tool
    }


def test_close_tool_states_its_own_refusals_and_the_rest_keep_the_suffix() -> None:
    descriptions = {t.name: t.description for t in MANIFEST.tools}
    close = descriptions[CLOSE_TOOL]
    assert REFUSALS.strip() not in close
    assert "never returns not_found" in close
    assert OWN_REFUSALS == {CLOSE_TOOL}
    # Every other tool keeps its row text with the suffix appended once, as before.
    others = {name: text for *_, name, text in SPECS if name and name != CLOSE_TOOL}
    assert len(others) == 13
    for name, text in others.items():
        assert descriptions[name] == text + REFUSALS, name


GOOD_PAIR = {"source_kind": "health-probe", "source_ref": "health-probe:api"}


@pytest.mark.parametrize(
    "pair",
    [
        {**GOOD_PAIR, "source_kind": "Bad Kind"},
        {**GOOD_PAIR, "source_kind": "a" * 65},
        {**GOOD_PAIR, "source_ref": ""},
        {**GOOD_PAIR, "source_ref": "r" * 513},
    ],
)
def test_create_and_close_refuse_the_same_bad_pairs(pair: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ItemCreate.model_validate({"title": "x", **pair})
    with pytest.raises(ValidationError):
        ItemCloseBySource.model_validate(pair)


def test_ordinary_create_refuses_the_reserved_handoff_kind() -> None:
    """Only the approved destination makes ``handoff`` items, because its source
    index adopts any item already holding such a pair."""
    reserved = {"source_kind": "handoff", "source_ref": EXTERNAL_PAYLOAD["source_ref"]}
    with pytest.raises(ValidationError, match="reserved for approved handoffs"):
        ItemCreate.model_validate({"title": "x", **reserved})
    for near in ("handoff.leads", "leads.handoff", "handoffs"):
        pair = {**reserved, "source_kind": near}
        assert ItemCreate.model_validate({"title": "x", **pair}).source_kind == near


def test_the_declared_external_handler_refuses_without_touching_storage() -> None:
    """Core runs the create only through the registered destination; the handler
    the operation declares refuses before it reads or writes anything."""
    [handler] = [
        handler
        for declaration, handler in MANIFEST.operations
        if declaration.safety_class is SafetyClass.EXTERNAL
    ]

    class NoStorage:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the handler touched storage: {name}")

    with pytest.raises(OperationRefused) as refused:
        handler(
            object(),  # type: ignore[arg-type]  # refuses before reading a context
            NoStorage(),  # type: ignore[arg-type]  # any access fails the test
            ExternalItemCreate.model_validate(EXTERNAL_PAYLOAD),
        )
    assert refused.value.state == "operation_not_permitted"


def test_close_input_bounds_the_note_and_takes_nothing_else() -> None:
    widest = {"source_kind": "a" * 64, "source_ref": "r" * 512}
    assert ItemCreate.model_validate({"title": "x", **widest}).source_ref == "r" * 512
    closing = ItemCloseBySource.model_validate({**widest, "note": "n" * 2000})
    assert closing.note == "n" * 2000
    assert ItemCloseBySource.model_validate(GOOD_PAIR).note is None
    for bad in (
        {**GOOD_PAIR, "note": ""},
        {**GOOD_PAIR, "note": "n" * 2001},
        {**GOOD_PAIR, "ref": ITEM},
        {"source_kind": "health-probe"},
    ):
        with pytest.raises(ValidationError):
            ItemCloseBySource.model_validate(bad)


def test_the_alert_writer_set_names_currents_declared_create_and_close() -> None:
    """Core spells the ``alert_writer`` set's two names as literals, because core
    must not import this package. This holds each literal to a declared operation, so
    renaming either operation here fails a test instead of emptying the set."""
    declared = {declaration.name for declaration, _ in OPERATIONS}
    assert ALERT_WRITER_OPERATIONS == {"current.item.create", CLOSE}
    assert ALERT_WRITER_OPERATIONS <= declared


DETAIL: dict[str, Any] = {
    "ref": ITEM,
    "title": "Synthetic task",
    "status": "open",
    "priority": "normal",
    "area": None,
    "project_ref": None,
    "due_on": None,
    "snoozed_until": None,
    "woke": False,
    "overdue": False,
    "blocked": False,
    "event_count": 1,
    "revision": 1,
    "updated_at": "2026-10-21T09:00:00Z",
    "body": "Type: support-ticket",
    "provenance": "alert",
    "source_kind": "support.form",
    "source_ref": "support-request-1",
    "created_at": "2026-10-21T09:00:00Z",
    "closed_at": None,
    "project": None,
    "blockers": [],
    "blocking": [],
    "comments": [],
}


def test_a_create_result_dumps_the_item_or_the_ref_with_no_null_key() -> None:
    full = ItemCreated(action="grouped", item=ItemDetail.model_validate(DETAIL))
    dumped = full.model_dump(mode="json")
    assert list(dumped) == ["action", "item"]
    assert dumped["item"]["body"] == "Type: support-ticket"
    slim = ItemCreated(action="grouped", ref=ITEM).model_dump(mode="json")
    assert slim == {"action": "grouped", "ref": ITEM}
    for neither_or_both in ({}, {"item": DETAIL, "ref": ITEM}):
        with pytest.raises(ValidationError):
            ItemCreated.model_validate({"action": "created", **neither_or_both})


def test_the_read_check_names_get_which_every_create_or_close_role_may_run() -> None:
    """``reads_items`` checks only the operation set, which is sound only while get is
    declared here and open to every role that may create or close by source."""
    roles = {d.name: d.roles for d, _ in OPERATIONS}
    assert READ_ITEM == "current.item.get" and READ_ITEM in roles
    assert roles["current.item.create"] <= roles[READ_ITEM]
    assert roles[CLOSE] <= roles[READ_ITEM]


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
