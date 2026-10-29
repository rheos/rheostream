"""Approved EXTERNAL continuation, typed settlement and explicit resolution.

Every case drives the public seams: a KEYED EXTERNAL call staged through
``dispatch``, approved or refused through ``core.approval.approve``/``.refuse``,
continued by one worker visit, read back through ``core.external_action.get`` and
``core.operation.get``, and resolved through ``core.operation.resolve``. The
destination is synthetic and records every invocation in ``calls``.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import (
    AuditSpec,
    ExternalSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.approvals.binding import INVALID_APPROVAL
from rheo_core.approvals.tables import approval, approval_payload
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.context import MEMBERSHIP_MISSING
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.modules import DESTINATIONS, DestinationMode, DestinationRegistration
from rheo_core.operations import OperationOutcome, dispatch, register_core_operations
from rheo_core.operations.continuation import (
    EXTERNAL_ACTION_EXECUTE,
    ExternalContinuationGrant,
    ExternalDestinationError,
    ExternalFailureClass,
    GrantRefused,
    continue_approved_external_in_transaction,
    mint_continuation_grant,
    run_external_continuation,
)
from rheo_core.operations.external_actions import external_action
from rheo_core.operations.idempotency_tables import idempotency_result
from rheo_core.operations.refusals import ROLE_NOT_PERMITTED
from rheo_core.operations.registry import REGISTRY
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage import control_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import audit_record, job, operation
from rheo_core.work.jobs import enqueue_job, request_cancellation
from rheo_core.work.kinds import (
    ContinuationHandler,
    ContinuationLease,
    ContinuationMode,
    ContinuationResult,
    JobKindRegistry,
)
from rheo_core.work.loop import visit_workspace
from sqlalchemy import delete, func, select, update

pytestmark = pytest.mark.postgres

OPERATION = "core.external.continue"
OWNER = "continuation-test"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str
    destination_ref: Literal["synthetic"] = "synthetic"
    title: str = Field(min_length=1)


class _Output(BaseModel):
    item_ref: str


def _declaration() -> OperationDeclaration:
    return OperationDeclaration(
        name=OPERATION,
        safety_class=SafetyClass.EXTERNAL,
        roles=frozenset({Role.OWNER}),
        input_model=_Input,
        output=_Output,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key", request_schema_version=1, result_schema_version=1
        ),
        external=ExternalSpec(
            destination_field="destination_ref",
            destination_ref="synthetic",
            result_ref_field="item_ref",
        ),
        audit=AuditSpec(subject_field=None),
    )


@dataclass
class _Destination:
    """The synthetic destination: a script of outcomes, and what it was asked.

    A provider destination remembers each effect it performs, as a real provider
    does: a later reconcile reports it even when the worker's own transaction rolled
    back. ``reconciled`` may also be an exception, which reconcile then raises.
    """

    calls: list[str] = field(default_factory=list)
    script: list[object] = field(default_factory=list)
    ready: bool = True
    provider: bool = False
    reconciled: _Output | BaseException | None = None
    reconcile_calls: list[str | None] = field(default_factory=list)

    def execute(self, ctx: WorkspaceContext, uow: Any, model: BaseModel) -> BaseModel:
        assert isinstance(model, _Input)
        self.calls.append(model.request_key)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        output = _Output(item_ref=f"synthetic:{model.request_key}")
        if self.provider:
            self.reconciled = output
        return output

    def reconcile(
        self, ctx: WorkspaceContext, uow: Any, request_id: str | None
    ) -> BaseModel | None:
        self.reconcile_calls.append(request_id)
        if isinstance(self.reconciled, BaseException):
            raise self.reconciled
        return self.reconciled


@pytest.fixture
def ctx(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    built = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(built, WorkspaceContext)
    register_core_operations()
    install_sink("core", CORE_AUDIT_SINK)
    return built


def _register(
    monkeypatch: pytest.MonkeyPatch, *, provider: bool = False
) -> _Destination:
    destination = _Destination(provider=provider)

    def forbidden_handler(ctx: WorkspaceContext, uow: Any, model: BaseModel) -> Any:
        raise AssertionError("the operation handler must never run for EXTERNAL")

    monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
    REGISTRY._operations.pop(OPERATION, None)
    REGISTRY.register(_declaration(), forbidden_handler, origin=CORE_ORIGIN)
    monkeypatch.setattr(DESTINATIONS, "_destinations", dict(DESTINATIONS._destinations))
    DESTINATIONS._destinations.pop("synthetic", None)
    DESTINATIONS.register(
        DestinationRegistration(
            destination_ref="synthetic",
            operation_name=OPERATION,
            mode=DestinationMode.PROVIDER if provider else DestinationMode.IN_PROCESS,
            is_installed=lambda: True,
            is_enabled=lambda ctx: True,
            is_ready=lambda ctx, uow: destination.ready,
            credentials_available=(lambda ctx, uow: True) if provider else None,
            execute=destination.execute,
            reconcile=destination.reconcile if provider else None,
        ),
        origin="core",
    )
    return destination


def _payload(key: str) -> dict[str, object]:
    return {"request_key": key, "destination_ref": "synthetic", "title": "Synthetic"}


def _stage(ctx: WorkspaceContext, key: str) -> OperationOutcome:
    outcome = dispatch(ctx, OPERATION, _payload(key))
    assert outcome.state == "approval_required", outcome
    return outcome


def _approve(ctx: WorkspaceContext, approval_id: UUID) -> OperationOutcome:
    outcome = dispatch(ctx, "core.approval.approve", {"approval_id": str(approval_id)})
    assert outcome.ok, outcome
    return outcome


def _visit(
    cluster: Any,
    workspace: UUID,
    *,
    after: float = 1.0,
    continuation: ContinuationHandler = run_external_continuation,
) -> None:
    kinds = JobKindRegistry()
    kinds.register_continuation(EXTERNAL_ACTION_EXECUTE, continuation)
    at = datetime.now(UTC) + timedelta(seconds=after)
    visit_workspace(
        DueWorkspace(workspace_id=workspace, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: at,
        jitter=None,
    )


@dataclass(frozen=True)
class _State:
    action: dict[str, Any]
    approval: str
    operation: tuple[str, str | None, str | None]
    envelope: tuple[str, str | None]
    jobs: list[tuple[str, str, Any, Any, bool, int]]


def _state(ctx: WorkspaceContext, held: OperationOutcome, key: str) -> _State:
    with open_unit_of_work(ctx) as uow:
        conn = uow.connection
        action = dict(
            conn.execute(
                select(external_action).where(
                    external_action.c.operation_id == held.operation_id
                )
            )
            .mappings()
            .one()
        )
        approval_state = conn.execute(
            select(approval.c.state).where(approval.c.id == held.approval_id)
        ).scalar_one()
        op = conn.execute(
            select(
                operation.c.state, operation.c.result_ref, operation.c.error_code
            ).where(operation.c.id == held.operation_id)
        ).one()
        envelope = conn.execute(
            select(idempotency_result.c.state, idempotency_result.c.error_code).where(
                idempotency_result.c.operation_name == OPERATION,
                idempotency_result.c.idempotency_key == key,
            )
        ).one()
        jobs = conn.execute(
            select(
                job.c.kind,
                job.c.state,
                job.c.operation_id,
                job.c.input,
                job.c.cancel_requested,
                job.c.attempts,
            ).order_by(job.c.created_at)
        ).all()
    return _State(
        action=action,
        approval=str(approval_state),
        operation=(str(op.state), op.result_ref, op.error_code),
        envelope=(str(envelope.state), envelope.error_code),
        jobs=[tuple(row) for row in jobs],  # type: ignore[misc]
    )


def _action_read(ctx: WorkspaceContext, action_id: UUID) -> dict[str, Any]:
    outcome = dispatch(
        ctx, "core.external_action.get", {"external_action_id": str(action_id)}
    )
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.model_dump()


def _audit_rows(
    ctx: WorkspaceContext, operation_id: UUID | None
) -> list[tuple[str, UUID | None]]:
    """``(outcome, actor_id)`` of every audit row for the held operation, in order."""
    with open_unit_of_work(ctx) as uow:
        rows = uow.connection.execute(
            select(audit_record.c.outcome, audit_record.c.actor_id)
            .where(audit_record.c.operation_id == operation_id)
            .order_by(audit_record.c.occurred_at, audit_record.c.id)
        ).all()
    return [(str(row.outcome), row.actor_id) for row in rows]


def _approved_and_queued(
    ctx: WorkspaceContext, key: str
) -> tuple[OperationOutcome, _State]:
    held = _stage(ctx, key)
    assert held.approval_id is not None
    _approve(ctx, held.approval_id)
    return held, _state(ctx, held, key)


# --- approval queues exactly one job; the continuation performs the effect ----------


def test_approval_queues_one_continuation_and_never_invokes_inline(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "approve-one")

    # Carried item (a): approval runs nothing inline.
    assert destination.calls == []
    action_id = queued.action["id"]
    assert queued.jobs == [
        (
            EXTERNAL_ACTION_EXECUTE,
            "queued",
            None,
            {"external_action_id": str(action_id)},
            False,
            0,
        )
    ]
    assert queued.action["state"] == "pending"
    assert queued.action["execution_job_id"] is not None
    assert queued.approval == "approved"
    assert queued.operation[0] == "running"
    assert queued.envelope == ("pending", None)

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "approve-one")
    assert destination.calls == ["approve-one"]
    assert done.action["state"] == "succeeded"
    assert done.action["result_ref"] == "synthetic:approve-one"
    assert done.action["failure_class"] is None
    assert done.action["attempts"] == 1
    assert done.approval == "executed"
    assert done.operation == ("succeeded", "synthetic:approve-one", None)
    assert done.envelope == ("succeeded", None)
    assert [row[1] for row in done.jobs] == ["succeeded"]
    read = _action_read(ctx, action_id)
    assert read["state"] == "succeeded"
    assert read["restage_eligible"] is False

    # A repeat replays the retained success: no second approval, effect or job.
    again = dispatch(ctx, OPERATION, _payload("approve-one"))
    assert again.ok and again.result == _Output(item_ref="synthetic:approve-one")
    assert again.approval_id is None
    assert destination.calls == ["approve-one"]
    with open_unit_of_work(ctx) as uow:
        approvals = uow.connection.execute(
            select(func.count())
            .select_from(approval)
            .where(approval.c.operation_name == OPERATION)
        ).scalar_one()
    assert approvals == 1
    assert len(_state(ctx, held, "approve-one").jobs) == 1


def test_a_second_visit_after_success_invokes_nothing(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    _approved_and_queued(ctx, "once-only")
    _visit(cluster, ctx.workspace_id)
    _visit(cluster, ctx.workspace_id, after=600)
    assert destination.calls == ["once-only"]


# --- typed retry, exhaustion and crash recovery -------------------------------------


def _retryable() -> ExternalDestinationError:
    return ExternalDestinationError(
        ExternalFailureClass.RETRYABLE_NO_EFFECT, "synthetic_busy", "try again"
    )


def test_typed_retry_requeues_without_settling_then_succeeds(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    destination.script = [_retryable()]
    held, _ = _approved_and_queued(ctx, "retry-once")

    _visit(cluster, ctx.workspace_id)
    retried = _state(ctx, held, "retry-once")
    assert destination.calls == ["retry-once"]
    assert retried.action["state"] == "executing"
    assert retried.action["error_code"] == "synthetic_busy"
    assert retried.action["terminal_at"] is None
    assert retried.approval == "approved"
    assert retried.operation[0] == "running"
    assert retried.envelope == ("pending", None)
    assert [row[1] for row in retried.jobs] == ["queued"]

    _visit(cluster, ctx.workspace_id, after=120)
    done = _state(ctx, held, "retry-once")
    assert destination.calls == ["retry-once", "retry-once"]
    assert done.action["state"] == "succeeded"
    assert done.action["attempts"] == 2
    assert done.action["error_code"] is None


def _set_budget(ctx: WorkspaceContext, max_attempts: int) -> None:
    with open_unit_of_work(ctx) as uow:
        uow.connection.execute(
            update(job)
            .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            .values(max_attempts=max_attempts)
        )
        uow.commit()


def test_budget_exhaustion_with_absence_proven_settles_failed(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    destination.script = [_retryable()]
    held, queued = _approved_and_queued(ctx, "exhaust")
    _set_budget(ctx, 1)

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "exhaust")
    assert destination.calls == ["exhaust"]
    assert done.action["state"] == "failed"
    assert done.action["failure_class"] == "retryable_no_effect"
    assert done.action["error_code"] == "retry_budget_exhausted"
    assert done.approval == "executed"
    assert done.operation[0] == "failed"
    assert done.envelope == ("failed", "retry_budget_exhausted")
    assert [row[1] for row in done.jobs] == ["failed"]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is True
    replay = dispatch(ctx, OPERATION, _payload("exhaust"))
    assert replay.state == "failed" and replay.approval_id is None


def test_crash_beyond_budget_reconciles_once_without_invoking(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, _ = _approved_and_queued(ctx, "crashed")
    # A worker leased the last attempt and died: an expired lease, budget spent.
    with open_unit_of_work(ctx) as uow:
        uow.connection.execute(
            update(job)
            .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            .values(
                state="leased",
                lease_owner="dead-worker",
                lease_until=datetime.now(UTC) - timedelta(minutes=5),
                attempts=job.c.max_attempts,
            )
        )
        uow.commit()

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "crashed")
    assert destination.calls == []
    assert done.action["state"] == "failed"
    assert done.action["failure_class"] == "retryable_no_effect"
    assert done.approval == "executed"
    assert done.operation[0] == "failed"
    assert [row[1] for row in done.jobs] == ["failed"]


# --- cancellation, refusals and the pre-job refusal path ---------------------------


def test_cancelled_queued_job_settles_cancelled_without_effect(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "cancel-me")
    with open_unit_of_work(ctx) as uow:
        job_id = uow.connection.execute(select(job.c.id)).scalar_one()
        assert request_cancellation(uow.connection, job_id, now=datetime.now(UTC)) == (
            "queued"
        )
        uow.commit()
    # The continuation, not the cancel request, owns the related rows.
    assert _state(ctx, held, "cancel-me").action["state"] == "pending"

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "cancel-me")
    assert destination.calls == []
    assert done.action["state"] == "cancelled"
    assert done.action["failure_class"] == "cancelled_no_effect"
    assert done.approval == "refused"
    assert done.operation[0] == "cancelled"
    assert done.envelope == ("cancelled", "continuation_cancelled")
    assert [row[1] for row in done.jobs] == ["cancelled"]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False


def test_effect_time_destination_refusal_settles_refused(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "went-away")
    destination.ready = False

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "went-away")
    assert destination.calls == []
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "destination_unavailable_no_effect"
    assert done.approval == "refused"
    assert done.operation == ("failed", None, "guard_refused")
    assert done.envelope == ("refused", "guard_refused")
    assert [row[1] for row in done.jobs] == ["succeeded"]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is True
    # The hold's row, then the refusal's, both naming the held caller.
    assert _audit_rows(ctx, held.operation_id) == [
        ("refused", ctx.actor.id),
        ("refused", ctx.actor.id),
    ]


def test_typed_permanent_refusal_settles_refused(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    destination.script = [
        ExternalDestinationError(
            ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT, "synthetic_rule", "no"
        )
    ]
    held, queued = _approved_and_queued(ctx, "refuse-it")
    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "refuse-it")
    assert destination.calls == ["refuse-it"]
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "permanent_refusal_no_effect"
    assert done.envelope == ("refused", "synthetic_rule")
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False


def test_typed_cancellation_from_destination_settles_cancelled(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    destination.script = [
        ExternalDestinationError(
            ExternalFailureClass.CANCELLED_NO_EFFECT, "synthetic_cancel", "stop"
        )
    ]
    held, _ = _approved_and_queued(ctx, "dest-cancel")
    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "dest-cancel")
    assert done.action["state"] == "cancelled"
    assert [row[1] for row in done.jobs] == ["cancelled"]


def test_guard_refusal_at_approval_queues_no_job(
    ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held = _stage(ctx, "refused-at-approve")
    destination.ready = False
    assert held.approval_id is not None
    approved = _approve(ctx, held.approval_id)
    assert approved.result is not None
    assert approved.result.model_dump()["state"] == "refused"
    done = _state(ctx, held, "refused-at-approve")
    assert done.jobs == []
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "destination_unavailable_no_effect"
    assert done.operation == ("failed", None, "guard_refused")
    assert done.envelope == ("refused", "guard_refused")
    assert destination.calls == []
    # The same held-operation audit rows an effect-time refusal writes.
    assert _audit_rows(ctx, held.operation_id) == [
        ("refused", ctx.actor.id),
        ("refused", ctx.actor.id),
    ]


def test_human_refusal_before_any_job_refuses_action_and_reservation(
    ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held = _stage(ctx, "no-thanks")
    refused = dispatch(
        ctx, "core.approval.refuse", {"approval_id": str(held.approval_id)}
    )
    assert refused.ok, refused
    done = _state(ctx, held, "no-thanks")
    assert done.jobs == []
    assert done.approval == "refused"
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "cancelled_no_effect"
    assert done.operation[0] == "cancelled"
    assert done.envelope == ("refused", "approval_refused")
    assert destination.calls == []


# --- the lease finish is part of the transaction -------------------------------------


def test_lost_lease_finish_rolls_back_effect_and_every_settlement(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rheo_core.work.loop as loop

    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "lost-lease")
    monkeypatch.setattr(loop, "finish_succeeded", lambda *a, **k: False)

    _visit(cluster, ctx.workspace_id)
    after = _state(ctx, held, "lost-lease")
    assert destination.calls == ["lost-lease"]
    assert after.action["state"] == "pending"
    assert after.action["attempts"] == 0
    assert after.approval == "approved"
    assert after.operation[0] == "running"
    assert after.envelope == ("pending", None)
    assert [row[1] for row in after.jobs] == ["leased"]


# --- unknown outcomes stay unresolved until a person resolves them ------------------


def test_provider_unknown_outcome_is_unresolved_and_never_restaged(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch, provider=True)
    destination.script = [RuntimeError("timed out after sending")]
    held, queued = _approved_and_queued(ctx, "unknown")

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "unknown")
    assert destination.calls == ["unknown"]
    # Reconcile ran first, with the deterministic provider request id.
    assert destination.reconcile_calls == [f"rheo-external-{queued.action['id']}"]
    assert done.action["state"] == "unresolved"
    assert done.action["failure_class"] == "outcome_unknown"
    assert done.approval == "executed"
    assert done.operation[0] == "unresolved"
    assert done.envelope == ("unresolved", "destination_failed")
    assert [row[1] for row in done.jobs] == ["succeeded"]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False

    replay = dispatch(ctx, OPERATION, _payload("unknown"))
    assert replay.state == "unresolved" and replay.approval_id is None
    _visit(cluster, ctx.workspace_id, after=600)
    assert destination.calls == ["unknown"]


def _resolve(ctx: WorkspaceContext, operation_id: UUID, **fields: object) -> Any:
    return dispatch(
        ctx,
        "core.operation.resolve",
        {"operation_id": str(operation_id), **fields},
    )


def test_resolve_success_requires_a_verified_result_ref(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch, provider=True)
    destination.script = [RuntimeError("lost reply")]
    held, queued = _approved_and_queued(ctx, "resolve-ok")
    _visit(cluster, ctx.workspace_id)
    assert held.operation_id is not None

    blank = _resolve(ctx, held.operation_id, outcome="cancelled", note="   ")
    assert blank.state == "input_invalid"
    unverified = _resolve(
        ctx,
        held.operation_id,
        outcome="succeeded",
        note="provider says it landed",
        terminal_check_kind="provider_status",
        result_ref="synthetic:resolve-ok",
    )
    assert unverified.state == "external_resolution_unverified"
    assert _state(ctx, held, "resolve-ok").action["state"] == "unresolved"

    destination.reconciled = _Output(item_ref="synthetic:resolve-ok")
    wrong = _resolve(
        ctx,
        held.operation_id,
        outcome="succeeded",
        note="checked",
        terminal_check_kind="provider_status",
        result_ref="synthetic:other",
    )
    assert wrong.state == "external_resolution_unverified"
    resolved = _resolve(
        ctx,
        held.operation_id,
        outcome="succeeded",
        note="checked the provider",
        terminal_check_kind="provider_status",
        result_ref="synthetic:resolve-ok",
    )
    assert resolved.ok, resolved
    done = _state(ctx, held, "resolve-ok")
    assert done.action["state"] == "succeeded"
    assert done.action["result_ref"] == "synthetic:resolve-ok"
    assert done.operation == ("succeeded", "synthetic:resolve-ok", None)
    assert done.envelope == ("succeeded", None)
    replay = dispatch(ctx, OPERATION, _payload("resolve-ok"))
    assert replay.ok and replay.result == _Output(item_ref="synthetic:resolve-ok")


def test_resolve_failed_requires_proof_of_no_effect(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch, provider=True)
    destination.script = [RuntimeError("lost reply")]
    held, queued = _approved_and_queued(ctx, "resolve-failed")
    _visit(cluster, ctx.workspace_id)
    assert held.operation_id is not None

    destination.reconciled = _Output(item_ref="synthetic:resolve-failed")
    landed = _resolve(ctx, held.operation_id, outcome="failed", note="I think not")
    assert landed.state == "external_resolution_unverified"
    destination.reconciled = None
    resolved = _resolve(ctx, held.operation_id, outcome="failed", note="provider: none")
    assert resolved.ok, resolved
    done = _state(ctx, held, "resolve-failed")
    assert done.action["state"] == "failed"
    assert done.operation[0] == "failed"
    assert done.envelope == ("failed", "resolved_no_effect")
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is True
    again = _resolve(ctx, held.operation_id, outcome="cancelled", note="twice")
    assert again.state == "operation_state"


def test_resolve_cancelled_is_terminal_without_proof(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch, provider=True)
    destination.script = [RuntimeError("lost reply")]
    held, queued = _approved_and_queued(ctx, "resolve-cancel")
    _visit(cluster, ctx.workspace_id)
    assert held.operation_id is not None
    resolved = _resolve(ctx, held.operation_id, outcome="cancelled", note="abandon")
    assert resolved.ok, resolved
    done = _state(ctx, held, "resolve-cancel")
    assert done.action["state"] == "cancelled"
    assert done.envelope == ("cancelled", "resolved_cancelled")
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False


def test_provider_reconcile_proves_prior_success_without_invoking(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch, provider=True)
    destination.reconciled = _Output(item_ref="synthetic:already")
    held, _ = _approved_and_queued(ctx, "already")
    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "already")
    assert destination.calls == []
    assert done.action["state"] == "succeeded"
    assert done.operation == ("succeeded", "synthetic:already", None)


# --- forged and mismatched grants never reach the destination ----------------------


def test_forged_grant_construction_is_refused() -> None:
    with pytest.raises(TypeError, match="minted only"):
        ExternalContinuationGrant(
            workspace_id=UUID(int=1),
            job_id=UUID(int=2),
            owner=OWNER,
            action_id=UUID(int=3),
            approval_id=UUID(int=4),
            operation_id=UUID(int=5),
            operation_name=OPERATION,
            idempotency_key="forged",
            destination_ref="synthetic",
            payload_digest=b"\x00" * 32,
            request_schema_version=1,
            result_schema_version=1,
            attempts=1,
            max_attempts=8,
            mode=ContinuationMode.RUN,
            seal=object(),
        )


def test_continuation_refuses_anything_but_a_grant(ctx: WorkspaceContext) -> None:
    with open_unit_of_work(ctx) as uow:
        with pytest.raises(TypeError):
            continue_approved_external_in_transaction(
                object(),  # type: ignore[arg-type]
                uow,
                now=datetime.now(UTC),
            )


def test_a_job_that_is_not_the_actions_execution_job_gets_no_grant(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "forged-job")
    action_id = queued.action["id"]
    with open_unit_of_work(ctx) as uow:
        # Park the real job, then queue an impostor naming the same action.
        uow.connection.execute(
            update(job)
            .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            .values(next_run_at=datetime.now(UTC) + timedelta(days=1))
        )
        enqueue_job(
            uow.connection,
            kind=EXTERNAL_ACTION_EXECUTE,
            payload={"external_action_id": str(action_id)},
            now=datetime.now(UTC),
            max_attempts=8,
        )
        uow.commit()
    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "forged-job")
    assert destination.calls == []
    assert done.action["state"] == "pending"
    assert sorted(row[1] for row in done.jobs) == ["failed", "queued"]


def test_a_settled_reservation_cannot_be_continued_again(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, queued = _approved_and_queued(ctx, "settled-twice")
    _visit(cluster, ctx.workspace_id)
    action_id = queued.action["id"]
    with open_unit_of_work(ctx) as uow:
        job_id = uow.connection.execute(select(job.c.id)).scalar_one()
        # Even a job forced back into this worker's lease finds no pending binding.
        uow.connection.execute(
            update(job)
            .where(job.c.id == job_id)
            .values(state="leased", lease_owner=OWNER)
        )
        refused = mint_continuation_grant(
            uow.connection,
            workspace_id=ctx.workspace_id,
            job_id=job_id,
            owner=OWNER,
            external_action_id=action_id,
            attempts=1,
            max_attempts=8,
            mode=ContinuationMode.RUN,
        )
    assert isinstance(refused, GrantRefused)
    assert "keyed reservation is 'succeeded'" in refused.detail
    assert destination.calls == ["settled-twice"]


def test_external_action_get_refuses_an_unknown_id(ctx: WorkspaceContext) -> None:
    missing = dispatch(
        ctx, "core.external_action.get", {"external_action_id": str(UUID(int=7))}
    )
    assert missing.state == "not_found"


_BINDING_BREAKS: dict[str, tuple[str, str]] = {
    "approval_not_approved": ("approval", "approval is 'pending'"),
    "operation_not_running": ("operation", "operation is 'pending'"),
    "job_carries_an_operation": ("job", "job carries an operation id"),
    "approval_digest_differs": ("digest", "approval digest differs"),
}


@pytest.mark.parametrize("case", sorted(_BINDING_BREAKS))
def test_a_mismatched_binding_gets_no_grant_and_no_effect(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    destination = _register(monkeypatch)
    key = f"mismatch-{case}"
    held, _ = _approved_and_queued(ctx, key)
    target, problem = _BINDING_BREAKS[case]
    statement: Any
    if target == "approval":
        statement = (
            update(approval)
            .where(approval.c.id == held.approval_id)
            .values(state="pending")
        )
    elif target == "operation":
        statement = (
            update(operation)
            .where(operation.c.id == held.operation_id)
            .values(state="pending")
        )
    elif target == "job":
        statement = (
            update(job)
            .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            .values(operation_id=held.operation_id)
        )
    else:
        statement = (
            update(approval)
            .where(approval.c.id == held.approval_id)
            .values(payload_digest=b"\x01" * 32)
        )
    with open_unit_of_work(ctx) as uow:
        assert uow.connection.execute(statement).rowcount == 1
        uow.commit()

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, key)
    assert destination.calls == []
    assert done.action["state"] == "pending"
    assert done.envelope == ("pending", None)
    assert [row[1] for row in done.jobs] == ["failed"]
    with open_unit_of_work(ctx) as uow:
        error = uow.connection.execute(
            select(job.c.last_error).where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
        ).scalar_one()
    assert error is not None and problem in error


# --- the held caller and the payload are rechecked at effect time ------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [("demoted", ROLE_NOT_PERMITTED), ("revoked", MEMBERSHIP_MISSING)],
)
def test_effect_time_recheck_refuses_a_held_caller_who_lost_permission(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    workspace: UUID,
    owner_account_id: UUID,
    change: str,
    code: str,
) -> None:
    destination = _register(monkeypatch)
    key = f"held-{change}"
    held, _ = _approved_and_queued(ctx, key)
    membership = control_tables.membership
    where = (membership.c.account_id == owner_account_id) & (
        membership.c.workspace_id == workspace
    )
    with cluster.backend.control_engine.begin() as connection:
        # The operation is owner-only: a member may no longer run it.
        statement = (
            update(membership).where(where).values(role=Role.MEMBER.value)
            if change == "demoted"
            else delete(membership).where(where)
        )
        assert connection.execute(statement).rowcount == 1

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, key)
    assert destination.calls == []
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "permanent_refusal_no_effect"
    assert done.approval == "refused"
    # The continuation's own recheck refused, before any guard was asked.
    assert done.operation == ("failed", None, code)
    assert done.envelope == ("refused", code)
    assert [row[1] for row in done.jobs] == ["succeeded"]


def test_effect_time_recheck_refuses_a_changed_payload_snapshot(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _register(monkeypatch)
    held, _ = _approved_and_queued(ctx, "snapshot")
    with open_unit_of_work(ctx) as uow:
        changed = uow.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == held.approval_id)
            .values(body={**_payload("snapshot"), "title": "Changed after approval"})
        ).rowcount
        uow.commit()
    assert changed == 1

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "snapshot")
    assert destination.calls == []
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "permanent_refusal_no_effect"
    assert done.approval == "refused"
    assert done.operation == ("failed", None, INVALID_APPROVAL)
    assert done.envelope == ("refused", INVALID_APPROVAL)


# --- a provider effect survives a rolled-back run: recovery reconciles first --------


def _lose_the_invoking_worker(ctx: WorkspaceContext, *, cancel: bool) -> None:
    """A worker leased the job, invoked the provider and died before committing.

    Its transaction rolled back, so the action still reads ``pending`` with no
    ``provider_request_id`` and no attempt, while the provider kept the effect. The
    lease has expired. Without a cancellation the dead worker spent the last attempt.
    """
    with open_unit_of_work(ctx) as uow:
        uow.connection.execute(
            update(job)
            .where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
            .values(
                state="leased",
                lease_owner="dead-worker",
                lease_until=datetime.now(UTC) - timedelta(minutes=5),
                attempts=1 if cancel else job.c.max_attempts,
                cancel_requested=cancel,
            )
        )
        uow.commit()


_RECOVERED: dict[tuple[str, str], tuple[str, str | None, str, str, Any, str, bool]] = {
    # (mode, reconcile) -> action, failure class, approval, operation, envelope, job,
    # restage eligible
    ("exhausted", "landed"): (
        "succeeded",
        None,
        "executed",
        "succeeded",
        ("succeeded", None),
        "succeeded",
        False,
    ),
    ("cancelled", "landed"): (
        "succeeded",
        None,
        "executed",
        "succeeded",
        ("succeeded", None),
        "succeeded",
        False,
    ),
    ("exhausted", "absent"): (
        "failed",
        "retryable_no_effect",
        "executed",
        "failed",
        ("failed", "retry_budget_exhausted"),
        "failed",
        True,
    ),
    ("cancelled", "absent"): (
        "cancelled",
        "cancelled_no_effect",
        "refused",
        "cancelled",
        ("cancelled", "continuation_cancelled"),
        "cancelled",
        False,
    ),
    ("exhausted", "unknown"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        "unresolved",
        ("unresolved", "outcome_unknown"),
        "succeeded",
        False,
    ),
    ("cancelled", "unknown"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        "unresolved",
        ("unresolved", "outcome_unknown"),
        "succeeded",
        False,
    ),
}


@pytest.mark.parametrize(("mode", "found"), sorted(_RECOVERED))
def test_provider_recovery_after_a_lost_invocation_reconciles_first(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    found: str,
) -> None:
    destination = _register(monkeypatch, provider=True)
    key = f"lost-{mode}-{found}"
    held, queued = _approved_and_queued(ctx, key)
    action_id = queued.action["id"]
    request_id = f"rheo-external-{action_id}"
    _lose_the_invoking_worker(ctx, cancel=mode == "cancelled")
    destination.reconciled = {
        "landed": _Output(item_ref=f"synthetic:{key}"),
        "absent": None,
        "unknown": RuntimeError("the provider status read timed out"),
    }[found]

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, key)
    action, failure_class, approval_state, op_state, envelope, job_state, restage = (
        _RECOVERED[(mode, found)]
    )
    # Recovery never invokes, and it asks with the deterministic request id even
    # though the rolled-back run left none on the row.
    assert destination.calls == []
    assert destination.reconcile_calls == [request_id]
    assert done.action["state"] == action
    assert done.action["failure_class"] == failure_class
    assert done.action["provider_request_id"] == request_id
    assert done.approval == approval_state
    assert done.operation[0] == op_state
    assert done.envelope == envelope
    assert [row[1] for row in done.jobs] == [job_state]
    assert _action_read(ctx, action_id)["restage_eligible"] is restage
    if found == "landed":
        assert done.action["result_ref"] == f"synthetic:{key}"
        assert done.operation[1] == f"synthetic:{key}"
    if found == "unknown":
        # Resolution asks the provider about the same request.
        assert held.operation_id is not None
        destination.reconciled = None
        resolved = _resolve(
            ctx, held.operation_id, outcome="failed", note="provider: none"
        )
        assert resolved.ok, resolved
        assert destination.reconcile_calls == [request_id, request_id]


@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_with_an_unregistered_destination_stays_unresolved(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    destination = _register(monkeypatch, provider=True)
    key = f"unregistered-{cancel}"
    held, queued = _approved_and_queued(ctx, key)
    _lose_the_invoking_worker(ctx, cancel=cancel)
    # Nothing can now show the destination ran in process, or ask it what happened.
    DESTINATIONS._destinations.pop("synthetic")

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, key)
    assert destination.calls == []
    assert destination.reconcile_calls == []
    assert done.action["state"] == "unresolved"
    assert done.action["failure_class"] == "outcome_unknown"
    assert done.approval == "executed"
    assert done.operation[0] == "unresolved"
    assert [row[1] for row in done.jobs] == ["succeeded"]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False


# --- the loop's rerun-in-cancel and raise-then-requeue paths -----------------------


def _cancel_after_the_lease(
    ctx: WorkspaceContext, modes: list[ContinuationMode]
) -> ContinuationHandler:
    """The real continuation, with a cancellation committed between lease and run."""

    def continuation(uow: UnitOfWork, lease: ContinuationLease) -> ContinuationResult:
        if not modes:
            with open_unit_of_work(ctx) as other:
                other.connection.execute(
                    update(job)
                    .where(job.c.id == lease.job_id)
                    .values(cancel_requested=True)
                )
                other.commit()
        modes.append(lease.mode)
        return run_external_continuation(uow, lease)

    return continuation


@pytest.mark.parametrize("provider", [False, True])
def test_a_cancellation_that_beats_the_finish_reruns_in_cancel_mode(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: bool,
) -> None:
    destination = _register(monkeypatch, provider=provider)
    key = f"beaten-{provider}"
    held, queued = _approved_and_queued(ctx, key)
    request_id = f"rheo-external-{queued.action['id']}"
    modes: list[ContinuationMode] = []

    _visit(cluster, ctx.workspace_id, continuation=_cancel_after_the_lease(ctx, modes))
    done = _state(ctx, held, key)
    # The run invoked once; its finish lost to the cancellation and rolled back; the
    # same visit then reran at once in cancellation mode, which never invokes.
    assert modes == [ContinuationMode.RUN, ContinuationMode.CANCEL_REQUESTED]
    assert destination.calls == [key]
    if provider:
        # The provider kept the effect, so the cancellation cannot erase it.
        assert destination.reconcile_calls == [request_id, request_id]
        assert done.action["state"] == "succeeded"
        assert done.approval == "executed"
        assert done.operation == ("succeeded", f"synthetic:{key}", None)
        assert done.envelope == ("succeeded", None)
        assert [row[1] for row in done.jobs] == ["succeeded"]
    else:
        # In process, the rollback took the effect with it.
        assert destination.reconcile_calls == []
        assert done.action["state"] == "cancelled"
        assert done.action["failure_class"] == "cancelled_no_effect"
        assert done.action["attempts"] == 0
        assert done.approval == "refused"
        assert done.operation[0] == "cancelled"
        assert done.envelope == ("cancelled", "continuation_cancelled")
        assert [row[1] for row in done.jobs] == ["cancelled"]


def _raise_after_the_first_run(raised: list[str]) -> ContinuationHandler:
    """The real continuation, then a storage-style fault on its first return."""

    def continuation(uow: UnitOfWork, lease: ContinuationLease) -> ContinuationResult:
        result = run_external_continuation(uow, lease)
        if not raised:
            raised.append(result.disposition.value)
            raise RuntimeError("synthetic settlement fault")
        return result

    return continuation


@pytest.mark.parametrize("provider", [False, True])
def test_a_continuation_that_raises_rolls_back_and_is_requeued(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: bool,
) -> None:
    destination = _register(monkeypatch, provider=provider)
    key = f"raised-{provider}"
    held, queued = _approved_and_queued(ctx, key)
    request_id = f"rheo-external-{queued.action['id']}"
    raised: list[str] = []
    continuation = _raise_after_the_first_run(raised)

    _visit(cluster, ctx.workspace_id, continuation=continuation)
    requeued = _state(ctx, held, key)
    assert raised == ["succeeded"]
    assert destination.calls == [key]
    # Nothing the continuation wrote survived; the job is queued for another try.
    assert requeued.action["state"] == "pending"
    assert requeued.action["attempts"] == 0
    assert requeued.action["provider_request_id"] is None
    assert requeued.approval == "approved"
    assert requeued.operation[0] == "running"
    assert requeued.envelope == ("pending", None)
    assert [(row[1], row[5]) for row in requeued.jobs] == [("queued", 1)]
    with open_unit_of_work(ctx) as uow:
        error = uow.connection.execute(
            select(job.c.last_error).where(job.c.kind == EXTERNAL_ACTION_EXECUTE)
        ).scalar_one()
    assert error == "RuntimeError"

    _visit(cluster, ctx.workspace_id, after=600, continuation=continuation)
    done = _state(ctx, held, key)
    if provider:
        # Run mode reconciles first: the kept effect is found, not repeated.
        assert destination.calls == [key]
        assert destination.reconcile_calls == [request_id, request_id]
    else:
        # The in-process effect was rolled back, so it runs again, once.
        assert destination.calls == [key, key]
    assert done.action["state"] == "succeeded"
    assert done.approval == "executed"
    assert done.operation == ("succeeded", f"synthetic:{key}", None)
    assert [row[1] for row in done.jobs] == ["succeeded"]


# --- a retried run reconciles the earlier lease's effect before any refusal ---------


def _demote_the_owner(cluster: Any, *, workspace: UUID, owner_account_id: UUID) -> None:
    membership = control_tables.membership
    with cluster.backend.control_engine.begin() as connection:
        changed = connection.execute(
            update(membership)
            .where(
                (membership.c.account_id == owner_account_id)
                & (membership.c.workspace_id == workspace)
            )
            .values(role=Role.MEMBER.value)
        ).rowcount
    assert changed == 1


# (refusal, reconcile) -> action, failure class, approval, operation, envelope,
# restage eligible. The job ends ``succeeded`` in every row (handled_terminal).
_RETRIED: dict[
    tuple[str, str], tuple[str, str | None, str, tuple[str, str | None], Any, bool]
] = {
    ("unready", "landed"): (
        "succeeded",
        None,
        "executed",
        ("succeeded", None),
        ("succeeded", None),
        False,
    ),
    ("unready", "absent"): (
        "refused",
        "destination_unavailable_no_effect",
        "refused",
        ("failed", "guard_refused"),
        ("refused", "guard_refused"),
        True,
    ),
    ("unready", "unknown"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        ("unresolved", None),
        ("unresolved", "outcome_unknown"),
        False,
    ),
    ("demoted", "landed"): (
        "succeeded",
        None,
        "executed",
        ("succeeded", None),
        ("succeeded", None),
        False,
    ),
    ("demoted", "absent"): (
        "refused",
        "permanent_refusal_no_effect",
        "refused",
        ("failed", ROLE_NOT_PERMITTED),
        ("refused", ROLE_NOT_PERMITTED),
        False,
    ),
    ("demoted", "unknown"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        ("unresolved", None),
        ("unresolved", "outcome_unknown"),
        False,
    ),
    # Unregistered: nothing can be asked, whatever the provider would have said.
    ("unregistered", "landed"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        ("unresolved", None),
        ("unresolved", "outcome_unknown"),
        False,
    ),
    ("unregistered", "absent"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        ("unresolved", None),
        ("unresolved", "outcome_unknown"),
        False,
    ),
    ("unregistered", "unknown"): (
        "unresolved",
        "outcome_unknown",
        "executed",
        ("unresolved", None),
        ("unresolved", "outcome_unknown"),
        False,
    ),
}


@pytest.mark.parametrize(("refusal", "found"), sorted(_RETRIED))
def test_a_retried_run_reconciles_the_earlier_lease_before_refusing(
    ctx: WorkspaceContext,
    cluster: Any,
    monkeypatch: pytest.MonkeyPatch,
    workspace: UUID,
    owner_account_id: UUID,
    refusal: str,
    found: str,
) -> None:
    """Attempt 1 invokes the provider and crashes; attempt 2 runs with budget left.

    The crash rolled back the ``executing`` marker and the request id, but the
    provider kept the effect. Attempt 2 is ordinary run mode, and an effect-time
    refusal there must not erase that effect: the earlier lease is reconciled
    first, and only proven absence lets the refusal stand.
    """
    destination = _register(monkeypatch, provider=True)
    key = f"retried-{refusal}-{found}"
    held, queued = _approved_and_queued(ctx, key)
    action_id = queued.action["id"]
    request_id = f"rheo-external-{action_id}"
    raised: list[str] = []
    _visit(cluster, ctx.workspace_id, continuation=_raise_after_the_first_run(raised))
    crashed = _state(ctx, held, key)
    assert raised == ["succeeded"] and destination.calls == [key]
    assert crashed.action["state"] == "pending"
    assert crashed.action["provider_request_id"] is None
    assert [(row[1], row[5]) for row in crashed.jobs] == [("queued", 1)]

    # What the provider will answer attempt 2; "landed" is what it remembers.
    if found == "absent":
        destination.reconciled = None
    elif found == "unknown":
        destination.reconciled = RuntimeError("the provider status read timed out")
    if refusal == "unready":
        destination.ready = False
    elif refusal == "demoted":
        _demote_the_owner(
            cluster, workspace=workspace, owner_account_id=owner_account_id
        )
    else:
        DESTINATIONS._destinations.pop("synthetic")

    _visit(cluster, ctx.workspace_id, after=600)
    done = _state(ctx, held, key)
    action, failure_class, approval_state, op, envelope, restage = _RETRIED[
        (refusal, found)
    ]
    assert destination.calls == [key]
    if refusal == "unregistered":
        assert destination.reconcile_calls == [request_id]
    else:
        assert destination.reconcile_calls == [request_id, request_id]
        assert done.action["provider_request_id"] == request_id
    assert done.action["state"] == action
    assert done.action["failure_class"] == failure_class
    assert done.approval == approval_state
    assert done.operation[0] == op[0] and done.operation[2] == op[1]
    assert done.envelope == envelope
    assert [(row[1], row[5]) for row in done.jobs] == [("succeeded", 2)]
    assert _action_read(ctx, action_id)["restage_eligible"] is restage
    if found == "landed" and refusal != "unregistered":
        assert done.action["result_ref"] == f"synthetic:{key}"
        assert done.operation[1] == f"synthetic:{key}"


def test_a_retried_run_with_proven_absence_invokes_once_more(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1's effect did not land after all: attempt 2 asks once, then runs."""
    destination = _register(monkeypatch, provider=True)
    held, queued = _approved_and_queued(ctx, "retried-absent-runs")
    request_id = f"rheo-external-{queued.action['id']}"
    raised: list[str] = []
    _visit(cluster, ctx.workspace_id, continuation=_raise_after_the_first_run(raised))
    assert raised == ["succeeded"] and destination.calls == ["retried-absent-runs"]
    destination.reconciled = None

    _visit(cluster, ctx.workspace_id, after=600)
    done = _state(ctx, held, "retried-absent-runs")
    # One reconcile for the retried lease, reused at the invocation gate.
    assert destination.reconcile_calls == [request_id, request_id]
    assert destination.calls == ["retried-absent-runs", "retried-absent-runs"]
    assert done.action["state"] == "succeeded"
    assert done.action["attempts"] == 1
    assert done.operation == ("succeeded", "synthetic:retried-absent-runs", None)
    assert [(row[1], row[5]) for row in done.jobs] == [("succeeded", 2)]


