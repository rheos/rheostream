"""The bounded repair job and Leads' minimal projection of Current's source link.

Synthetic, local-only data throughout. The job is driven directly in one committed
unit of work per run, exactly as the worker runs it, so each run's page and cursor
are observable; one test runs it from its real schedule through a worker visit.
Events are withheld from Leads where a test proves the job repairs a missed event.
"""

from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import install_and_enable_module
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.modules import readiness
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations import dispatch
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import schedule
from rheo_leads import current_links as cl
from rheo_leads import handoff_routing as routing_module
from rheo_leads.export import export_records, import_records
from rheo_leads.storage import pipeline as p
from sqlalchemy import select, update

from postgres.test_leads_current_handoff import (
    Routing,
    routing_harness,
    set_module_state,
)
from postgres.test_leads_pipeline import ok

pytestmark = pytest.mark.postgres


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


def reconcile(
    r: Routing,
    *,
    page_size: int = 2,
    ctx: WorkspaceContext | None = None,
    commit: bool = True,
) -> cl.ReconcileReport:
    with open_unit_of_work(r.ctx) as uow:
        repair = ctx or cl.repair_context(r.i.workspace, uow)
        assert isinstance(repair, WorkspaceContext), repair
        report = cl.reconcile(
            repair,
            HandlerUnitOfWork(uow, consumers=r.i.consumers),
            page_size=page_size,
        )
        if commit:
            uow.commit()
    return report


def cursor(r: Routing, scan: str) -> dict[str, Any] | None:
    with open_unit_of_work(r.ctx) as uow:
        found = (
            uow.connection.execute(
                select(p.current_link_cursor).where(
                    p.current_link_cursor.c.scan == scan
                )
            )
            .mappings()
            .one_or_none()
        )
    return None if found is None else dict(found)


def after(r: Routing, scan: str) -> UUID | None:
    found = cursor(r, scan)
    return None if found is None else found["after_id"]


def many(
    r: Routing, count: int, *, approve: bool = True, subject: str = "Synthetic inquiry"
) -> list[Any]:
    """``count`` handoffs of one qualified opportunity, in request order."""
    opportunity = r.opportunity(subject)
    r.qualify(opportunity)
    made = [r.handoff(opportunity, f"key-{n}") for n in range(count)]
    if approve:
        for result in made:
            r.approve(r.row(result)["approval_id"])
        r.run_worker()
    return made


def ident(result: Any) -> UUID:
    return RecordRef.parse(result.ref).id


