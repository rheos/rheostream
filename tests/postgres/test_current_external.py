"""Approved Current external destination and its independent source-link dedup.

Synthetic, local-only data throughout. The approved path runs the real public
dispatcher, the shell-equivalent approval operation and one real worker visit; the
source-pair tests call Current's registered destination directly, with no core
keyed row at all, so the two retry layers are proven apart.
"""

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.events import ConsumerRegistry
from rheo_core.modules import DESTINATIONS
from rheo_core.modules.production import assert_production_external_composition
from rheo_core.operations import REGISTRY, dispatch, register_core_operations
from rheo_core.operations.continuation import (
    EXTERNAL_ACTION_EXECUTE,
    ExternalDestinationError,
    ExternalFailureClass,
    run_external_continuation,
)
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.idempotency_tables import (
    external_action,
    idempotency_result,
)
from rheo_core.refs import uuid7
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import outbox_event
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_current import external
from rheo_current.contracts import ExternalItemCreate, ExternalItemCreated
from rheo_current.storage import tables as t
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

OPERATION = "current.item.create_external"
SOURCE_REF = "leads.handoff:00000000-0000-7000-8000-000000000001"
OTHER_SOURCE = "leads.handoff:00000000-0000-7000-8000-000000000002"
ITEM_PREFIX = "current.item:00000000-0000-7000-8000-"


def payload(
    *, key: str = "handoff-1#1", source: str = SOURCE_REF, **extra: object
) -> dict[str, object]:
    return {
        "destination_ref": "current",
        "source_ref": source,
        "title": "Prepare synthetic delivery",
        "body": "Public-safe fixture body",
        "priority": "high",
        "area": "work",
        "due_on": "2030-02-03",
        "idempotency_key": key,
        **extra,
    }


@dataclass
class Harness:
    cluster: ClusterSession
    owner: WorkspaceContext
    service: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry
    calls: list[str]

    def stage(self, **kwargs: Any) -> OperationOutcome:
        """The public dispatcher, as the person a handoff request comes from."""
        outcome = dispatch(
            self.owner,
            OPERATION,
            payload(**kwargs),
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )
        return outcome

    def approve_and_run(self, held: OperationOutcome) -> None:
        assert held.state == "approval_required", held
        assert held.approval_id is not None
        approved = dispatch(
            self.owner,
            "core.approval.approve",
            {"approval_id": str(held.approval_id)},
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )
        assert approved.ok, approved
        kinds = JobKindRegistry()
        kinds.register_continuation(EXTERNAL_ACTION_EXECUTE, run_external_continuation)
        at = datetime.now(UTC) + timedelta(seconds=1)
        visit_workspace(
            DueWorkspace(workspace_id=self.owner.workspace_id, observed_due_at=None),
            kinds=kinds,
            consumers=self.consumers,
            backend=self.cluster.backend,
            owner="current-external-test",
            clock=lambda: at,
            jitter=None,
        )

    def execute(self, uow: Any, **kwargs: Any) -> ExternalItemCreated:
        """The registered destination itself, with no core keyed row involved."""
        found = DESTINATIONS.lookup("current")
        assert found is not None
        output = found.registration.execute(
            self.owner,
            HandlerUnitOfWork(uow, consumers=self.consumers),
            ExternalItemCreate.model_validate(payload(**kwargs)),
        )
        assert isinstance(output, ExternalItemCreated)
        return output

    def rows(self, table: Any) -> list[dict[str, Any]]:
        with open_unit_of_work(self.owner) as uow:
            return [dict(r) for r in uow.connection.execute(select(table)).mappings()]

    def action(self, held: OperationOutcome) -> dict[str, Any]:
        with open_unit_of_work(self.owner) as uow:
            return dict(
                uow.connection.execute(
                    select(external_action).where(
                        external_action.c.operation_id == held.operation_id
                    )
                )
                .mappings()
                .one()
            )


@pytest.fixture
def current_external(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Harness]:
    with loaded_probe_modules(monkeypatch, "current") as surfaces:
        register_core_operations()
        register_core_operations(surfaces.operations)
        monkeypatch.setattr(
            REGISTRY, "_operations", dict(surfaces.operations._operations)
        )
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        install_and_enable_module(cluster.backend, operator, workspace, "current")
        owner = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(owner, WorkspaceContext)
        with open_unit_of_work(owner) as uow:
            service = context_for_event_consumer(
                workspace,
                uow,
                request_id=uuid7(),
                operation_names=frozenset({OPERATION}),
            )
        assert isinstance(service, WorkspaceContext)
        # Count every real destination call without changing what it does.
        calls: list[str] = []
        real_create = external.create

        def counted(ctx: WorkspaceContext, uow: Any, data: Any) -> Any:
            calls.append(data.idempotency_key)
            return real_create(ctx, uow, data)

        monkeypatch.setattr(external, "create", counted)
        yield Harness(cluster, owner, service, surfaces, ConsumerRegistry(), calls)


