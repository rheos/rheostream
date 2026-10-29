"""Leads routes a qualified opportunity to Current through core's approved action.

Synthetic, local-only data throughout. Every request goes through the real public
dispatcher as the requesting person, every approval through core's own approval
operation, and every effect through one real worker visit running core's approved
continuation into Current's registered destination. Where a test needs a core
action in a state the approved path cannot reach cheaply (an exhausted retry
budget, a person's resolution), it writes that one published state onto the real
staged row, satisfying core's own constraints, and says so.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.events import ConsumerRegistry
from rheo_core.modules import DESTINATIONS
from rheo_core.modules.execution import RegisteredDestination
from rheo_core.operations import (
    REGISTRY,
    core_ops,
    dispatch,
    register_core_operations,
    transaction,
)
from rheo_core.operations.continuation import (
    EXTERNAL_ACTION_EXECUTE,
    run_external_continuation,
)
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.idempotency_tables import external_action
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.settings import resolver as settings_resolver
from rheo_core.settings import write_path
from rheo_core.settings.schema import REGISTRY as SETTINGS
from rheo_core.settings.schema import SettingsRegistry
from rheo_core.storage.core_tables import module_state
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job, schedule
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_leads import MANIFEST, pipeline_common
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import func, select, update

from postgres.test_leads_intake import Intake
from postgres.test_leads_pipeline import capture, ok, setup

pytestmark = pytest.mark.postgres

CREATE = "current.item.create_external"
PAYLOAD = {
    "title": "Synthetic delivery",
    "body": "Public-safe body",
    "priority": "high",
}


@dataclass
class Routing:
    """One workspace with Leads, Relationships and (optionally) Current enabled."""

    i: Intake
    with_current: bool
    routed: bool = False

    @property
    def ctx(self) -> WorkspaceContext:
        return self.i.ctx

    def call(
        self, name: str, ctx: WorkspaceContext | None = None, /, **body: Any
    ) -> OperationOutcome:
        return dispatch(
            ctx or self.i.ctx,
            name,
            body,
            registry=self.i.surfaces.operations,
            consumers=self.i.consumers,
        )

    def opportunity(self, subject: str = "Synthetic inquiry") -> Any:
        if not self.routed:
            setup(self.i, "create_opportunity")
            self.routed = True
        capture(self.i, subject, {"subject": subject})
        return next(
            ok(self.i, "opportunity.get", ref=row["ref"])
            for row in ok(self.i, "opportunity.list").items
            if row["title"] == subject
        )

    def get(self, reference: str) -> Any:
        return ok(self.i, "opportunity.get", ref=reference)

    def qualify(self, opportunity: Any) -> Any:
        current = self.get(opportunity.ref)
        return ok(
            self.i,
            "qualification.assess",
            ref=current.ref,
            revision=current.revision,
            objective="Synthetic suitability check",
        )

    def request(
        self,
        opportunity: Any,
        key: str = "handoff-one",
        ctx: WorkspaceContext | None = None,
        **changes: Any,
    ) -> OperationOutcome:
        current = self.get(opportunity.ref)
        body = dict(
            ref=current.ref,
            revision=current.revision,
            purpose="internal_analysis",
            destination_ref="current",
            payload=PAYLOAD,
            idempotency_key=key,
        )
        body.update(changes)
        return self.call("leads.handoff.request", ctx, **body)

    def handoff(
        self, opportunity: Any, key: str = "handoff-one", **changes: Any
    ) -> Any:
        outcome = self.request(opportunity, key, **changes)
        assert outcome.ok, outcome
        return outcome.result

    def restage(
        self,
        handoff: Any,
        attempt: int | None = None,
        ctx: WorkspaceContext | None = None,
    ) -> OperationOutcome:
        row = self.row(handoff)
        return self.call(
            "leads.handoff.restage",
            ctx,
            handoff_id=str(row["id"]),
            attempt_count=row["attempt_count"] if attempt is None else attempt,
        )

    def approve(self, approval_id: object) -> None:
        approved = self.call("core.approval.approve", approval_id=str(approval_id))
        assert approved.ok, approved

    def run_worker(self, *, seconds: int = 1, repair: bool = False) -> None:
        """One real worker visit. The repair job runs only when ``repair`` is set,
        so a test that drives the job directly sees exactly its own runs."""
        from rheo_leads import current_links

        kinds = JobKindRegistry()
        kinds.register_continuation(EXTERNAL_ACTION_EXECUTE, run_external_continuation)
        for kind in MANIFEST.jobs:
            if repair and kind.name == current_links.RECONCILE_JOB_KIND:
                kinds.register(kind.name, kind.input_model, kind.handler)
        at = datetime.now(UTC) + timedelta(seconds=seconds)
        visit_workspace(
            DueWorkspace(workspace_id=self.i.workspace, observed_due_at=None),
            kinds=kinds,
            consumers=self.i.consumers,
            backend=self.i.cluster.backend,
            owner="leads-current-handoff-test",
            clock=lambda: at,
            jitter=None,
        )

    def row(self, handoff: Any) -> dict[str, Any]:
        identifier = RecordRef.parse(handoff.ref).id
        with open_unit_of_work(self.ctx) as uow:
            found = (
                uow.connection.execute(
                    select(p.handoff).where(p.handoff.c.id == identifier)
                )
                .mappings()
                .one()
            )
            snapshot = (
                uow.connection.execute(
                    select(p.handoff_snapshot).where(
                        p.handoff_snapshot.c.handoff_id == identifier
                    )
                )
                .mappings()
                .one()
            )
        return {**dict(found), "snapshot": dict(snapshot)}

    def actions(self) -> list[dict[str, Any]]:
        with open_unit_of_work(self.ctx) as uow:
            return [
                dict(r)
                for r in uow.connection.execute(
                    select(external_action).order_by(external_action.c.created_at)
                ).mappings()
            ]

    def count(self, table: Any) -> int:
        return self.i.count(table)

    def current_items(self) -> list[dict[str, Any]]:
        """Every Current item with its source, through Current's own public reads."""
        listed = self.call("current.item.list", status="all")
        assert listed.ok, listed
        found = []
        for summary in listed.result.items:
            detail = self.call("current.item.get", ref=summary.ref)
            assert detail.ok, detail
            found.append({"ref": summary.ref, "source_ref": detail.result.source_ref})
        return found

    def service(self, *names: str) -> WorkspaceContext:
        """A restricted service context from core's boundary factory."""
        with open_unit_of_work(self.ctx) as uow:
            ctx = context_for_event_consumer(
                self.i.workspace,
                uow,
                request_id=uuid7(),
                operation_names=frozenset(names),
            )
        assert isinstance(ctx, WorkspaceContext) and ctx.role is Role.SERVICE
        return ctx

    def set_action(self, action_id: object, **values: Any) -> None:
        """Write one published core state onto a real staged action (see module doc)."""
        moment = datetime.now(UTC)
        with open_unit_of_work(self.ctx) as uow:
            uow.connection.execute(
                update(external_action)
                .where(external_action.c.id == action_id)
                .values(updated_at=moment, **values)
            )
            uow.commit()

    def destination_ready(self, monkeypatch: pytest.MonkeyPatch, ready: bool) -> None:
        """Make Current's registered destination report not ready, or ready again."""
        found = DESTINATIONS.lookup("current")
        assert found is not None
        original = getattr(self, "_original", None) or found
        self._original = original  # type: ignore[attr-defined]
        replaced = (
            original
            if ready
            else RegisteredDestination(
                original.registration.model_copy(
                    update={"is_ready": lambda ctx, uow: False}
                ),
                original.origin,
            )
        )
        monkeypatch.setitem(DESTINATIONS._destinations, "current", replaced)


