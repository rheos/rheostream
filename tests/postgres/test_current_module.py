"""Synthetic Current behaviour through real installation, dispatch and approval."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic_core import to_jsonable_python
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.deletion.tables import deletion_record
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.tool_facade import call_registered_tool
from rheo_core.refs.resolver import register_resolver
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import outbox_event
from rheo_current import MANIFEST
from rheo_current.areas import DEFAULT_AREAS
from rheo_current.export import export_records, import_records
from rheo_current.resolvers import resolve_record
from rheo_current.storage import tables as t
from sqlalchemy import func, inspect, select, update

pytestmark = pytest.mark.postgres


@dataclass
class Current:
    cluster: ClusterSession
    workspace: UUID
    ctx: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry

    def call(self, name: str, **payload: Any) -> OperationOutcome:
        return dispatch(
            self.ctx,
            name if name.startswith("core.") else "current." + name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def tool(self, name: str, **arguments: Any) -> OperationOutcome:
        return call_registered_tool(
            self.ctx,
            name,
            arguments,
            consumers=self.consumers,
            tools=self.surfaces.tools,
            registry=self.surfaces.operations,
        )

    def ok(self, name: str, **payload: Any) -> Any:
        outcome = self.call(name, **payload)
        assert outcome.ok, outcome
        return outcome.result

    def item(self, title: str = "Synthetic task", **payload: Any) -> Any:
        return self.ok("item.create", title=title, **payload).item

    def get(self, reference: str) -> Any:
        return self.ok("item.get", ref=reference)

    def status(self, reference: str, status: str, **payload: Any) -> OperationOutcome:
        return self.call(
            "item.set_status",
            ref=reference,
            revision=self.get(reference).revision,
            status=status,
            **payload,
        )

    def as_role(self, role: Role, name: str) -> "Current":
        account = add_member(
            self.cluster.backend, self.workspace, role, display_name=name
        )
        ctx = context_for_harness(self.workspace, account, role)
        assert isinstance(ctx, WorkspaceContext)
        return Current(self.cluster, self.workspace, ctx, self.surfaces, self.consumers)

    def count(self, table: Any) -> int:
        with open_unit_of_work(self.ctx) as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
            )

    def expire_snooze(self, reference: str) -> None:
        with open_unit_of_work(self.ctx) as uow:
            uow.connection.execute(
                update(t.item)
                .where(t.item.c.id == UUID(reference.rsplit(":", 1)[1]))
                .values(snoozed_until=datetime.now(UTC) - timedelta(minutes=1))
            )
            uow.commit()

    def events(self) -> list[tuple[str, str]]:
        with open_unit_of_work(self.ctx) as uow:
            return [
                (row.type, row.subject_ref)
                for row in uow.connection.execute(
                    select(outbox_event.c.type, outbox_event.c.subject_ref)
                    .where(outbox_event.c.type.like("current.%"))
                    .order_by(outbox_event.c.id)
                )
            ]


def install(
    cluster: ClusterSession, workspace: UUID, account: UUID
) -> WorkspaceContext:
    operator = context_for_operator(workspace)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(cluster.backend, operator, workspace, "current")
    ctx = context_for_harness(workspace, account, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    return ctx


@pytest.fixture
def current(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Current]:
    with loaded_probe_modules(monkeypatch, "current") as surfaces:
        register_core_operations()
        register_core_operations(surfaces.operations)
        for kind in ("item", "project"):
            register_resolver("current", kind, resolve_record, origin="current")
        ctx = install(cluster, workspace, owner_account_id)
        yield Current(cluster, workspace, ctx, surfaces, ConsumerRegistry())


def test_current_installs_alone_with_its_schema_and_areas(current: Current) -> None:
    assert current.ctx.enabled_modules == frozenset({"current"})
    with open_unit_of_work(current.ctx) as uow:
        inspector = inspect(uow.connection)
        names = set(inspector.get_table_names(schema="current"))
        assert names == {table.name for table in t.metadata.tables.values()} | {
            "alembic_version_current"
        }
        for table in t.metadata.tables.values():
            columns = {c["name"] for c in inspector.get_columns(table.name, "current")}
            assert columns == set(table.columns.keys()), table.name
            indexes = {i["name"] for i in inspector.get_indexes(table.name, "current")}
            assert indexes == {i.name for i in table.indexes}, table.name
    areas = current.ok("area.list").items
    assert [(a.key, a.label) for a in areas] == list(DEFAULT_AREAS)
    assert len(MANIFEST.operations) == 16 and len(MANIFEST.tools) == 15


def test_source_dedup_groups_reopens_and_wakes(current: Current) -> None:
    project = current.ok("project.create", title="Synthetic project")
    source = {"source_kind": "email", "source_ref": "thread-1", "provenance": "email"}
    first = current.ok(
        "item.create",
        title="Reply to the example request",
        body="Original body",
        area="work",
        project_ref=project.ref,
        fingerprint="message-1",
        **source,
    )
    assert first.action == "created" and first.item.event_count == 1
    same = current.ok("item.create", title="Other", fingerprint="message-1", **source)
    assert same.action == "grouped"
    assert same.item.event_count == 1 and same.item.revision == first.item.revision
    grouped = current.ok(
        "item.create",
        title="Changed title",
        body="Changed body",
        area="bills",
        priority="high",
        fingerprint="message-2",
        **source,
    )
    assert grouped.action == "grouped" and grouped.item.event_count == 2
    assert grouped.item.priority == "high"
    lower = current.ok("item.create", title="x", priority="low", **source)
    assert lower.item.priority == "high" and lower.item.event_count == 3
    reference = first.item.ref
    assert current.status(reference, "done").ok
    reopened = current.ok("item.create", title="x", fingerprint="message-3", **source)
    assert reopened.action == "reopened"
    assert reopened.item.status == "open" and reopened.item.closed_at is None
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    assert current.status(reference, "snoozed", snoozed_until=later).ok
    woke = current.ok("item.create", title="x", fingerprint="message-4", **source)
    assert woke.action == "woke"
    item = woke.item
    assert item.status == "open" and item.snoozed_until is None
    assert item.event_count == 5
    assert (item.title, item.body, item.area, item.project_ref) == (
        "Reply to the example request",
        "Original body",
        "work",
        project.ref,
    )
    assert [c.kind for c in item.comments] == ["system"] * 4
    assert current.count(t.item) == 1 and current.count(t.signal) == 4
    assert [kind for kind, _ in current.events()] == [
        "current.item.created",
        "current.item.completed",
        "current.item.reopened",
    ]
    other = current.ok(
        "item.create", title="Other source", source_kind="alert", source_ref="thread-1"
    )
    assert other.action == "created"
    assert current.item("No source").ref != current.item("No source").ref


def test_status_transitions_and_snooze_invariants(current: Current) -> None:
    reference = current.item().ref
    later = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert current.status(reference, "snoozed").state == "snooze_invalid"
    assert current.status(reference, "snoozed", snoozed_until=past).state == (
        "snooze_invalid"
    )
    assert current.status(reference, "waiting", snoozed_until=later).state == (
        "snooze_invalid"
    )
    for status in ("waiting", "snoozed", "open", "snoozed", "waiting", "done"):
        extra = {"snoozed_until": later} if status == "snoozed" else {}
        outcome = current.status(reference, status, **extra)
        assert outcome.ok, (status, outcome)
        item = outcome.result
        assert item.status == status
        assert (item.snoozed_until is not None) == (status == "snoozed")
        assert (item.closed_at is not None) == (status == "done")
    for status in ("waiting", "snoozed"):
        extra = {"snoozed_until": later} if status == "snoozed" else {}
        refused = current.status(reference, status, **extra)
        assert refused.state == "transition_not_allowed"
    reopened = current.status(reference, "open").result
    assert reopened.status == "open" and reopened.closed_at is None
    assert [kind for kind, _ in current.events()] == [
        "current.item.created",
        "current.item.completed",
        "current.item.reopened",
    ]


def test_expired_snooze_wakes_into_the_active_list(current: Current) -> None:
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    sleeping = current.item("Sleeping").ref
    waking = current.item("Waking").ref
    done = current.item("Finished").ref
    open_ref = current.item("Open").ref
    for reference in (sleeping, waking):
        assert current.status(reference, "snoozed", snoozed_until=later).ok
    assert current.status(done, "done").ok
    current.expire_snooze(waking)
    active = {i.ref: i for i in current.ok("item.list").items}
    assert set(active) == {waking, open_ref}
    assert active[waking].woke and not active[open_ref].woke
    assert [i.ref for i in current.ok("item.list", woke=True).items] == [waking]
    assert {i.ref for i in current.ok("item.list", status="snoozed").items} == {
        sleeping,
        waking,
    }
    summary = current.ok("summary.get")
    assert summary.woke == 1 and summary.active == 2
    assert summary.by_status == {"open": 1, "waiting": 0, "snoozed": 2, "done": 1}


def test_dependencies_refuse_cycles_and_flag_blocked_items(current: Current) -> None:
    a, b, c = (current.item(name).ref for name in ("A", "B", "C"))
    assert current.call("dependency.add", item_ref=a, blocker_ref=a).state == (
        "dependency_cycle"
    )
    current.ok("dependency.add", item_ref=a, blocker_ref=b)
    current.ok("dependency.add", item_ref=a, blocker_ref=b)
    current.ok("dependency.add", item_ref=b, blocker_ref=c)
    assert current.call("dependency.add", item_ref=c, blocker_ref=a).state == (
        "dependency_cycle"
    )
    assert current.call("dependency.add", item_ref=b, blocker_ref=a).state == (
        "dependency_cycle"
    )
    item = current.get(a)
    assert item.blocked and [x.ref for x in item.blockers] == [b]
    assert [x.ref for x in current.get(c).blocking] == [b]
    assert {i.ref for i in current.ok("item.list", blocked=True).items} == {a, b}
    assert current.status(c, "done").ok
    assert not current.get(b).blocked and current.get(a).blocked
    assert current.status(a, "done").ok, "completing a blocked item is allowed"
    approval = current.call("core.record.delete", ref=b)
    assert approval.state == "approval_required"
    assert current.ok("core.approval.approve", approval_id=str(approval.approval_id))
    assert current.count(t.dependency) == 0
    assert current.get(a).blockers == [] and current.get(c).blocking == []
    removed = current.call("dependency.remove", item_ref=a, blocker_ref=c)
    assert removed.state == "not_found"


def test_project_delete_detaches_items(current: Current) -> None:
    project = current.ok("project.create", title="Synthetic project", area="dev")
    item = current.item("In project", project_ref=project.ref)
    current.item("Loose")
    assert current.ok("project.get", ref=project.ref).item_counts["open"] == 1
    held = current.call("core.record.delete", ref=project.ref)
    assert held.state == "approval_required", held
    done = current.call("core.approval.approve", approval_id=str(held.approval_id))
    assert done.ok and done.result.state != "refused", done
    after = current.get(item.ref)
    assert after.project_ref is None and after.revision == item.revision + 1
    assert current.count(t.project) == 0 and current.count(t.item) == 2
    with open_unit_of_work(current.ctx) as uow:
        refs = {
            f"{r.record_type}:{r.record_id}"
            for r in uow.connection.execute(select(deletion_record))
        }
    assert refs == {project.ref}


def test_area_retire_keeps_values_readable(current: Current) -> None:
    member = current.as_role(Role.MEMBER, "member-one")
    assert member.call("area.create", key="garden", label="Garden").state == (
        "role_not_permitted"
    )
    created = current.ok("area.create", key="garden", label="Garden")
    assert created.position == len(DEFAULT_AREAS) + 1
    assert current.call("area.create", key="garden", label="Again").state == (
        "area_exists"
    )
    item = current.item("Plant beds", area="garden")
    current.ok("area.update", key="garden", label="Garden and yard", retired=True)
    assert current.get(item.ref).area == "garden"
    assert "garden" not in {a.key for a in current.ok("area.list").items}
    retired = current.ok("area.list", include_retired=True).items
    assert {a.key: a for a in retired}["garden"].retired
    assert current.call("item.create", title="x", area="garden").state == (
        "area_retired"
    )
    assert current.call("item.create", title="x", area="nowhere").state == (
        "area_unknown"
    )
    kept = current.ok(
        "item.update", ref=item.ref, revision=item.revision, title="Plant the beds"
    )
    assert kept.area == "garden" and kept.title == "Plant the beds"
    moved = current.ok(
        "item.update", ref=item.ref, revision=kept.revision, clear=["area"]
    )
    assert moved.area is None
    assert current.call(
        "item.update", ref=item.ref, revision=moved.revision, area="garden"
    ).state == ("area_retired")
    current.ok("area.update", key="garden", retired=False)
    assert "garden" in {a.key for a in current.ok("area.list").items}


def test_comment_author_comes_from_the_actor(current: Current) -> None:
    reference = current.item().ref
    member = current.as_role(Role.MEMBER, "member-two")
    current.ok("item.comment", ref=reference, body="Owner note on the example")
    member.ok("item.comment", ref=reference, body="Member note")
    comments = current.get(reference).comments
    assert [(c.author_kind, c.author_id) for c in comments] == [
        ("account", current.ctx.actor.id),
        ("account", member.ctx.actor.id),
    ]
    assert current.call(
        "item.comment", ref=reference, body="x", author_id=str(UUID(int=1))
    ).state == ("input_invalid")
    hits = current.ok("item.list", query="examples note").items
    assert [i.ref for i in hits] == [reference]
    assert current.ok("item.list", query="unrelated words").items == []


def test_compare_and_set_and_clearing(current: Current) -> None:
    item = current.item("Due item", due_on="2020-01-01", priority="critical")
    undated = current.item("No due date")
    assert [i.ref for i in current.ok("item.list", overdue=True).items] == [item.ref]
    assert [i.ref for i in current.ok("item.list", overdue=False).items] == [
        undated.ref
    ]
    updated = current.ok(
        "item.update", ref=item.ref, revision=item.revision, clear=["due_on"]
    )
    assert updated.due_on is None and updated.priority == "critical"
    stale = current.call(
        "item.update", ref=item.ref, revision=item.revision, title="Lost update"
    )
    assert stale.state == "record_stale"
    assert current.status(item.ref, "done").ok
    assert current.call(
        "item.set_status", ref=item.ref, revision=updated.revision, status="open"
    ).state == ("record_stale")
    assert current.call(
        "item.update",
        ref=item.ref,
        revision=current.get(item.ref).revision,
        due_on="2031-01-01",
        clear=["due_on"],
    ).state == ("input_invalid")
    project = current.ok("project.create", title="P")
    stale_project = current.call(
        "project.update", ref=project.ref, revision=project.revision + 1, title="Q"
    )
    assert stale_project.state == "record_stale"
    paused = current.ok(
        "project.update", ref=project.ref, revision=project.revision, status="paused"
    )
    assert paused.status == "paused" and paused.title == "P"


def test_deletes_are_owner_only(current: Current) -> None:
    item = current.item()
    member = current.as_role(Role.MEMBER, "member-three")
    member.ok("item.update", ref=item.ref, revision=item.revision, title="Member edit")
    refused = member.call("core.record.delete", ref=item.ref)
    assert refused.state == "not_found", refused
    held = current.call("core.record.delete", ref=item.ref)
    assert held.state == "approval_required"
    current.ok("item.comment", ref=item.ref, body="Changes the timestamp only")
    done = current.call("core.approval.approve", approval_id=str(held.approval_id))
    assert done.ok and done.result.state != "refused", done
    assert current.count(t.item) == 0 and current.count(t.comment) == 0


def test_delete_approval_is_revision_bound(current: Current) -> None:
    item = current.item()
    held = current.call("core.record.delete", ref=item.ref)
    current.ok("item.update", ref=item.ref, revision=item.revision, title="Changed")
    outcome = current.call("core.approval.approve", approval_id=str(held.approval_id))
    assert outcome.ok and outcome.result.state == "refused", outcome
    assert current.count(t.item) == 1


def test_export_restore_round_trip(
    current: Current, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    current.ok("area.create", key="garden", label="Garden")
    current.ok("area.update", key="interior", retired=True)
    project = current.ok("project.create", title="Synthetic project", area="garden")
    a = current.ok(
        "item.create",
        title="A",
        body="Body text",
        area="garden",
        project_ref=project.ref,
        due_on="2030-05-01",
        source_kind="alert",
        source_ref="alert-1",
        fingerprint="f-1",
    ).item
    b = current.item("B")
    current.ok("dependency.add", item_ref=a.ref, blocker_ref=b.ref)
    current.ok("item.comment", ref=a.ref, body="A comment")
    later = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert current.status(b.ref, "snoozed", snoozed_until=later).ok
    with open_unit_of_work(current.ctx) as uow:
        rows = to_jsonable_python(
            export_records(SimpleNamespace(connection=uow.connection))
        )
    schema = json.loads(
        resources.files("rheo_current").joinpath("export.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    for row in rows:
        validator.validate(row)
    assert {row["record_type"] for row in rows} == {
        f"current.{name}"
        for name in ("area", "project", "item", "comment", "signal", "dependency")
    }

    other = make_workspace()
    ctx = install(current.cluster, other, owner_account_id)
    with open_unit_of_work(ctx) as uow:
        import_records(uow, rows)()
        uow.commit()
    restored = Current(current.cluster, other, ctx, current.surfaces, current.consumers)
    with open_unit_of_work(ctx) as uow:
        again = to_jsonable_python(
            export_records(SimpleNamespace(connection=uow.connection))
        )
    assert again == rows
    item = restored.get(a.ref)
    assert item.blocked and item.project_ref == project.ref and item.area == "garden"
    assert (
        restored.ok(
            "item.create",
            title="x",
            source_kind="alert",
            source_ref="alert-1",
            fingerprint="f-1",
        ).action
        == "grouped"
    )
    with open_unit_of_work(ctx) as uow:
        with pytest.raises(Exception, match="restore_target_not_empty"):
            import_records(uow, rows)
    broken = [r for r in rows if r["record_type"] != "current.project"]
    third = install(current.cluster, make_workspace(), owner_account_id)
    with open_unit_of_work(third) as uow:
        with pytest.raises(Exception, match="artifact_invalid"):
            import_records(uow, broken)


def test_mcp_tools_drive_the_work_loop(current: Current) -> None:
    """Through the real tool facade, which forwards every field, defaults included."""
    names = {t.name for t in MANIFEST.tools}
    assert names <= current.surfaces.tools.names()
    project = current.tool("current_create_project", title="Synthetic project")
    assert project.ok, project
    created = current.tool(
        "current_create",
        title="Agent task",
        area="dev",
        project_ref=project.result.ref,
        due_on="2031-02-03",
        provenance="agent",
    )
    assert created.ok and created.result.action == "created", created
    item = created.result.item
    blocker = current.tool("current_create", title="Blocker").result.item
    renamed = current.tool(
        "current_update", ref=item.ref, revision=item.revision, title="Agent task v2"
    )
    assert renamed.ok, renamed
    kept = renamed.result
    assert (kept.area, kept.project_ref, str(kept.due_on)) == (
        "dev",
        project.result.ref,
        "2031-02-03",
    )
    assert current.tool(
        "current_add_dependency", item_ref=item.ref, blocker_ref=blocker.ref
    ).result.blocked
    assert current.tool("current_comment", ref=item.ref, body="Agent note").ok
    listed = current.tool("current_list", blocked=True)
    assert [i.ref for i in listed.result.items] == [item.ref]
    done = current.tool(
        "current_status", ref=item.ref, revision=kept.revision, status="done"
    )
    assert done.ok and done.result.status == "done", done
    assert current.tool("current_summary").result.by_status["done"] == 1
    assert len(current.tool("current_areas").result.items) == len(DEFAULT_AREAS)
    assert len(current.tool("current_list_projects").result.items) == 1
    held = current.tool("current_delete_project", ref=project.result.ref)
    assert held.state == "approval_required", held
    assert current.tool("current_delete_project", ref=item.ref).state == (
        "input_invalid"
    )