def test_external_approved_path_replays_core_result_without_reinvoking_destination(
    current_external: Harness,
) -> None:
    h = current_external
    held = h.stage()
    assert h.rows(t.item) == []  # staging alone never touches Current
    h.approve_and_run(held)
    assert h.calls == ["handoff-1#1"]

    action = h.action(held)
    assert action["state"] == "succeeded", action
    items = h.rows(t.item)
    assert len(items) == 1
    item = items[0]
    item_ref = f"current.item:{item['id']}"
    assert action["result_ref"] == item_ref
    # One standalone item: no project, never folded, exactly the approved values.
    assert item["project_id"] is None
    assert (item["title"], item["body"], item["priority"], item["area"]) == (
        "Prepare synthetic delivery",
        "Public-safe fixture body",
        "high",
        "work",
    )
    assert str(item["due_on"]) == "2030-02-03"
    assert (item["status"], item["event_count"], item["revision"]) == ("open", 1, 1)
    assert (item["provenance"], item["source_kind"], item["source_ref"]) == (
        "handoff",
        "handoff",
        SOURCE_REF,
    )
    link = h.rows(t.source_item_link)
    assert link == [
        {
            "source_ref": SOURCE_REF,
            "item_ref": item_ref,
            "revision": 1,
            "item_state": "available",
            "project_ref": None,
            "last_project_ref": None,
            "project_state": "never_assigned",
            "changed_at": link[0]["changed_at"],
        }
    ]
    assert h.rows(t.comment) == [] and h.rows(t.signal) == []
    created = [r for r in h.rows(outbox_event) if r["type"].startswith("current.")]
    assert [(r["type"], r["subject_ref"]) for r in created] == [
        ("current.item.created", item_ref)
    ]

    # Core's retained KEYED replay: the same approved request answers with the
    # stored Current reference and never reaches the destination again.
    replay = h.stage()
    assert replay.ok, replay
    assert replay.result == ExternalItemCreated(item_ref=item_ref)
    assert h.calls == ["handoff-1#1"]
    assert len(h.rows(t.item)) == 1 and len(h.rows(external_action)) == 1
    envelopes = h.rows(idempotency_result)
    assert [(e["idempotency_key"], e["state"]) for e in envelopes] == [
        ("handoff-1#1", "succeeded")
    ]


def test_source_pair_dedup_is_independent_and_tombstone_refuses_recreation(
    current_external: Harness,
) -> None:
    h = current_external
    with open_unit_of_work(h.service) as uow:
        first = h.execute(uow, key="first")
        second = h.execute(uow, key="second")
        assert first == second
        assert h.calls == ["first", "second"]  # both reached Current: no core row
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.item)
            ).scalar_one()
            == 1
        )
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.source_item_link)
            ).scalar_one()
            == 1
        )
        uow.connection.execute(
            update(t.source_item_link)
            .where(t.source_item_link.c.source_ref == SOURCE_REF)
            .values(item_state="deleted", changed_at=datetime.now(UTC))
        )
        uow.connection.execute(delete(t.item))
        with pytest.raises(ExternalDestinationError, match="source_tombstoned") as err:
            h.execute(uow, key="third")
        assert (
            err.value.failure_class is ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT
        )
        assert "tombstone" in err.value.detail
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.item)
            ).scalar_one()
            == 0
        )


def test_distinct_core_keys_for_one_source_converge_on_one_item(
    current_external: Harness,
) -> None:
    """Current's own dedup, through the real approved path: a new key is a new core
    envelope, so the destination does run, and still returns the first item."""
    h = current_external
    first = h.stage(key="handoff-1#1")
    h.approve_and_run(first)
    second = h.stage(key="handoff-1#2", priority="critical", title="Changed title")
    h.approve_and_run(second)
    assert h.calls == ["handoff-1#1", "handoff-1#2"]
    assert h.action(first)["result_ref"] == h.action(second)["result_ref"]
    items = h.rows(t.item)
    assert len(items) == 1 and len(h.rows(t.source_item_link)) == 1
    # Never a repeat signal: no count, no priority raise, no new title, no comment.
    assert (items[0]["event_count"], items[0]["priority"], items[0]["title"]) == (
        1,
        "high",
        "Prepare synthetic delivery",
    )
    assert h.rows(t.comment) == []