@contextmanager
def routing_harness(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_current: bool,
) -> Iterator[Routing]:
    names = ("leads", "relationships") + (("current",) if with_current else ())
    monkeypatch.setattr(
        OWNED_DELETIONS, "_participants", list(OWNED_DELETIONS._participants)
    )
    with loaded_probe_modules(
        monkeypatch, *names, deletions=OWNED_DELETIONS
    ) as surfaces:
        settings = SettingsRegistry()
        for spec in SETTINGS.specs():
            settings.register(spec, origin=SETTINGS.origin_of(spec.key))
        from rheo_core.modules.loader import loaded_manifests

        manifests = loaded_manifests()
        for manifest in manifests.values():
            for spec in manifest.configuration_schema:
                if spec.key not in {v.key for v in settings.specs()}:
                    settings.register(spec, origin=manifest.module_id)
        monkeypatch.setattr(settings_resolver, "REGISTRY", settings)
        monkeypatch.setattr(core_ops, "SETTINGS_REGISTRY", settings)
        monkeypatch.setattr(write_path, "REGISTRY", settings)
        monkeypatch.setattr(pipeline_common, "REGISTRY", surfaces.operations)
        monkeypatch.setattr(transaction, "REGISTRY", surfaces.operations)
        register_core_operations()
        register_core_operations(registry=surfaces.operations)
        # The continuation rebuilds the held caller against the process registry.
        monkeypatch.setattr(
            REGISTRY, "_operations", dict(surfaces.operations._operations)
        )
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        for module_id in ("relationships", "leads") + (
            ("current",) if with_current else ()
        ):
            install_and_enable_module(cluster.backend, operator, workspace, module_id)
        for manifest in manifests.values():
            for name, resolver in manifest.resolvers:
                register_resolver(
                    manifest.module_id, name, resolver, origin=manifest.module_id
                )
        ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(ctx, WorkspaceContext)
        consumers = ConsumerRegistry()
        for manifest in manifests.values():
            for subscription in manifest.subscriptions:
                consumers.register(subscription)
        with open_unit_of_work(ctx) as uow:
            connection_id = uow.connection.execute(
                select(t.intake_connection.c.id)
            ).scalar_one()
            funnel_id = uow.connection.execute(select(t.funnel.c.id)).scalar_one()
        intake = Intake(
            cluster, workspace, ctx, surfaces, consumers, connection_id, funnel_id
        )
        assert {"leads", "relationships"} <= ctx.enabled_modules
        yield Routing(intake, with_current)


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