def test_a_first_run_refuses_an_unregistered_destination_as_before(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No earlier lease, so nothing to reconcile: the refusal stands, restageable."""
    destination = _register(monkeypatch, provider=True)
    held, queued = _approved_and_queued(ctx, "first-unregistered")
    DESTINATIONS._destinations.pop("synthetic")
    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, "first-unregistered")
    assert destination.calls == [] and destination.reconcile_calls == []
    assert done.action["state"] == "refused"
    assert done.action["failure_class"] == "destination_unavailable_no_effect"
    assert [(row[1], row[5]) for row in done.jobs] == [("succeeded", 1)]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is True


# --- a cancellation that never leased proves no effect from the job row alone ------


@pytest.mark.parametrize("case", ["unregistered", "reconcile_raises"])
def test_cancelling_a_never_leased_job_settles_cancelled_without_a_lookup(
    ctx: WorkspaceContext, cluster: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """``attempts == 0`` at the cancel lease: no earlier lease could have invoked."""
    destination = _register(monkeypatch, provider=True)
    key = f"never-leased-{case}"
    held, queued = _approved_and_queued(ctx, key)
    with open_unit_of_work(ctx) as uow:
        job_id = uow.connection.execute(select(job.c.id)).scalar_one()
        assert request_cancellation(uow.connection, job_id, now=datetime.now(UTC)) == (
            "queued"
        )
        uow.commit()
    if case == "unregistered":
        DESTINATIONS._destinations.pop("synthetic")
    else:
        destination.reconciled = RuntimeError("the provider is unreachable")

    _visit(cluster, ctx.workspace_id)
    done = _state(ctx, held, key)
    assert destination.calls == [] and destination.reconcile_calls == []
    assert done.action["state"] == "cancelled"
    assert done.action["failure_class"] == "cancelled_no_effect"
    assert done.action["provider_request_id"] is None
    assert done.approval == "refused"
    assert done.operation[0] == "cancelled"
    assert done.envelope == ("cancelled", "continuation_cancelled")
    assert [(row[1], row[5]) for row in done.jobs] == [("cancelled", 0)]
    assert _action_read(ctx, queued.action["id"])["restage_eligible"] is False
