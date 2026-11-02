"""Viewer-safe Leads aggregation of Current's independently authorized reads."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.factories import context_from_token
from rheo_core.storage.routing import open_unit_of_work
from rheo_current.storage import tables as current_tables
from rheo_leads import handoff_views
from rheo_leads.storage import pipeline as leads_tables
from sqlalchemy import select

from postgres.test_leads_current_handoff import Routing, routing_harness
from postgres.test_leads_current_links import (
    delete,
    item_update,
    move_to_project,
    set_module_state,
)

pytestmark = pytest.mark.postgres

VIEW = "leads.handoff.current_view"
LINK = "current.item.link_state"
ITEM = "current.item.get"
PROJECT = "current.project.get"


@pytest.fixture
def routing(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Routing]:
    with routing_harness(
        cluster, workspace, owner_account_id, monkeypatch, with_current=True
    ) as harness:
        yield harness


def delivered(r: Routing) -> Any:
    opportunity = r.opportunity()
    r.qualify(opportunity)
    result = r.handoff(opportunity)
    r.approve(r.row(result)["approval_id"])
    r.run_worker()
    # The repeated request is the public lost-response recovery path and copies the
    # core success/result ref into Leads before later Current lifecycle changes.
    return r.handoff(opportunity)


def view(r: Routing, handoff: Any, ctx: WorkspaceContext | None = None) -> Any:
    outcome = r.call(VIEW, ctx, handoff_ref=handoff.ref)
    assert outcome.ok, outcome
    return outcome.result


def restricted(r: Routing, *operations: str) -> WorkspaceContext:
    issued = r.call(
        "core.token.issue",
        kind="mcp",
        operations=[VIEW, *operations],
    )
    assert issued.ok, issued
    ctx = context_from_token(str(issued.result.value), "mcp")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.operation_set == frozenset({VIEW, *operations})
    return ctx


def test_current_view_preserves_the_original_context_and_reads_link_first(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Synthetic delivery project")
    caller = restricted(routing, LINK, ITEM, PROJECT)
    calls: list[tuple[WorkspaceContext, str, dict[str, object]]] = []
    real = handoff_views.call_in_transaction

    def spy(
        ctx: WorkspaceContext, uow: Any, name: str, payload: dict[str, object]
    ) -> Any:
        calls.append((ctx, name, payload))
        return real(ctx, uow, name, payload)

    monkeypatch.setattr(handoff_views, "call_in_transaction", spy)
    shown = view(routing, result, caller)
    assert [name for _, name, _ in calls] == [LINK, ITEM, PROJECT]
    assert all(ctx is caller for ctx, _, _ in calls)
    assert calls[0][2] == {"source_ref": result.ref}
    assert calls[1][2] == {"ref": routing.row(result)["result_ref"]}
    assert calls[2][2] == {"ref": project_ref}
    assert shown.item.state == "live" and shown.project.state == "live"
    assert shown.project.ref == project_ref
    dumped = shown.model_dump(mode="json")
    assert "project_ref" not in dumped["item"]["content"]
    assert "project" not in dumped["item"]["content"]
    assert set(dumped["history"]) == {
        "ref",
        "opportunity_ref",
        "purpose",
        "state",
        "attempt_count",
        "requested_at",
        "resolved_at",
    }


def test_item_and_project_are_authorized_independently(routing: Routing) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Independent project")

    project_only = view(routing, result, restricted(routing, LINK, PROJECT))
    assert project_only.item.state == "denied"
    assert project_only.project.state == "live"
    assert project_only.project.ref == project_ref

    item_only = view(routing, result, restricted(routing, LINK, ITEM))
    assert item_only.item.state == "live"
    assert item_only.project.state == "denied"
    denied = item_only.model_dump(mode="json")["project"]
    assert set(denied) == {"state", "message"}
    assert project_ref not in str(denied)


def test_item_failure_does_not_suppress_the_project_read(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Still readable")
    real = handoff_views.call_in_transaction

    def fail_item(
        ctx: WorkspaceContext, uow: Any, name: str, payload: dict[str, object]
    ) -> Any:
        if name == ITEM:
            raise RuntimeError("synthetic provider failure")
        return real(ctx, uow, name, payload)

    monkeypatch.setattr(handoff_views, "call_in_transaction", fail_item)
    shown = view(routing, result)
    assert shown.item.state == "unavailable"
    assert shown.project.state == "live" and shown.project.ref == project_ref
    assert "synthetic" not in str(shown.model_dump(mode="json"))


def test_failed_link_read_returns_only_redacted_unavailable_branches(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Hidden by failed link")
    item_ref = routing.row(result)["result_ref"]
    real = handoff_views.call_in_transaction

    def fail_link(
        ctx: WorkspaceContext, uow: Any, name: str, payload: dict[str, object]
    ) -> Any:
        if name == LINK:
            raise RuntimeError("raw provider link failure")
        return real(ctx, uow, name, payload)

    monkeypatch.setattr(handoff_views, "call_in_transaction", fail_link)
    shown = view(routing, result)
    dumped = shown.model_dump(mode="json")
    assert shown.item.state == shown.project.state == "unavailable"
    assert item_ref not in str(dumped) and project_ref not in str(dumped)
    assert "provider" not in str(dumped)


def test_deleted_item_keeps_an_independently_readable_project(routing: Routing) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Surviving project")
    delete(routing, routing.row(result)["result_ref"])
    shown = view(routing, result)
    assert shown.item.state == "deleted"
    assert shown.project.state == "live" and shown.project.ref == project_ref


def test_project_clear_and_deletion_beat_a_stale_leads_projection(
    routing: Routing,
) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Stale projection")
    routing.run_worker()
    assert routing.row(result)["current_project_ref"] == project_ref

    item_update(routing, routing.row(result)["result_ref"], clear=["project_ref"])
    cleared = view(routing, result)
    assert cleared.item.state == "live" and cleared.project.state == "cleared"
    assert project_ref not in str(cleared.model_dump(mode="json"))
    # The event has not run, so this proves the viewer did not use the stale copy.
    assert routing.row(result)["current_project_ref"] == project_ref

    replacement = move_to_project(routing, result, "Deleted directly")
    delete(routing, replacement)
    deleted = view(routing, result)
    assert deleted.item.state == "live" and deleted.project.state == "deleted"
    assert replacement not in str(deleted.model_dump(mode="json"))


def test_role_and_token_restrictions_are_not_widened(routing: Routing) -> None:
    result = delivered(routing)
    service = routing.service(VIEW, LINK, ITEM, PROJECT)
    refused = routing.call(VIEW, service, handoff_ref=result.ref)
    assert refused.state == "role_not_permitted", refused

    token_limited = view(routing, result, restricted(routing, LINK))
    assert token_limited.item.state == "denied"
    invalid = routing.call(VIEW, handoff_ref=result.ref, operation=ITEM)
    assert invalid.state == "input_invalid", invalid


def test_permission_revocation_and_current_unavailability_never_reuse_live_content(
    routing: Routing,
) -> None:
    result = delivered(routing)
    project_ref = move_to_project(routing, result, "Never cached")
    first = view(routing, result)
    assert first.item.state == first.project.state == "live"
    title = first.item.content.title

    revoked = view(routing, result, restricted(routing, LINK))
    payload = revoked.model_dump(mode="json")
    assert revoked.item.state == revoked.project.state == "denied"
    assert title not in str(payload) and project_ref not in str(payload)

    set_module_state(routing, "current", "disabled")
    account_id = routing.ctx.actor.id
    assert account_id is not None
    without_current = context_for_harness(routing.i.workspace, account_id, Role.OWNER)
    assert isinstance(without_current, WorkspaceContext), without_current
    assert "current" not in without_current.enabled_modules
    unavailable = view(routing, result, without_current)
    payload = unavailable.model_dump(mode="json")
    assert unavailable.item.state == unavailable.project.state == "unavailable"
    assert title not in str(payload) and project_ref not in str(payload)


def test_current_view_writes_neither_module(routing: Routing) -> None:
    result = delivered(routing)
    move_to_project(routing, result, "Read-only project")

    def rows(table: Any) -> list[dict[str, Any]]:
        with open_unit_of_work(routing.ctx) as uow:
            return [
                dict(row) for row in uow.connection.execute(select(table)).mappings()
            ]

    tables = (
        leads_tables.handoff,
        current_tables.item,
        current_tables.project,
        current_tables.source_item_link,
    )
    before = [rows(table) for table in tables]
    view(routing, result)
    assert [rows(table) for table in tables] == before