@pytest.fixture
def leads_only(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Routing]:
    with routing_harness(
        cluster, workspace, owner_account_id, monkeypatch, with_current=False
    ) as harness:
        yield harness


def staged(r: Routing, subject: str = "Synthetic inquiry", key: str = "one") -> Any:
    opportunity = r.opportunity(subject)
    r.qualify(opportunity)
    return r.handoff(opportunity, key)


# --- request: qualification, snapshot, staging -------------------------------------


def test_handoff_without_a_qualification_refuses_and_writes_nothing(
    routing: Routing,
) -> None:
    opportunity = routing.opportunity()
    refused = routing.request(opportunity)
    assert refused.state == "qualification_required", refused
    assert routing.count(p.handoff) == 0 and routing.actions() == []
    assert routing.get(opportunity.ref).revision == opportunity.revision


def test_handoff_snapshots_the_latest_qualification_and_stale_only_warns(
    routing: Routing,
) -> None:
    fresh = routing.opportunity("Fresh evidence")
    first = routing.qualify(fresh)
    latest = routing.qualify(fresh)
    fresh_handoff = routing.handoff(fresh, "fresh")
    snapshot = routing.row(fresh_handoff)["snapshot"]
    assert snapshot["qualification_ref"] == latest.ref != first.ref
    assert snapshot["qualification_stale"] is False

    stale = routing.opportunity("Stale evidence")
    routing.qualify(stale)
    current = routing.get(stale.ref)
    ok(
        routing.i,
        "opportunity.update",
        ref=current.ref,
        revision=current.revision,
        value_amount="900.00",
        value_currency="USD",
        value_basis="project_fee",
    )
    # A stale qualification is recorded and never blocks: there is no threshold.
    stale_handoff = routing.handoff(stale, "stale")
    stale_row = routing.row(stale_handoff)
    assert stale_row["snapshot"]["qualification_stale"] is True
    assert stale_row["state"] == "pending" and stale_row["attempt_count"] == 1
    assert stale_handoff.data["qualification_stale"] is True


