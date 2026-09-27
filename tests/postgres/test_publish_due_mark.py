"""Issue #180: an event published inside a dispatch marks its workspace due.

``publish`` writes ``core.event_delivery`` rows inside the handler's transaction,
where there is no control-plane connection to mark the due-work index with. Before
#180 nothing marked it, so a subscribed consumer's delivery waited for the reconcile
floor (``work.due_reconcile_seconds``, 900 s by default) instead of running on the
next worker pass. ``publish`` now asks for ``dispatch()``'s post-commit due mark
(``HandlerUnitOfWork.request_due_mark``, the #146 hook) whenever it wrote at least
one delivery row.

Each test pushes the index an hour out, as a visit that found nothing leaves it, and
then reads it back after the dispatch:

- a publish with a subscribed, enabled consumer pulls it back to now;
- a publish nobody consumes leaves it alone;
- a publish whose dispatch rolls back leaves it alone;
- a publish inside an approved execution, which runs on a view the approval gate
  builds inside ``core.approval.approve``'s own dispatch, pulls it back too.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.consumers import EVENT_TYPE, ensure_consumer_tables
from harness.consumers import registry as consumer_registry
from harness.registry import (
    NoteWriteInput,
    Nothing,
    enable_harness_module,
    probe_declaration,
    register_harness,
)
from pydantic import BaseModel
from rheo_contracts import Role, SafetyClass, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry, NewEvent, publish
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.work_index_tables import workspace_work_due
from rheo_core.storage.work_tables import event_delivery
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

pytestmark = pytest.mark.postgres

PUBLISH = "harness.probe.publish"
PUBLISH_THEN_FAIL = "harness.probe.publish_then_fail"
PUBLISH_GATED = "harness.probe.publish_gated"
SUBJECT = "harness.note:due-mark"


class _Boom(RuntimeError):
    """Raised after the publish, so the dispatch rolls the delivery row back."""


def _publish_note(ctx: WorkspaceContext, uow: UnitOfWork, body: str) -> None:
    consumers = uow.consumers if isinstance(uow, HandlerUnitOfWork) else None
    assert consumers is not None, "the dispatch under test wired no registry"
    publish(
        ctx,
        uow,
        NewEvent(
            type=EVENT_TYPE,
            schema_version=1,
            subject_ref=SUBJECT,
            subject_revision=1,
            data={"body": body},
        ),
        now=datetime.now(UTC),
        consumers=consumers,
    )


def _publishing_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: BaseModel
) -> Nothing:
    assert isinstance(model_input, NoteWriteInput)
    _publish_note(ctx, uow, model_input.body)
    return Nothing()


def _publish_then_fail(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: BaseModel
) -> Nothing:
    assert isinstance(model_input, NoteWriteInput)
    _publish_note(ctx, uow, model_input.body)
    raise _Boom("after the publish")


_GATED_DECLARATION = probe_declaration(PUBLISH_GATED, NoteWriteInput).model_copy(
    update={"safety_class": SafetyClass.DESTRUCTIVE}
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    """Core and harness on the process-wide registry, which the approval gate looks
    the held operation up in, plus the gated probe. All three are idempotent."""
    register_core_operations()
    register_harness()
    REGISTRY.register(
        _GATED_DECLARATION, _publishing_handler, origin=TEST_HARNESS_ORIGIN
    )


@pytest.fixture
def private_registry() -> OperationRegistry:
    """The two ungated probes live on a registry of this test's own."""
    registry = OperationRegistry()
    register_core_operations(registry)
    register_harness(registry=registry)
    registry.register(
        probe_declaration(PUBLISH, NoteWriteInput),
        _publishing_handler,
        origin=TEST_HARNESS_ORIGIN,
    )
    registry.register(
        probe_declaration(PUBLISH_THEN_FAIL, NoteWriteInput),
        _publish_then_fail,
        origin=TEST_HARNESS_ORIGIN,
    )
    return registry


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """The workspace database with ``harness`` enabled, so the harness consumer's
    subscription fans out, and the consumer's tables created."""
    built = cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )
    with built.begin() as conn:
        enable_harness_module(conn)
        ensure_consumer_tables(conn)
    return built


