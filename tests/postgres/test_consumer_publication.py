"""A real worker consumer publishes inside its delivery transaction (#304)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.registry import enable_harness_module
from rheo_contracts import ActorKind, Entry, EventEnvelope, Role, WorkspaceContext
from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.events import (
    ConsumerRegistry,
    ConsumerSubscription,
    NewEvent,
    failed_delivery_count_for_subjects,
    publish,
)
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import event_delivery, outbox_event
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import select, update

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("fail", [False, True])
def test_followup_event_and_fanout_share_worker_transaction(
    cluster: ClusterSession,
    workspace: UUID,
    fail: bool,
) -> None:
    """Both publication and fan-out disappear if their consumer transaction fails."""
    registry = ConsumerRegistry()
    observed: list[WorkspaceContext] = []
    delivered: list[UUID] = []
    now = datetime.now(UTC)
    engine = cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )
    with engine.begin() as connection:
        enable_harness_module(connection)

    def first(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
        context = uow.consumer_context
        assert context is not None
        assert uow.consumers is registry
        assert context.workspace_id == workspace
        assert context.actor.kind is ActorKind.SYSTEM
        assert context.actor.id is None
        assert context.role is Role.SERVICE
        assert context.entry is Entry.JOB
        assert context.principal.account_id is None
        assert context.principal.bound_purpose is None
        assert context.operation_set == frozenset()
        assert context.enabled_modules == frozenset({"harness"})
        assert context.audience is None
        assert context.request_id == envelope.correlation_id
        observed.append(context)
        publish(
            context,
            uow,
            NewEvent(
                type="core.followup",
                schema_version=1,
                subject_ref=envelope.subject_ref,
                subject_revision=1,
                data={},
                causation_id=envelope.id,
            ),
            now=now,
            consumers=uow.consumers,
        )
        if fail:
            raise RuntimeError("synthetic consumer failure")

    def second(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
        assert uow.consumer_context is not None
        delivered.append(envelope.id)

    registry.register(
        ConsumerSubscription("core.first", "core.original", "core", False, first)
    )
    registry.register(
        ConsumerSubscription(
            "harness.second", "core.followup", "harness", False, second
        )
    )
    registry.register(
        ConsumerSubscription("absent.third", "core.followup", "absent", False, second)
    )
    context = context_for_operator(workspace)
    assert isinstance(context, WorkspaceContext)
    with open_unit_of_work(context) as uow:
        publish(
            context,
            uow,
            NewEvent(
                type="core.original",
                schema_version=1,
                subject_ref="core.fixture:example",
                subject_revision=1,
                data={"workspace_id": "forged", "role": "owner"},
            ),
            now=now,
            consumers=registry,
        )
        uow.commit()
    visit_workspace(
        DueWorkspace(workspace_id=workspace, observed_due_at=None),
        kinds=JobKindRegistry(),
        consumers=registry,
        backend=cluster.backend,
        owner="publication-test",
        clock=lambda: now + timedelta(seconds=1),
        jitter=None,
    )
    assert len(observed) == 1
    with open_unit_of_work(context) as uow:
        events = uow.connection.execute(select(outbox_event)).mappings().all()
        deliveries = uow.connection.execute(select(event_delivery)).mappings().all()
    assert len(events) == (1 if fail else 2)
    assert len(deliveries) == (1 if fail else 2)
    assert len(delivered) == (0 if fail else 1)
    if not fail:
        followup = next(row for row in events if row["type"] == "core.followup")
        assert followup["actor_kind"] == "system"
        assert followup["actor_id"] is None
        assert followup["correlation_id"] == context.request_id
    else:
        with open_unit_of_work(context) as uow:
            # Terminal failure is an operator-visible state, distinct from retry.
            uow.connection.execute(update(event_delivery).values(state="failed"))
            assert (
                failed_delivery_count_for_subjects(
                    uow.connection,
                    consumer_id="core.first",
                    subject_refs=["core.fixture:example", "core.fixture:example"],
                )
                == 1
            )
            assert (
                failed_delivery_count_for_subjects(
                    uow.connection,
                    consumer_id="core.other",
                    subject_refs=["core.fixture:example"],
                )
                == 0
            )
            assert (
                failed_delivery_count_for_subjects(
                    uow.connection,
                    consumer_id="core.first",
                    subject_refs=[],
                )
                == 0
            )
            assert (
                failed_delivery_count_for_subjects(
                    uow.connection,
                    consumer_id="core.first",
                    subject_refs=["unrelated"],
                )
                == 0
            )


def test_consumer_context_refuses_a_transaction_for_another_workspace(
    workspace: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    """A valid workspace id cannot bless a different workspace transaction."""
    other = make_workspace()
    context = context_for_operator(workspace)
    assert isinstance(context, WorkspaceContext)
    with open_unit_of_work(context) as uow:
        refused = context_for_event_consumer(other, uow, request_id=context.request_id)
    assert isinstance(refused, Refusal)
    assert refused.state == "database_mismatch"