def spy_on_public_calls(monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    """Observe every registered operation the repair reaches, in call order."""
    monkeypatch.setattr(cl, "call_in_transaction", spy)
    monkeypatch.setattr(routing_module, "call_in_transaction", spy)


def without_leads_events(r: Routing) -> None:
    """Leads misses every link event from here on (disabled, or a lost delivery)."""
    r.i.consumers._consumers.pop(cl.LINK_CONSUMER_ID, None)


# --- core first, then the link --------------------------------------------------------


def test_repair_reads_core_before_any_link_lookup(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting, settled = many(routing, 2, approve=False)
    routing.approve(routing.row(settled)["approval_id"])
    routing.run_worker()
    calls: list[tuple[str, str]] = []

    def spy(ctx: WorkspaceContext, uow: Any, name: str, payload: Any, **kw: Any) -> Any:
        calls.append((name, str(next(iter(payload.values())))))
        return call_in_transaction(ctx, uow, name, payload, **kw)

    spy_on_public_calls(monkeypatch, spy)
    report = reconcile(routing, page_size=10)
    waiting_action = str(routing.row(waiting)["external_action_id"])
    settled_action = str(routing.row(settled)["external_action_id"])
    # The nonterminal scan: core for each row; the link only after proven success.
    assert calls[:3] == [
        ("core.external_action.get", waiting_action),
        ("core.external_action.get", settled_action),
        ("current.item.link_state", settled.ref),
    ]
    # The successful scan reads only the link.
    assert calls[3:] == [("current.item.link_state", settled.ref)]
    assert routing.row(waiting)["state"] == "pending"
    row = routing.row(settled)
    assert row["state"] == "succeeded" and row["result_ref"].startswith("current.item:")
    assert row["current_link_revision"] == 1
    assert report.nonterminal.examined == 2 and report.succeeded.examined == 1


def test_a_core_outcome_is_copied_without_any_link_read_unless_it_succeeded(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    (result,) = many(routing, 1, approve=False)
    routing.exhaust(monkeypatch, result)
    names: list[str] = []

    def spy(ctx: WorkspaceContext, uow: Any, name: str, payload: Any, **kw: Any) -> Any:
        names.append(name)
        return call_in_transaction(ctx, uow, name, payload, **kw)

    spy_on_public_calls(monkeypatch, spy)
    reconcile(routing)
    assert names == ["core.external_action.get"]
    row = routing.row(result)
    assert (row["state"], row["result_detail"]) == ("failed", "retryable_no_effect")
    assert row["current_link_revision"] is None


# --- a person's resolution of an unresolved action ------------------------------------


def history(r: Routing, handoff: Any) -> Any:
    opportunity = f"leads.opportunity:{r.row(handoff)['opportunity_id']}"
    (item,) = ok(r.i, "handoff.list", ref=opportunity).items
    return item


def test_a_resolution_to_succeeded_reaches_leads_and_then_its_link(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    (result,) = many(routing, 1, approve=False)
    double = routing.unresolve(monkeypatch, result)
    reconcile(routing, page_size=10)
    row = routing.row(result)
    assert (row["state"], row["result_detail"]) == ("unresolved", "outcome_unknown")

    # The provider had acted; a person checks it and resolves the action.
    double.landed = routing.land(result)
    resolved = routing.resolve(
        result,
        "succeeded",
        terminal_check_kind="provider_status",
        result_ref=double.landed,
    )
    assert resolved.ok, resolved
    routing.swap_destination(monkeypatch)
    report = reconcile(routing, page_size=10)
    assert report.nonterminal.examined == 1 and report.nonterminal.updated == 1
    row = routing.row(result)
    assert (row["state"], row["result_ref"], row["result_detail"]) == (
        "succeeded",
        double.landed,
        None,
    )
    assert projection(routing, result) == ("never_assigned", None, None, 1)
    shown = history(routing, result)
    assert (shown.state, shown.result_ref) == ("succeeded", double.landed)

    # From here the successful scan carries it: a later move is repaired.
    without_leads_events(routing)
    target = move_to_project(routing, result, "Resolved project")
    again = reconcile(routing, page_size=10)
    assert again.nonterminal.examined == 0 and again.succeeded.updated == 1
    assert projection(routing, result) == ("linked", target, None, 2)


def test_a_resolution_to_failed_reaches_leads_and_restages_on_a_new_approval(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    (result,) = many(routing, 1, approve=False)
    routing.unresolve(monkeypatch, result)
    reconcile(routing, page_size=10)
    assert routing.row(result)["state"] == "unresolved"
    assert routing.restage(result).state == "handoff_not_restageable"

    # Nothing landed: the provider proves absence and a person resolves it failed.
    resolved = routing.resolve(result, "failed")
    assert resolved.ok, resolved
    routing.swap_destination(monkeypatch)
    report = reconcile(routing, page_size=10)
    assert report.nonterminal.updated == 1
    first = routing.row(result)
    assert (first["state"], first["result_detail"], first["result_ref"]) == (
        "failed",
        "resolved_no_effect",
        None,
    )
    assert history(routing, result).state == "failed"

    restaged = routing.restage(result)
    assert restaged.ok, restaged
    second = routing.row(result)
    assert (second["state"], second["attempt_count"]) == ("pending", 2)
    assert second["approval_id"] != first["approval_id"]
    routing.run_worker()
    assert routing.current_items() == []
    routing.approve(second["approval_id"])
    routing.run_worker()
    (item,) = routing.current_items()
    assert item["source_ref"] == result.ref


def test_a_resolution_to_cancelled_is_recorded_and_never_restages_but_a_new_key_does(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    (result,) = many(routing, 1, approve=False)
    routing.unresolve(monkeypatch, result)
    reconcile(routing, page_size=10)
    assert routing.row(result)["state"] == "unresolved"

    resolved = routing.resolve(result, "cancelled")
    assert resolved.ok, resolved
    routing.swap_destination(monkeypatch)
    reconcile(routing, page_size=10)
    row = routing.row(result)
    assert (row["state"], row["result_detail"]) == (
        "cancelled",
        "resolved_cancelled",
    )
    refused = routing.restage(result)
    assert refused.state == "handoff_not_restageable", refused

    opportunity = routing.get(f"leads.opportunity:{row['opportunity_id']}")
    fresh = routing.handoff(opportunity, "fresh-after-cancel")
    assert routing.row(fresh)["state"] == "pending"
    assert routing.row(fresh)["attempt_count"] == 1


def test_a_settled_absence_leaves_the_nonterminal_scan(routing: Routing) -> None:
    """An unresolved handoff whose core action is gone is read once, then left."""
    (result,) = many(routing, 1, approve=False)
    with open_unit_of_work(routing.ctx) as uow:
        uow.connection.execute(
            update(p.handoff)
            .where(p.handoff.c.id == ident(result))
            .values(
                state="unresolved",
                result_detail="outcome_unknown",
                external_action_id=UUID(int=98),
                approval_id=None,
            )
        )
        uow.commit()
    first = reconcile(routing, page_size=10)
    assert first.nonterminal.examined == 1
    row = routing.row(result)
    assert (row["state"], row["result_detail"]) == (
        "unresolved",
        "external_action_missing",
    )
    assert reconcile(routing, page_size=10).nonterminal.examined == 0


def test_an_invalid_link_combination_is_skipped_without_pinning_the_cursor(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, good = many(routing, 2)
    reconcile(routing, page_size=10)
    without_leads_events(routing)
    move_to_project(routing, bad, "Bad project")
    good_target = move_to_project(routing, good, "Good project")

    def spy(ctx: WorkspaceContext, uow: Any, name: str, payload: Any, **kw: Any) -> Any:
        answer = call_in_transaction(ctx, uow, name, payload, **kw)
        if name == cl.LINK_STATE and payload["source_ref"] == bad.ref:
            dumped = {
                **answer.model_dump(mode="json"),
                "project_state": "linked",
                "project_ref": None,
            }
            return SimpleNamespace(model_dump=lambda mode: dumped)
        return answer

    spy_on_public_calls(monkeypatch, spy)
    report = reconcile(routing, page_size=2)
    assert report.succeeded.examined == 2
    assert report.succeeded.unavailable == 1 and report.succeeded.updated == 1
    assert projection(routing, bad) == ("never_assigned", None, None, 1)
    assert projection(routing, good) == ("linked", good_target, None, 2)
    assert after(routing, "succeeded") == ident(good)


# --- two independent cursors ----------------------------------------------------------


def test_each_scan_pages_strictly_after_its_cursor_and_wraps(routing: Routing) -> None:
    made = many(routing, 5)
    ids = [ident(m) for m in made]

    reconcile(routing)
    assert after(routing, "nonterminal") == ids[1]
    assert after(routing, "succeeded") == ids[1]
    assert [routing.row(m)["state"] for m in made] == ["succeeded"] * 2 + [
        "pending"
    ] * 3

    reconcile(routing)
    assert after(routing, "nonterminal") == ids[3]
    assert after(routing, "succeeded") == ids[3]

    report = reconcile(routing)
    # A short page is the end of the range: both cursors wrap to the start.
    assert report.nonterminal.wrapped and report.succeeded.wrapped
    assert after(routing, "nonterminal") is None and after(routing, "succeeded") is None
    assert cursor(routing, "succeeded")["cycles"] == 1  # type: ignore[index]
    assert all(routing.row(m)["state"] == "succeeded" for m in made)
    assert all(routing.row(m)["current_link_revision"] == 1 for m in made)

    # The next cycle revisits old successful handoffs from the start.
    revisit = reconcile(routing)
    assert revisit.succeeded.examined == 2 and after(routing, "succeeded") == ids[1]
    assert revisit.nonterminal.examined == 0 and revisit.nonterminal.wrapped


def test_a_bad_first_page_cannot_starve_later_rows(routing: Routing) -> None:
    blocked = many(routing, 2, approve=False)  # never approved: a no-op page
    (later,) = many(routing, 1, subject="Later inquiry")
    first = reconcile(routing)
    assert first.nonterminal.examined == 2 and first.nonterminal.updated == 0
    assert routing.row(later)["state"] == "pending"
    reconcile(routing)
    assert routing.row(later)["state"] == "succeeded"
    assert all(routing.row(b)["state"] == "pending" for b in blocked)

    # A successful class whose first page Current cannot answer.
    with open_unit_of_work(routing.ctx) as uow:
        for gone in blocked:
            uow.connection.execute(
                update(p.handoff)
                .where(p.handoff.c.id == ident(gone))
                .values(
                    state="succeeded",
                    result_ref="current.item:00000000-0000-7000-8000-0000000000f1",
                )
            )
        uow.commit()
    without_leads_events(routing)
    move_to_project(routing, later, "Later project")
    with open_unit_of_work(routing.ctx) as uow:
        uow.connection.execute(
            update(p.current_link_cursor).values(after_requested_at=None, after_id=None)
        )
        uow.commit()
    bad = reconcile(routing)
    assert bad.succeeded.examined == 2 and bad.succeeded.unavailable == 2
    assert routing.row(later)["current_project_state"] in {"never_assigned", "linked"}
    reconcile(routing)
    assert routing.row(later)["current_project_state"] == "linked"


def test_a_restart_before_commit_repeats_the_page_and_a_repeat_is_idempotent(
    routing: Routing,
) -> None:
    made = many(routing, 2)
    reconcile(routing, commit=False)
    assert cursor(routing, "nonterminal") is None
    assert [routing.row(m)["state"] for m in made] == ["pending", "pending"]
    reconcile(routing)
    settled = [routing.row(m) for m in made]
    assert [s["state"] for s in settled] == ["succeeded", "succeeded"]
    # The same page again changes nothing.
    with open_unit_of_work(routing.ctx) as uow:
        uow.connection.execute(
            update(p.current_link_cursor).values(after_requested_at=None, after_id=None)
        )
        uow.commit()
    again = reconcile(routing)
    assert again.nonterminal.updated == 0 and again.succeeded.updated == 0
    assert [routing.row(m) for m in made] == settled


def test_rows_entering_either_side_of_a_cursor_are_reached(routing: Routing) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    first = [routing.handoff(opportunity, f"early-{n}") for n in range(2)]
    for result in first:
        routing.approve(routing.row(result)["approval_id"])
    routing.run_worker()
    reconcile(routing)  # both scans now sit after the second early handoff
    # After the cursor: a new handoff is reached in this cycle.
    late = routing.handoff(opportunity, "late")
    routing.approve(routing.row(late)["approval_id"])
    routing.run_worker()
    reconcile(routing)
    assert routing.row(late)["state"] == "succeeded"
    # Before the cursor: a lifecycle change on an early handoff is reached next cycle.
    without_leads_events(routing)
    move_to_project(routing, first[0], "Early project")
    reconcile(routing)
    reconcile(routing)
    assert routing.row(first[0])["current_project_state"] == "linked"


# --- lifecycle repair after an earlier scan -------------------------------------------


def project(r: Routing, title: str) -> str:
    made = r.call("current.project.create", title=title)
    assert made.ok, made
    return str(made.result.ref)


def item_update(r: Routing, item_ref: str, **changes: Any) -> None:
    item = r.call("current.item.get", ref=item_ref)
    assert item.ok, item
    updated = r.call(
        "current.item.update", ref=item_ref, revision=item.result.revision, **changes
    )
    assert updated.ok, updated


def move_to_project(r: Routing, handoff: Any, title: str) -> str:
    target = project(r, title)
    item_update(r, r.row(handoff)["result_ref"], project_ref=target)
    return target


def delete(r: Routing, reference: str) -> None:
    held = r.call("core.record.delete", ref=reference)
    assert held.state == "approval_required", held
    done = r.call("core.approval.approve", approval_id=str(held.approval_id))
    assert done.ok and done.result.state != "refused", done


def projection(r: Routing, handoff: Any) -> tuple[Any, ...]:
    row = r.row(handoff)
    return (
        row["current_project_state"],
        row["current_project_ref"],
        row["last_current_project_ref"],
        row["current_link_revision"],
    )


def test_missed_lifecycle_changes_after_an_earlier_scan_are_repaired(
    routing: Routing,
) -> None:
    (result,) = many(routing, 1)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("never_assigned", None, None, 1)
    without_leads_events(routing)
    item_ref = routing.row(result)["result_ref"]
    first = project(routing, "Synthetic first")
    second = project(routing, "Synthetic second")

    item_update(routing, item_ref, project_ref=first)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("linked", first, None, 2)

    item_update(routing, item_ref, project_ref=second)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("linked", second, first, 3)

    item_update(routing, item_ref, clear=["project_ref"])
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("cleared", None, second, 4)

    item_update(routing, item_ref, project_ref=first)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("linked", first, second, 5)

    # Item deletion keeps the project: only Current's item state moves.
    delete(routing, item_ref)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("linked", first, second, 6)
    assert routing.row(result)["result_ref"] == item_ref

    # The project that outlived its item is deleted later.
    delete(routing, first)
    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("deleted", None, first, 7)
    assert routing.row(result)["state"] == "succeeded"


def test_current_absent_denied_or_unready_preserves_the_projection(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    (result,) = many(routing, 1)
    reconcile(routing, page_size=10)
    without_leads_events(routing)
    target = move_to_project(routing, result, "Kept project")
    reconcile(routing, page_size=10)
    kept = projection(routing, result)
    assert kept == ("linked", target, None, 2)
    item_update(routing, routing.row(result)["result_ref"], clear=["project_ref"])

    # Disabled: the repair context, built in its own transaction, no longer has it.
    set_module_state(routing, "current", "disabled")
    report = reconcile(routing, page_size=10)
    assert report.succeeded.examined == 1 and report.succeeded.unavailable == 1
    assert projection(routing, result) == kept
    set_module_state(routing, "current", "enabled")

    # Denied: a service context that does not name the link read.
    denied = routing.service("core.external_action.get")
    report = reconcile(routing, page_size=10, ctx=denied)
    assert report.succeeded.examined == 1 and report.succeeded.unavailable == 1
    assert projection(routing, result) == kept

    real = readiness.require_ready

    def unready(conn: Any, module_id: str) -> None:
        if module_id == "current":
            raise ModuleNotReady("module_unavailable", "synthetic")
        real(conn, module_id)

    with monkeypatch.context() as patched:
        patched.setattr(readiness, "require_ready", unready)
        report = reconcile(routing, page_size=10)
    assert report.succeeded.unavailable == 1 and projection(routing, result) == kept

    reconcile(routing, page_size=10)
    assert projection(routing, result) == ("cleared", None, target, 3)


def test_a_service_context_reads_an_external_action_only_when_granted(
    routing: Routing,
) -> None:
    (result,) = many(routing, 1, approve=False)
    action_id = str(routing.row(result)["external_action_id"])
    with open_unit_of_work(routing.ctx) as uow:
        repair = cl.repair_context(routing.i.workspace, uow)
    assert isinstance(repair, WorkspaceContext) and repair.role is Role.SERVICE
    assert repair.operation_set == cl.REPAIR_OPERATIONS
    read = dispatch(
        repair,
        "core.external_action.get",
        {"external_action_id": action_id},
        registry=routing.i.surfaces.operations,
    )
    assert read.ok and read.result.state == "pending", read
    bare = routing.service()
    refused = dispatch(
        bare,
        "core.external_action.get",
        {"external_action_id": action_id},
        registry=routing.i.surfaces.operations,
    )
    assert refused.state == "operation_not_permitted", refused


def test_the_schedule_runs_the_repair_after_an_approved_effect(
    routing: Routing,
) -> None:
    (result,) = many(routing, 1)
    assert routing.row(result)["state"] == "pending"
    # The schedule was turned on by the stage; a later visit runs it. Nothing else
    # writes the core outcome or the link revision into Leads here.
    routing.run_worker(seconds=int(cl.RECONCILE_INTERVAL_SECONDS) + 5, repair=True)
    row = routing.row(result)
    assert row["state"] == "succeeded" and row["current_link_revision"] == 1


# --- restore --------------------------------------------------------------------------


def restore_elsewhere(
    routing: Routing, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> tuple[UUID, WorkspaceContext]:
    """Export this workspace's Leads rows and import them into a fresh one."""
    with open_unit_of_work(routing.ctx) as uow:
        rows = [
            {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in dict(r).items()
            }
            for r in export_records(SimpleNamespace(connection=uow.connection))  # type: ignore[arg-type]
        ]
    assert not any(r["record_type"] == "leads.current_link_cursor" for r in rows)
    from rheo_core.exports.artifact import _validated_module_rows
    from rheo_leads import MANIFEST

    serialized = _validated_module_rows(MANIFEST, rows, "leads")

    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    for module_id in ("relationships", "leads"):
        install_and_enable_module(routing.i.cluster.backend, operator, other, module_id)
    with open_unit_of_work(operator) as uow:
        import_records(uow, serialized)()
        uow.commit()
    owner = context_for_harness(other, owner_account_id, Role.OWNER)
    assert isinstance(owner, WorkspaceContext)
    return other, owner


def repair_schedule_enabled(ctx: WorkspaceContext) -> bool | None:
    with open_unit_of_work(ctx) as uow:
        found: bool | None = uow.connection.execute(
            select(schedule.c.enabled).where(
                schedule.c.module_id == "leads",
                schedule.c.name == cl.RECONCILE_SCHEDULE,
            )
        ).scalar_one_or_none()
    return found


@pytest.mark.parametrize("destination", ["current", None])
def test_a_restore_with_current_handoffs_turns_the_repair_schedule_on(
    routing: Routing,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
    destination: str | None,
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    routing.handoff(opportunity, "restored", destination_ref=destination)
    other, owner = restore_elsewhere(routing, make_workspace, owner_account_id)
    # Restored successful handoffs are revisited only if the job runs there too;
    # a restore with no handoff to Current has nothing to repair.
    assert bool(repair_schedule_enabled(owner)) is (destination == "current")


def test_a_restored_nonterminal_handoff_is_unresolved_and_never_restaged(
    routing: Routing,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    waiting, delivered = many(routing, 2, approve=False)
    routing.approve(routing.row(delivered)["approval_id"])
    routing.run_worker()
    reconcile(routing, page_size=10)
    assert routing.row(waiting)["state"] == "pending"
    assert routing.row(delivered)["state"] == "succeeded"
    other, owner = restore_elsewhere(routing, make_workspace, owner_account_id)

    def restored(result: Any) -> dict[str, Any]:
        with open_unit_of_work(owner) as uow:
            return dict(
                uow.connection.execute(
                    select(p.handoff).where(p.handoff.c.id == ident(result))
                )
                .mappings()
                .one()
            )

    pending_now = restored(waiting)
    # No core action came with the artifact, so nothing can prove its outcome.
    assert (pending_now["state"], pending_now["result_detail"]) == (
        "unresolved",
        "restored_unresolved",
    )
    assert (
        pending_now["external_action_id"] == routing.row(waiting)["external_action_id"]
    )
    assert pending_now["attempt_count"] == 1 and pending_now["result_ref"] is None
    kept = restored(delivered)
    assert (
        kept["state"] == "succeeded"
        and kept["result_ref"] == routing.row(delivered)["result_ref"]
    )

    refused = dispatch(
        owner,
        "leads.handoff.restage",
        {"handoff_id": str(ident(waiting)), "attempt_count": 1},
        registry=routing.i.surfaces.operations,
        consumers=routing.i.consumers,
    )
    assert refused.state == "handoff_not_restageable", refused
    with open_unit_of_work(owner) as uow:
        repair = cl.repair_context(other, uow)
        assert isinstance(repair, WorkspaceContext)
        report = cl.reconcile(
            repair, HandlerUnitOfWork(uow, consumers=routing.i.consumers), page_size=10
        )
        uow.commit()
    assert report.nonterminal.examined == 0
    assert restored(waiting)["state"] == "unresolved"
    # Current is not installed here: the successful handoff keeps its projection.
    assert report.succeeded.unavailable == 1
    assert restored(delivered)["current_link_revision"] == 1


def test_a_nonterminal_handoff_whose_core_action_is_gone_becomes_unresolved(
    routing: Routing,
) -> None:
    (result,) = many(routing, 1, approve=False)
    with open_unit_of_work(routing.ctx) as uow:
        uow.connection.execute(
            update(p.handoff)
            .where(p.handoff.c.id == ident(result))
            .values(external_action_id=UUID(int=99), approval_id=None)
        )
        uow.commit()
    reconcile(routing)
    row = routing.row(result)
    assert (row["state"], row["result_detail"]) == (
        "unresolved",
        "external_action_missing",
    )
    assert row["result_ref"] is None
    refused = routing.restage(result)
    assert refused.state == "handoff_not_restageable", refused
