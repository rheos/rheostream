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
from rheo_core.approvals.tables import approval
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness
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
from rheo_core.operations.registry import REGISTRY
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job, operation
from rheo_core.work.jobs import enqueue_job, request_cancellation
from rheo_core.work.kinds import ContinuationMode, JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import func, select, update

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
    """The synthetic destination: a script of outcomes, and what it was asked."""

    calls: list[str] = field(default_factory=list)
    script: list[object] = field(default_factory=list)
    ready: bool = True
    reconciled: _Output | None = None
    reconcile_calls: list[str | None] = field(default_factory=list)

    def execute(self, ctx: WorkspaceContext, uow: Any, model: BaseModel) -> BaseModel:
        assert isinstance(model, _Input)
        self.calls.append(model.request_key)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        return _Output(item_ref=f"synthetic:{model.request_key}")

    def reconcile(
        self, ctx: WorkspaceContext, uow: Any, request_id: str | None
    ) -> BaseModel | None:
        self.reconcile_calls.append(request_id)
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
    destination = _Destination()

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


def _visit(cluster: Any, workspace: UUID, *, after: float = 1.0) -> None:
    kinds = JobKindRegistry()
    kinds.register_continuation(EXTERNAL_ACTION_EXECUTE, run_external_continuation)
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