@pytest.fixture
def owner(workspace: UUID, owner_account_id: UUID, engine: Engine) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    assert "harness" in ctx.enabled_modules
    return ctx


def _due_at(cluster: ClusterSession, workspace: UUID) -> datetime | None:
    with cluster.backend.control_engine.connect() as conn:
        value = conn.execute(
            select(workspace_work_due.c.due_at).where(
                workspace_work_due.c.workspace_id == workspace
            )
        ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


def _push_due_out(cluster: ClusterSession, workspace: UUID) -> datetime:
    """Set the index an hour out, as a visit that found nothing leaves it."""
    later = datetime.now(UTC) + timedelta(hours=1)
    with cluster.backend.control_engine.begin() as conn:
        statement = pg_insert(workspace_work_due).values(
            workspace_id=workspace, due_at=later, updated_at=datetime.now(UTC)
        )
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[workspace_work_due.c.workspace_id],
                set_={"due_at": later},
            )
        )
    pushed = _due_at(cluster, workspace)
    assert pushed is not None
    return pushed


def _deliveries(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(select(func.count()).select_from(event_delivery)).scalar_one()
        )


def test_a_published_delivery_marks_the_workspace_due_for_the_next_pass(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    engine: Engine,
    private_registry: OperationRegistry,
) -> None:
    pushed = _push_due_out(cluster, workspace)
    before = datetime.now(UTC)

    outcome = dispatch(
        owner,
        PUBLISH,
        {"body": "a note for the recorder"},
        registry=private_registry,
        consumers=consumer_registry(),
    )

    assert outcome.ok, outcome
    assert _deliveries(engine) == 1
    due = _due_at(cluster, workspace)
    assert due is not None
    assert due < pushed, (due, pushed)
    assert due <= datetime.now(UTC)
    assert due >= before - timedelta(seconds=5)


def test_a_publish_nobody_consumes_leaves_the_index_alone(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    engine: Engine,
    private_registry: OperationRegistry,
) -> None:
    pushed = _push_due_out(cluster, workspace)

    outcome = dispatch(
        owner,
        PUBLISH,
        {"body": "a note nobody listens for"},
        registry=private_registry,
        consumers=ConsumerRegistry(),
    )

    assert outcome.ok, outcome
    assert _deliveries(engine) == 0
    assert _due_at(cluster, workspace) == pushed


def test_a_rolled_back_publish_marks_nothing(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    engine: Engine,
    private_registry: OperationRegistry,
) -> None:
    pushed = _push_due_out(cluster, workspace)

    outcome = dispatch(
        owner,
        PUBLISH_THEN_FAIL,
        {"body": "a note that never lands"},
        registry=private_registry,
        consumers=consumer_registry(),
    )

    assert outcome.state == "failed", outcome
    assert _deliveries(engine) == 0
    assert _due_at(cluster, workspace) == pushed


def test_a_publish_inside_an_approved_execution_marks_the_workspace_due(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    engine: Engine,
) -> None:
    """The gate runs the held handler on a view of its own, built inside the approve
    dispatch. A request made on that inner view must reach the outer one, which is
    the view ``dispatch()`` reads after its commit."""
    held = dispatch(
        owner,
        PUBLISH_GATED,
        {"body": "a note that waits for approval"},
        consumers=consumer_registry(),
    )
    assert held.state == "approval_required", held
    assert held.approval_id is not None
    assert _deliveries(engine) == 0
    pushed = _push_due_out(cluster, workspace)

    approved = dispatch(
        owner,
        APPROVAL_APPROVE,
        {"approval_id": str(held.approval_id)},
        consumers=consumer_registry(),
    )

    assert approved.ok, approved
    assert _deliveries(engine) == 1
    due = _due_at(cluster, workspace)
    assert due is not None
    assert due < pushed, (due, pushed)