def test_source_index_recovers_after_the_core_row_expires(
    current_external: Harness,
) -> None:
    """With the retained envelope swept, the same approved key reaches Current again
    and the source link still answers with the one item."""
    h = current_external
    held = h.stage()
    h.approve_and_run(held)
    item_ref = h.action(held)["result_ref"]
    with open_unit_of_work(h.owner) as uow:
        uow.connection.execute(delete(external_action))
        uow.connection.execute(delete(idempotency_result))
        uow.commit()
    again = h.stage()
    h.approve_and_run(again)
    assert h.calls == ["handoff-1#1", "handoff-1#1"]
    assert h.action(again)["result_ref"] == item_ref
    assert len(h.rows(t.item)) == 1


def test_retry_after_completion_never_reopens_or_folds(
    current_external: Harness,
) -> None:
    h = current_external
    with open_unit_of_work(h.service) as uow:
        made = h.execute(uow, key="first")
        identifier = RecordRef.parse(made.item_ref).id
        uow.connection.execute(
            update(t.item)
            .where(t.item.c.id == identifier)
            .values(status="done", closed_at=datetime.now(UTC))
        )
        again = h.execute(uow, key="second", priority="critical")
        assert again == made
        row = (
            uow.connection.execute(select(t.item).where(t.item.c.id == identifier))
            .mappings()
            .one()
        )
        assert (row["status"], row["priority"], row["event_count"]) == (
            "done",
            "high",
            1,
        )
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.comment)
            ).scalar_one()
            == 0
        )


def test_a_direct_item_with_the_source_pair_is_linked_not_duplicated(
    current_external: Harness,
) -> None:
    """``uq_item_source`` is the backstop: an item created another way with the same
    pair is adopted, its project carried into the link as live."""
    h = current_external
    project = dispatch(
        h.owner,
        "current.project.create",
        {"title": "Synthetic project"},
        registry=h.surfaces.operations,
        consumers=h.consumers,
    )
    assert project.ok, project
    direct = dispatch(
        h.owner,
        "current.item.create",
        {
            "title": "Made directly",
            "source_kind": "handoff",
            "source_ref": SOURCE_REF,
            "project_ref": project.result.ref,
        },
        registry=h.surfaces.operations,
        consumers=h.consumers,
    )
    assert direct.ok, direct
    with open_unit_of_work(h.service) as uow:
        made = h.execute(uow)
        uow.commit()
    assert made.item_ref == direct.result.item.ref
    assert len(h.rows(t.item)) == 1
    [link] = h.rows(t.source_item_link)
    assert (link["item_ref"], link["project_ref"], link["project_state"]) == (
        made.item_ref,
        project.result.ref,
        "linked",
    )
    assert h.rows(t.item)[0]["event_count"] == 1


def test_same_source_concurrent_creates_make_one_item(
    current_external: Harness,
) -> None:
    h = current_external
    barrier = threading.Barrier(2, timeout=60)
    results: list[str] = []
    failures: list[BaseException] = []

    def worker(key: str) -> None:
        try:
            with open_unit_of_work(h.service) as uow:
                barrier.wait()
                results.append(h.execute(uow, key=key).item_ref)
                uow.commit()
        except BaseException as exc:  # surfaced below, never swallowed
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == 2 and results[0] == results[1]
    assert len(h.rows(t.item)) == 1 and len(h.rows(t.source_item_link)) == 1


def test_distinct_sources_make_distinct_items(current_external: Harness) -> None:
    h = current_external
    with open_unit_of_work(h.service) as uow:
        one = h.execute(uow, key="one")
        two = h.execute(uow, key="two", source=OTHER_SOURCE)
        uow.commit()
    assert one != two
    links = {r["source_ref"]: r["item_ref"] for r in h.rows(t.source_item_link)}
    assert links == {SOURCE_REF: one.item_ref, OTHER_SOURCE: two.item_ref}
    assert len(h.rows(t.item)) == 2


def test_a_tombstoned_source_settles_refused_through_the_approved_path(
    current_external: Harness,
) -> None:
    """A deleted source is a permanent no-effect refusal, never a retry."""
    h = current_external
    held = h.stage()
    h.approve_and_run(held)
    with open_unit_of_work(h.owner) as uow:
        uow.connection.execute(
            update(t.source_item_link).values(
                item_state="deleted", changed_at=datetime.now(UTC)
            )
        )
        uow.connection.execute(delete(t.item))
        uow.commit()
    again = h.stage(key="handoff-1#2")
    h.approve_and_run(again)
    action = h.action(again)
    assert action["state"] == "refused", action
    assert action["failure_class"] == "permanent_refusal_no_effect"
    assert h.rows(t.item) == []