def test_handoff_stages_one_held_attempt_and_leaves_stage_and_outcome(
    routing: Routing,
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    before = routing.get(opportunity.ref)
    result = routing.handoff(opportunity)
    row = routing.row(result)
    (action,) = routing.actions()
    assert row["state"] == "pending" and row["attempt_count"] == 1
    assert row["external_action_id"] == action["id"]
    assert row["approval_id"] == action["approval_id"]
    assert action["operation_name"] == CREATE and action["state"] == "pending"
    assert action["idempotency_key"] == result.ref + "#1"
    assert action["source_ref"] == result.ref
    assert routing.current_items() == []
    after = routing.get(opportunity.ref)
    assert (after.data["stage_id"], after.data["disposition_outcome"]) == (
        before.data["stage_id"],
        before.data["disposition_outcome"],
    )
    # The first staged handoff turns the repair schedule on.
    with open_unit_of_work(routing.ctx) as uow:
        assert uow.connection.execute(
            select(schedule.c.enabled).where(
                schedule.c.module_id == "leads",
                schedule.c.name == "current_links_reconcile",
            )
        ).scalar_one()


def test_a_service_context_cannot_stage_a_handoff(routing: Routing) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    service = routing.service("leads.handoff.request", CREATE)
    refused = routing.request(opportunity, ctx=service)
    assert not refused.ok and refused.state == "role_not_permitted", refused
    assert routing.count(p.handoff) == 0 and routing.actions() == []


def test_same_key_same_snapshot_returns_the_same_handoff_after_a_lost_response(
    routing: Routing,
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    first = routing.handoff(opportunity)
    routing.approve(first.data["approval_id"])
    routing.run_worker()
    # The response was lost; the person sends the same request again.
    again = routing.handoff(opportunity)
    assert again.ref == first.ref
    assert again.data["state"] == "succeeded"
    (item,) = routing.current_items()
    assert again.data["result_ref"] == item["ref"]
    assert item["source_ref"] == first.ref
    assert len(routing.actions()) == 1 and routing.count(p.handoff) == 1
    # A later qualification does not make the same request a different one.
    routing.qualify(opportunity)
    assert routing.handoff(opportunity).ref == first.ref
    assert stage_outcome(routing, opportunity) == ("triage", None)


def stage_outcome(r: Routing, opportunity: Any) -> tuple[str, str | None]:
    data = r.get(opportunity.ref).data
    return data["stage_id"], data["disposition_outcome"]


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {**PAYLOAD, "title": "Changed"}},
        {"destination_ref": None},
        {"purpose": "share_with_referral"},
    ],
)
def test_a_changed_request_under_a_used_key_refuses(
    routing: Routing, change: dict[str, Any]
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    routing.handoff(opportunity)
    refused = routing.request(opportunity, **change)
    assert refused.state == "idempotency_key_reused", refused
    assert len(routing.actions()) == 1 and routing.count(p.handoff) == 1


def test_an_unknown_destination_or_routing_field_is_refused(routing: Routing) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    unknown = routing.request(opportunity, destination_ref="example.elsewhere")
    assert unknown.state == "input_invalid", unknown
    planted = routing.request(
        opportunity, "planted", payload={**PAYLOAD, "source_ref": "leads.handoff:x"}
    )
    assert planted.state == "input_invalid", planted
    extra = routing.request(opportunity, "extra", payload={**PAYLOAD, "project": "p"})
    assert extra.state == "input_invalid", extra
    assert routing.count(p.handoff) == 0 and routing.actions() == []


# --- optional destination ---------------------------------------------------------


def test_current_absent_at_request_is_a_local_handoff_with_no_effect(
    leads_only: Routing,
) -> None:
    r = leads_only
    opportunity = r.opportunity()
    r.qualify(opportunity)
    result = r.handoff(opportunity)
    row = r.row(result)
    assert (row["state"], row["result_detail"]) == (
        "unavailable",
        "destination_unavailable",
    )
    assert row["attempt_count"] == 0 and row["external_action_id"] is None
    assert row["approval_id"] is None
    assert r.actions() == []
    with open_unit_of_work(r.ctx) as uow:
        assert (
            uow.connection.execute(
                select(func.count())
                .select_from(job)
                .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            ).scalar_one()
            == 0
        )
    # Still absent: a restage refuses and changes nothing.
    again = r.restage(result)
    assert again.state == "destination_unavailable", again
    assert r.row(result)["attempt_count"] == 0
    # Leads keeps working without Current.
    assert ok(r.i, "handoff.list", ref=opportunity.ref).items[0].ref == result.ref


def set_module_state(r: Routing, module_id: str, state: str) -> None:
    """Turn an installed module off or back on in this workspace's own rows."""
    with open_unit_of_work(r.ctx) as uow:
        uow.connection.execute(
            update(module_state)
            .where(module_state.c.module_id == module_id)
            .values(state=state)
        )
        uow.commit()


@pytest.mark.parametrize("cause", ["disabled", "not_ready"])
def test_request_time_unavailable_restages_once_current_is_ready(
    routing: Routing, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    if cause == "disabled":
        set_module_state(routing, "current", "disabled")
    else:
        routing.destination_ready(monkeypatch, False)
    result = routing.request(opportunity)
    assert result.ok, result
    row = routing.row(result.result)
    assert (row["state"], row["result_detail"]) == (
        "unavailable",
        "destination_unavailable",
    )
    assert routing.actions() == [] and row["approval_id"] is None

    if cause == "disabled":
        set_module_state(routing, "current", "enabled")
    else:
        routing.destination_ready(monkeypatch, True)
    restaged = routing.restage(result.result)
    assert restaged.ok, restaged
    row = routing.row(result.result)
    (action,) = routing.actions()
    assert (row["state"], row["attempt_count"]) == ("pending", 1)
    assert action["idempotency_key"] == result.result.ref + "#1"
    assert action["source_ref"] == result.result.ref


# --- the closed restage matrix ------------------------------------------------------


def test_destination_unavailable_no_effect_restages_with_a_new_approval(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = staged(routing)
    first = routing.row(result)
    routing.approve(first["approval_id"])
    routing.destination_ready(monkeypatch, False)
    routing.run_worker()
    (refused,) = routing.actions()
    assert refused["failure_class"] == "destination_unavailable_no_effect"
    assert routing.current_items() == []
    routing.destination_ready(monkeypatch, True)

    restaged = routing.restage(result)
    assert restaged.ok, restaged
    second = routing.row(result)
    assert second["attempt_count"] == 2 and second["state"] == "pending"
    assert second["approval_id"] != first["approval_id"]
    assert second["external_action_id"] != first["external_action_id"]
    actions = routing.actions()
    assert [a["idempotency_key"] for a in actions] == [
        result.ref + "#1",
        result.ref + "#2",
    ]
    assert {a["source_ref"] for a in actions} == {result.ref}
    # A restage is never approved by itself: nothing runs until a person approves.
    routing.run_worker()
    assert routing.current_items() == []
    routing.approve(second["approval_id"])
    routing.run_worker()
    (item,) = routing.current_items()
    assert item["source_ref"] == result.ref


def _terminal(state: str, failure_class: str | None, **extra: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "state": state,
        "failure_class": failure_class,
        "terminal_at": datetime.now(UTC),
    }
    if state != "succeeded":
        values["error_code"] = "synthetic_" + state
    values.update(extra)
    return values


ALLOWED = {
    "exhausted_retryable": _terminal("failed", "retryable_no_effect"),
    "resolved_failed": _terminal("failed", "permanent_refusal_no_effect"),
    "destination_unavailable": _terminal(
        "refused", "destination_unavailable_no_effect"
    ),
}
FORBIDDEN = {
    "pending": {},
    "succeeded": _terminal(
        "succeeded",
        None,
        result_ref="current.item:00000000-0000-7000-8000-0000000000e1",
    ),
    "permanent_refusal": _terminal("refused", "permanent_refusal_no_effect"),
    "cancelled": _terminal("cancelled", "cancelled_no_effect"),
    "unresolved": _terminal("unresolved", "outcome_unknown"),
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_each_allowed_core_outcome_restages_the_same_handoff(
    routing: Routing, name: str
) -> None:
    result = staged(routing)
    first = routing.row(result)
    routing.set_action(first["external_action_id"], **ALLOWED[name])
    restaged = routing.restage(result)
    assert restaged.ok, restaged
    second = routing.row(result)
    assert second["id"] == first["id"] and second["attempt_count"] == 2
    assert second["state"] == "pending"
    assert second["approval_id"] != first["approval_id"]
    assert routing.actions()[-1]["idempotency_key"] == result.ref + "#2"


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_each_forbidden_core_outcome_refuses_a_restage(
    routing: Routing, name: str
) -> None:
    result = staged(routing)
    first = routing.row(result)
    if FORBIDDEN[name]:
        routing.set_action(first["external_action_id"], **FORBIDDEN[name])
    refused = routing.restage(result)
    assert refused.state == "handoff_not_restageable", refused
    after = routing.row(result)
    assert after["attempt_count"] == 1 and len(routing.actions()) == 1
    assert after["approval_id"] == first["approval_id"]


def test_a_restage_replays_a_lost_response_and_refuses_a_stale_attempt(
    routing: Routing,
) -> None:
    result = staged(routing)
    first = routing.row(result)
    routing.set_action(first["external_action_id"], **ALLOWED["exhausted_retryable"])
    restaged = routing.restage(result, attempt=1)
    assert restaged.ok, restaged
    # The same restage again, after a lost response: the same answer, no new stage.
    replayed = routing.restage(result, attempt=1)
    assert replayed.ok and replayed.result.data["attempt_count"] == 2, replayed
    assert len(routing.actions()) == 2
    stale = routing.restage(result, attempt=0)
    assert stale.state == "record_stale", stale


def test_a_member_may_restage_and_a_service_context_may_not(routing: Routing) -> None:
    result = staged(routing)
    routing.set_action(
        routing.row(result)["external_action_id"], **ALLOWED["exhausted_retryable"]
    )
    service = routing.service("leads.handoff.restage", CREATE)
    refused = routing.restage(result, ctx=service)
    assert refused.state == "role_not_permitted", refused
    assert routing.row(result)["attempt_count"] == 1

    account = add_member(
        routing.i.cluster.backend, routing.i.workspace, Role.MEMBER, display_name="m"
    )
    member = context_for_harness(routing.i.workspace, account, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)
    restaged = routing.restage(result, ctx=member)
    assert restaged.ok, restaged
    row = routing.row(result)
    assert row["attempt_count"] == 2 and row["state"] == "pending"
    (_, action) = routing.actions()
    assert action["idempotency_key"] == result.ref + "#2"


# --- history ------------------------------------------------------------------------


def test_handoff_history_pages_with_ids_and_never_shows_the_snapshot(
    routing: Routing,
) -> None:
    opportunity = routing.opportunity()
    routing.qualify(opportunity)
    older = routing.handoff(opportunity, "older")
    newer = routing.handoff(opportunity, "newer")
    page = ok(routing.i, "handoff.list", ref=opportunity.ref, limit=1)
    assert [row.ref for row in page.items] == [newer.ref]
    assert page.next_offset == 1
    rest = ok(routing.i, "handoff.list", ref=opportunity.ref, offset=1, limit=1)
    assert [row.ref for row in rest.items] == [older.ref] and rest.next_offset is None
    (row,) = rest.model_dump(mode="json")["items"]
    stored = routing.row(older)
    assert row["external_action_id"] == str(stored["external_action_id"])
    assert row["approval_id"] == str(stored["approval_id"])
    assert row["attempt_count"] == 1 and row["state"] == "pending"
    assert row["current_project_state"] == "never_assigned"
    assert row["qualification_ref"] == stored["snapshot"]["qualification_ref"]
    assert not {"snapshot", "payload", "body", "snapshot_digest"} & set(row)
    assert "Synthetic delivery" not in str(page.model_dump(mode="json"))


# --- the newer-revision projection --------------------------------------------------


def _delivered(routing: Routing) -> Any:
    """A handoff whose item exists, with the core outcome already in Leads."""
    result = staged(routing)
    routing.approve(routing.row(result)["approval_id"])
    routing.run_worker()
    assert (
        routing.handoff(routing_opportunity(routing, result), "one").data["state"]
        == "succeeded"
    )
    return result


def routing_opportunity(routing: Routing, handoff: Any) -> Any:
    return routing.get(f"leads.opportunity:{routing.row(handoff)['opportunity_id']}")


def test_link_events_copy_only_a_newer_revision_into_the_handoff(
    routing: Routing,
) -> None:
    from rheo_leads import current_links

    result = _delivered(routing)
    item_ref = routing.row(result)["result_ref"]
    made = routing.call("current.project.create", title="Synthetic project")
    assert made.ok, made
    project_ref = made.result.ref
    item = routing.call("current.item.get", ref=item_ref)
    assert item.ok, item
    assigned = routing.call(
        "current.item.update",
        ref=item_ref,
        revision=item.result.revision,
        project_ref=project_ref,
    )
    assert assigned.ok, assigned
    routing.run_worker()  # delivers current.source_item_link.changed to Leads
    row = routing.row(result)
    assert (row["current_project_state"], row["current_project_ref"]) == (
        "linked",
        project_ref,
    )
    assert row["current_link_revision"] == 2

    # A redelivered or reordered older event changes nothing.
    older = current_links.CurrentLink.model_validate(
        {
            "source_ref": result.ref,
            "item_ref": item_ref,
            "project_ref": None,
            "last_project_ref": None,
            "item_state": "available",
            "project_state": "never_assigned",
            "revision": 1,
            "changed_at": datetime.now(UTC).isoformat(),
        }
    )
    with open_unit_of_work(routing.ctx) as uow:
        assert current_links.apply_link(uow, older) is False
        uow.commit()
    assert routing.row(result)["current_link_revision"] == 2
    # Stage and outcome never move with Current.
    assert stage_outcome(routing, routing_opportunity(routing, result)) == (
        "triage",
        None,
    )