def test_a_link_whose_item_row_is_gone_also_refuses(current_external: Harness) -> None:
    h = current_external
    with open_unit_of_work(h.service) as uow:
        h.execute(uow, key="first")
        uow.connection.execute(delete(t.item))
        with pytest.raises(ExternalDestinationError, match="source_tombstoned"):
            h.execute(uow, key="second")


def test_an_unusable_area_is_a_permanent_refusal(current_external: Harness) -> None:
    h = current_external
    with open_unit_of_work(h.service) as uow:
        with pytest.raises(ExternalDestinationError, match="area_unknown") as err:
            h.execute(uow, area="nowhere")
    assert err.value.failure_class is ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT


@pytest.mark.parametrize(
    "change",
    [
        {"project_ref": "current.project:00000000-0000-7000-8000-000000000009"},
        {"destination_ref": "other"},
        {"idempotency_key": ""},
    ],
)
def test_strict_input_refuses_before_any_core_or_current_write(
    current_external: Harness, change: dict[str, object]
) -> None:
    h = current_external
    if "project_ref" in change:
        # A real project, so nothing but the contract itself can refuse the field.
        project = dispatch(
            h.owner,
            "current.project.create",
            {"title": "Synthetic project"},
            registry=h.surfaces.operations,
            consumers=h.consumers,
        )
        assert project.ok, project
        change = {"project_ref": project.result.ref}
    request = {**payload(), **change}
    outcome = dispatch(
        h.owner,
        OPERATION,
        request,
        registry=h.surfaces.operations,
        consumers=h.consumers,
    )
    assert outcome.state == "input_invalid", outcome
    assert h.rows(external_action) == [] and h.rows(idempotency_result) == []
    assert h.rows(t.item) == [] and h.calls == []


def test_a_service_context_cannot_stage_it(current_external: Harness) -> None:
    """Core could never rebuild a service caller as the held person, so it is
    refused at staging rather than left to fail inside the continuation."""
    h = current_external
    outcome = dispatch(
        h.service,
        OPERATION,
        payload(),
        registry=h.surfaces.operations,
        consumers=h.consumers,
    )
    assert not outcome.ok and outcome.state != "approval_required", outcome
    assert h.rows(external_action) == []


def test_source_item_link_checks_hold_in_the_database(
    current_external: Harness,
) -> None:
    h = current_external
    base = {
        "source_ref": "synthetic:source",
        "item_ref": "current.item:00000000-0000-7000-8000-00000000000a",
        "revision": 1,
        "item_state": "available",
        "project_ref": None,
        "last_project_ref": None,
        "project_state": "never_assigned",
        "changed_at": datetime.now(UTC),
    }
    project = "current.project:00000000-0000-7000-8000-00000000000b"
    bad: list[dict[str, Any]] = [
        {"project_state": "linked"},
        {"project_ref": project},
        {"project_state": "cleared"},
        {"project_state": "deleted", "project_ref": project},
        {"last_project_ref": project},
        {"item_state": "gone"},
        {"project_state": "lost", "last_project_ref": project},
        {"revision": 0},
    ]
    for change in bad:
        with open_unit_of_work(h.owner) as uow:
            with pytest.raises(IntegrityError):
                uow.connection.execute(
                    insert(t.source_item_link).values(**{**base, **change})
                )
    good: list[Callable[[], dict[str, Any]]] = [
        lambda: {"project_state": "linked", "project_ref": project},
        lambda: {"project_state": "cleared", "last_project_ref": project},
        lambda: {
            "project_state": "linked",
            "project_ref": project,
            "last_project_ref": project,
        },
        lambda: {
            "project_state": "deleted",
            "last_project_ref": project,
            "item_state": "deleted",
        },
    ]
    for index, change in enumerate(good):
        with open_unit_of_work(h.owner) as uow:
            uow.connection.execute(
                insert(t.source_item_link).values(
                    **{
                        **base,
                        "source_ref": f"synthetic:{index}",
                        "item_ref": f"{ITEM_PREFIX}00000000001{index}",
                        **change(),
                    }
                )
            )
            uow.commit()
    with open_unit_of_work(h.owner) as uow:
        with pytest.raises(IntegrityError):  # one source per item, ever
            uow.connection.execute(
                insert(t.source_item_link).values(
                    **{
                        **base,
                        "item_ref": f"{ITEM_PREFIX}000000000010",
                    }
                )
            )


def test_the_loaded_current_manifest_passes_the_production_allowlist(
    current_external: Harness,
) -> None:
    """The real loader's registrations with Current present are the exact tuple."""
    h = current_external
    assert_production_external_composition(h.surfaces.operations, DESTINATIONS)
    found = DESTINATIONS.lookup("current")
    assert found is not None and found.origin == "current"
    assert DESTINATIONS.names() == frozenset({"current"})
