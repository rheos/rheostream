"""Grant-bound continuation, settlement and manual resolution of external actions.

Three entry points share one settlement vocabulary:

- :func:`queue_approved_external` runs inside ``core.approval.approve`` for a staged
  KEYED EXTERNAL/FINANCIAL action. It rechecks the binding, the held caller and every
  effective guard, then queues exactly one ``core.external_action.execute`` job
  (payload ``{"external_action_id": ...}``, ``operation_id`` null, no
  ``depends_on_ref``). It never invokes the destination.
- :func:`run_external_continuation` is that job kind's continuation. It mints an
  :class:`ExternalContinuationGrant` only after locking and matching the action,
  approval, operation, keyed reservation and leased job, then hands the grant to
  :func:`continue_approved_external_in_transaction`, which rechecks the held caller,
  payload, readiness and guards at effect time, invokes the exact registered
  destination at most once, and settles the action, approval, operation, keyed
  envelope and audit row in the loop's transaction. The loop writes the job.
- :func:`resolve_external` is ``core.operation.resolve`` for an ``unresolved``
  action: success needs a result ref the destination's reconcile confirms, failure
  needs the destination's proof of absence, and cancelled stays terminal.

A ``failed`` action is always proven no-effect. Anything possibly effective is
``unresolved`` and only :func:`resolve_external` moves it. An in-process destination
runs inside a savepoint of the continuation's own transaction, so its rollback is the
proof of absence; that is why an untyped error from one is ``retryable_no_effect``
while the same error from a provider invocation is ``outcome_unknown``.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_contracts import OperationDeclaration, WorkspaceContext
from sqlalchemy import Connection, null, select, update

from rheo_core.approvals import records as approval_records
from rheo_core.approvals import tables as approval_tables
from rheo_core.approvals.binding import (
    INVALID_APPROVAL,
    binding_detail,
    executable_payload,
    payload_digest,
)
from rheo_core.approvals.guards import effective_guards_for, run_guards
from rheo_core.audit import AUDIT_FAILED, AUDIT_REFUSED, AUDIT_SUCCEEDED, AuditOutcome
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import (
    context_for_approved_execution,
    context_from_operation,
)
from rheo_core.modules.execution import DESTINATIONS, DestinationRegistry
from rheo_core.modules.manifest import DestinationMode, DestinationRegistration
from rheo_core.operations import external_actions as actions
from rheo_core.operations import records as operation_records
from rheo_core.operations.external_actions import OPEN_STATES, ExternalActionRow
from rheo_core.operations.idempotency import IDEMPOTENCY_SCHEMA_INCOMPATIBLE
from rheo_core.operations.idempotency_tables import (
    EXTERNAL_FAILURE_CLASSES,
    idempotency_result,
)
from rheo_core.operations.refusals import OPERATION_UNKNOWN, OperationRefused
from rheo_core.operations.registry import (
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
)
from rheo_core.storage import work_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.work.jobs import EXTERNAL_ACTION_EXECUTE, LEASED, enqueue_job
from rheo_core.work.kinds import (
    ContinuationDisposition,
    ContinuationLease,
    ContinuationMode,
    ContinuationResult,
)

if TYPE_CHECKING:
    from rheo_core.events.consumers import ConsumerRegistry

logger = logging.getLogger("rheo_core.operations")

__all__ = [
    "EXTERNAL_ACTION_EXECUTE",
    "ExternalActionJobInput",
    "ExternalContinuationGrant",
    "ExternalDestinationError",
    "ExternalFailureClass",
    "GrantRefused",
    "continue_approved_external_in_transaction",
    "mint_continuation_grant",
    "queue_approved_external",
    "refuse_staged_external",
    "resolve_external",
    "run_external_continuation",
]

GUARD_REFUSED: Final = "guard_refused"
RETRY_BUDGET_EXHAUSTED: Final = "retry_budget_exhausted"
CONTINUATION_CANCELLED: Final = "continuation_cancelled"
APPROVAL_REFUSED: Final = "approval_refused"
OUTCOME_UNKNOWN: Final = "outcome_unknown"
DESTINATION_FAILED: Final = "destination_failed"
EXTERNAL_RESOLUTION_UNVERIFIED: Final = "external_resolution_unverified"
_DETAIL_LIMIT: Final = 2000


class ExternalFailureClass(StrEnum):
    """The closed proof classes an external outcome carries (spec table)."""

    DESTINATION_UNAVAILABLE_NO_EFFECT = "destination_unavailable_no_effect"
    PERMANENT_REFUSAL_NO_EFFECT = "permanent_refusal_no_effect"
    RETRYABLE_NO_EFFECT = "retryable_no_effect"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED_NO_EFFECT = "cancelled_no_effect"


if tuple(item.value for item in ExternalFailureClass) != EXTERNAL_FAILURE_CLASSES:
    raise RuntimeError(  # pragma: no cover - import-time invariant
        "ExternalFailureClass and the external_action failure_class check disagree"
    )


class ExternalDestinationError(Exception):
    """The only error a destination callback may raise: one class, a code, a text."""

    def __init__(
        self, failure_class: ExternalFailureClass, code: str, detail: str = ""
    ) -> None:
        super().__init__(f"{failure_class.value}: {code}")
        self.failure_class = ExternalFailureClass(failure_class)
        self.code = code
        self.detail = detail or code


class ExternalActionJobInput(BaseModel):
    """The whole payload of a continuation job: one action id, nothing executable."""

    model_config = ConfigDict(extra="forbid")

    external_action_id: UUID


@dataclass(frozen=True, slots=True)
class GrantRefused:
    code: str
    detail: str


_SEAL: Final = object()


@dataclass(frozen=True, slots=True)
class ExternalContinuationGrant:
    """Proof, minted under row locks, that one leased job may continue one action.

    Constructed only by :func:`mint_continuation_grant`; any other construction is a
    ``TypeError``. The continuation also re-locks and re-matches the rows it names,
    so a grant cannot outlive the binding it attests.
    """

    workspace_id: UUID
    job_id: UUID
    owner: str
    action_id: UUID
    approval_id: UUID
    operation_id: UUID
    operation_name: str
    idempotency_key: str
    destination_ref: str
    payload_digest: bytes
    request_schema_version: int
    result_schema_version: int
    attempts: int
    max_attempts: int
    mode: ContinuationMode
    seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _SEAL:
            raise TypeError(
                "an ExternalContinuationGrant is minted only by mint_continuation_grant"
            )


@dataclass(frozen=True, slots=True)
class _Bound:
    action: ExternalActionRow
    approval: approval_records.ApprovalRow


def _lock_and_bind(
    conn: Connection, *, action_id: UUID, job_id: UUID, owner: str
) -> _Bound | GrantRefused:
    """Lock action, approval, operation, keyed reservation and job, in that order,
    and prove they all name each other."""
    action = actions.get(conn, action_id=action_id, for_update=True)
    if action is None:
        return GrantRefused("grant_refused", "the external action does not exist")
    conn.execute(
        select(approval_tables.approval.c.id)
        .where(approval_tables.approval.c.id == action.approval_id)
        .with_for_update()
    )
    approval = approval_records.get(conn, approval_id=action.approval_id)
    op = conn.execute(
        select(
            work_tables.operation.c.name,
            work_tables.operation.c.state,
            work_tables.operation.c.approval_id,
        )
        .where(work_tables.operation.c.id == action.operation_id)
        .with_for_update()
    ).one_or_none()
    envelope = conn.execute(
        select(
            idempotency_result.c.state,
            idempotency_result.c.operation_id,
            idempotency_result.c.request_schema_version,
        )
        .where(
            idempotency_result.c.operation_name == action.operation_name,
            idempotency_result.c.idempotency_key == action.idempotency_key,
        )
        .with_for_update()
    ).one_or_none()
    job = conn.execute(
        select(
            work_tables.job.c.kind,
            work_tables.job.c.state,
            work_tables.job.c.lease_owner,
            work_tables.job.c.operation_id,
            work_tables.job.c.input,
        )
        .where(work_tables.job.c.id == job_id)
        .with_for_update()
    ).one_or_none()

    problems: list[str] = []
    if action.state not in OPEN_STATES:
        problems.append(f"action is {action.state!r}")
    if action.execution_job_id != job_id:
        problems.append("job is not the action's execution job")
    if job is None:
        problems.append("job is missing")
    else:
        if job.kind != EXTERNAL_ACTION_EXECUTE:
            problems.append("job kind is not the continuation kind")
        if job.state != LEASED or job.lease_owner != owner:
            problems.append("job is not leased by this worker")
        if job.operation_id is not None:
            problems.append("job carries an operation id")
        if job.input != {"external_action_id": str(action_id)}:
            problems.append("job payload is not exactly this action id")
    if approval is None:
        problems.append("approval is missing")
    else:
        if approval.state != approval_tables.APPROVED:
            problems.append(f"approval is {approval.state!r}")
        if (
            approval.operation_id != action.operation_id
            or approval.operation_name != action.operation_name
        ):
            problems.append("approval names another operation")
        if approval.destination_ref != action.destination_ref:
            problems.append("approval destination differs")
        if approval.payload_digest != action.approval_payload_digest:
            problems.append("approval digest differs")
    if op is None:
        problems.append("operation record is missing")
    else:
        if op.name != action.operation_name or op.approval_id != action.approval_id:
            problems.append("operation record names another action")
        if op.state != operation_records.RUNNING:
            problems.append(f"operation is {op.state!r}")
    if envelope is None:
        problems.append("keyed reservation is missing")
    else:
        if envelope.state != "pending":
            problems.append(f"keyed reservation is {envelope.state!r}")
        if envelope.operation_id != action.operation_id:
            problems.append("keyed reservation names another operation")
        if envelope.request_schema_version != action.request_schema_version:
            problems.append("keyed request schema version differs")
    if problems or approval is None:
        return GrantRefused("grant_refused", "; ".join(problems))
    return _Bound(action, approval)


def mint_continuation_grant(
    conn: Connection,
    *,
    workspace_id: UUID,
    job_id: UUID,
    owner: str,
    external_action_id: UUID,
    attempts: int,
    max_attempts: int,
    mode: ContinuationMode,
) -> ExternalContinuationGrant | GrantRefused:
    bound = _lock_and_bind(
        conn, action_id=external_action_id, job_id=job_id, owner=owner
    )
    if isinstance(bound, GrantRefused):
        return bound
    action = bound.action
    return ExternalContinuationGrant(
        workspace_id=workspace_id,
        job_id=job_id,
        owner=owner,
        action_id=action.id,
        approval_id=action.approval_id,
        operation_id=action.operation_id,
        operation_name=action.operation_name,
        idempotency_key=action.idempotency_key,
        destination_ref=action.destination_ref,
        payload_digest=action.approval_payload_digest,
        request_schema_version=action.request_schema_version,
        result_schema_version=action.result_schema_version,
        attempts=attempts,
        max_attempts=max_attempts,
        mode=mode,
        seal=_SEAL,
    )


def _matches(grant: ExternalContinuationGrant, bound: _Bound) -> bool:
    action = bound.action
    return (
        action.approval_id == grant.approval_id
        and action.operation_id == grant.operation_id
        and action.operation_name == grant.operation_name
        and action.idempotency_key == grant.idempotency_key
        and action.destination_ref == grant.destination_ref
        and action.approval_payload_digest == grant.payload_digest
        and action.request_schema_version == grant.request_schema_version
        and action.result_schema_version == grant.result_schema_version
    )


# --- settlement ---------------------------------------------------------------------


@dataclass(slots=True)
class _Settle:
    conn: Connection
    action_id: UUID
    approval_id: UUID
    operation_id: UUID
    operation_name: str
    key: str
    digest: bytes
    now: datetime
    uow: UnitOfWork | None = None
    held: WorkspaceContext | None = None
    operation: RegisteredOperation | None = None
    model_input: BaseModel | None = None


def _must(applied: bool, what: str) -> None:
    if not applied:
        raise RuntimeError(f"external settlement could not {what}")


def _short(text: str) -> str:
    return text[:_DETAIL_LIMIT]


def _envelope_error(s: _Settle, *, state: str, code: str, detail: str) -> None:
    changed = s.conn.execute(
        update(idempotency_result)
        .where(
            idempotency_result.c.operation_name == s.operation_name,
            idempotency_result.c.idempotency_key == s.key,
            idempotency_result.c.state == "pending",
        )
        .values(
            state=state,
            # SQL NULL, not JSON null: the terminal-shape check needs the former.
            result=null(),
            result_schema_version=None,
            error_code=code,
            error_detail=_short(detail),
            updated_at=s.now,
        )
    ).rowcount
    _must(changed == 1, "advance the pending keyed reservation")


def _audit(s: _Settle, outcome: AuditOutcome, *, required: bool) -> None:
    from rheo_core.operations.dispatch import record_operation_audit

    if s.uow is None or s.held is None or s.operation is None or s.model_input is None:
        _must(not required, "write the success audit row without its held caller")
        return
    written = record_operation_audit(
        s.held,
        s.uow,
        s.operation,
        request_digest=s.digest,
        model_input=s.model_input,
        outcome=outcome,
        operation_id=s.operation_id,
    )
    _must(written or not required, "write the success audit row")


def _action_terminal(
    s: _Settle,
    *,
    state: str,
    failure_class: ExternalFailureClass,
    code: str,
    detail: str,
) -> None:
    _must(
        actions.update_action(
            s.conn,
            action_id=s.action_id,
            from_states=OPEN_STATES,
            state=state,
            failure_class=failure_class.value,
            error_code=code,
            error_detail=_short(detail),
            updated_at=s.now,
            terminal_at=s.now,
        ),
        f"settle the action {state}",
    )


def _settle_success(
    s: _Settle, output: BaseModel, *, check_kind: str
) -> ContinuationResult:
    from rheo_core.operations.idempotency import finish_success

    assert s.operation is not None
    declaration = s.operation.declaration
    external = declaration.external
    assert external is not None
    result_ref = getattr(output, external.result_ref_field)
    _must(isinstance(result_ref, str) and bool(result_ref.strip()), "read a result ref")
    finish_success(s.conn, declaration=declaration, key=s.key, result=output, now=s.now)
    _must(
        actions.update_action(
            s.conn,
            action_id=s.action_id,
            from_states=OPEN_STATES,
            state="succeeded",
            result_ref=result_ref,
            result_detail=output.model_dump(mode="json"),
            failure_class=None,
            error_code=None,
            error_detail=None,
            updated_at=s.now,
            terminal_at=s.now,
        ),
        "settle the action succeeded",
    )
    _must(
        approval_records.mark_executed(s.conn, approval_id=s.approval_id, now=s.now),
        "consume the approval",
    )
    _must(
        operation_records.finish_succeeded(
            s.conn,
            operation_id=s.operation_id,
            now=s.now,
            terminal_check_kind=check_kind,
            terminal_check_at=s.now,
            result_ref=result_ref,
        ),
        "end the operation succeeded",
    )
    _audit(s, AUDIT_SUCCEEDED, required=True)
    return ContinuationResult(ContinuationDisposition.SUCCEEDED)


def _settle_refused(
    s: _Settle, failure_class: ExternalFailureClass, code: str, detail: str
) -> ContinuationResult:
    """Refused before any effect: action/approval refused, operation failed."""
    _action_terminal(
        s, state="refused", failure_class=failure_class, code=code, detail=detail
    )
    _must(
        approval_records.mark_guard_refused(s.conn, approval_id=s.approval_id),
        "refuse the approval",
    )
    _must(
        operation_records.finish_failed(
            s.conn,
            operation_id=s.operation_id,
            now=s.now,
            error_code=code,
            error_text=_short(detail),
        ),
        "end the operation failed",
    )
    _envelope_error(s, state="refused", code=code, detail=detail)
    _audit(s, AUDIT_REFUSED, required=False)
    return ContinuationResult(
        ContinuationDisposition.HANDLED_TERMINAL, f"{code}: {detail}"
    )


def _settle_failed(s: _Settle, code: str, detail: str) -> ContinuationResult:
    """Budget exhausted with absence proven: failed, approval consumed, job failed."""
    _action_terminal(
        s,
        state="failed",
        failure_class=ExternalFailureClass.RETRYABLE_NO_EFFECT,
        code=code,
        detail=detail,
    )
    _must(
        approval_records.mark_executed(s.conn, approval_id=s.approval_id, now=s.now),
        "consume the approval",
    )
    _must(
        operation_records.finish_failed(
            s.conn,
            operation_id=s.operation_id,
            now=s.now,
            error_code=code,
            error_text=_short(detail),
        ),
        "end the operation failed",
    )
    _envelope_error(s, state="failed", code=code, detail=detail)
    _audit(s, AUDIT_FAILED, required=False)
    return ContinuationResult(
        ContinuationDisposition.FAILED_TERMINAL, f"{code}: {detail}"
    )


def _settle_cancelled(s: _Settle, code: str, detail: str) -> ContinuationResult:
    """Cancellation with absence proven: cancelled, approval refused, job cancelled."""
    _action_terminal(
        s,
        state="cancelled",
        failure_class=ExternalFailureClass.CANCELLED_NO_EFFECT,
        code=code,
        detail=detail,
    )
    _must(
        approval_records.mark_guard_refused(s.conn, approval_id=s.approval_id),
        "refuse the approval",
    )
    _must(
        operation_records.finish_cancelled(
            s.conn, operation_id=s.operation_id, now=s.now
        ),
        "end the operation cancelled",
    )
    _envelope_error(s, state="cancelled", code=code, detail=detail)
    _audit(s, AUDIT_REFUSED, required=False)
    return ContinuationResult(
        ContinuationDisposition.CANCELLED_TERMINAL, f"{code}: {detail}"
    )


def _settle_unresolved(s: _Settle, code: str, detail: str) -> ContinuationResult:
    """The effect cannot be proved either way: unresolved until a person resolves it."""
    _action_terminal(
        s,
        state="unresolved",
        failure_class=ExternalFailureClass.OUTCOME_UNKNOWN,
        code=code,
        detail=detail,
    )
    _must(
        approval_records.mark_executed(s.conn, approval_id=s.approval_id, now=s.now),
        "consume the approval",
    )
    _must(
        operation_records.mark_unresolved(
            s.conn, operation_id=s.operation_id, now=s.now
        ),
        "end the operation unresolved",
    )
    _envelope_error(s, state="unresolved", code=code, detail=detail)
    _audit(s, AUDIT_FAILED, required=False)
    return ContinuationResult(
        ContinuationDisposition.HANDLED_TERMINAL, f"{code}: {detail}"
    )


def _classified(
    s: _Settle,
    grant: ExternalContinuationGrant,
    failure_class: ExternalFailureClass,
    code: str,
    detail: str,
) -> ContinuationResult:
    if failure_class in {
        ExternalFailureClass.DESTINATION_UNAVAILABLE_NO_EFFECT,
        ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT,
    }:
        return _settle_refused(s, failure_class, code, detail)
    if failure_class is ExternalFailureClass.CANCELLED_NO_EFFECT:
        return _settle_cancelled(s, code, detail)
    if failure_class is ExternalFailureClass.OUTCOME_UNKNOWN:
        return _settle_unresolved(s, code, detail)
    if grant.attempts >= grant.max_attempts:
        return _settle_failed(s, RETRY_BUDGET_EXHAUSTED, f"{code}: {detail}")
    _must(
        actions.update_action(
            s.conn,
            action_id=s.action_id,
            from_states=OPEN_STATES,
            error_code=code,
            error_detail=_short(detail),
            updated_at=s.now,
        ),
        "record the retryable error",
    )
    return ContinuationResult(ContinuationDisposition.RETRY, f"{code}: {detail}")


# --- the held caller and the effect --------------------------------------------------


def _held_caller(
    conn: Connection, workspace_id: UUID, approval: approval_records.ApprovalRow
) -> WorkspaceContext | Refusal:
    provenance = operation_records.get_context_provenance(
        conn, operation_id=approval.operation_id
    )
    if provenance is None:
        return Refusal(
            "operation_provenance_missing",
            f"the held operation {approval.operation_id} has no stored provenance",
        )
    return context_from_operation(
        workspace_id,
        actor_kind=approval.actor_kind,
        actor_id=approval.actor_id,
        audience_kind=provenance.audience_kind,
        audience_id=provenance.audience_id,
        entry=provenance.entry,
        purpose=approval.purpose,
    )


def _provider_request_id(action_id: UUID) -> str:
    """Deterministic, so reconcile can recompute it after any rollback or crash."""
    return f"rheo-external-{action_id}"


def _reconcile(
    s: _Settle,
    registration: DestinationRegistration,
    provider_request_id: str | None,
) -> BaseModel | None | ExternalDestinationError:
    """Ask a provider destination whether the effect landed.

    ``BaseModel`` is proven success, ``None`` proven absence, and an error is either a
    typed no-effect class (absence) or ``outcome_unknown``.
    """
    reconcile = registration.reconcile
    if reconcile is None or s.held is None or s.uow is None:
        return ExternalDestinationError(
            ExternalFailureClass.OUTCOME_UNKNOWN,
            OUTCOME_UNKNOWN,
            "the destination cannot be reconciled",
        )
    try:
        found = reconcile(s.held, HandlerUnitOfWork(s.uow), provider_request_id)
    except ExternalDestinationError as exc:
        return exc
    except Exception as exc:
        return ExternalDestinationError(
            ExternalFailureClass.OUTCOME_UNKNOWN, OUTCOME_UNKNOWN, type(exc).__name__
        )
    if found is None:
        return None
    if s.operation is None or not isinstance(found, s.operation.declaration.output):
        return ExternalDestinationError(
            ExternalFailureClass.OUTCOME_UNKNOWN,
            OUTCOME_UNKNOWN,
            "reconcile returned no valid declared output",
        )
    return found


def _recover(
    s: _Settle,
    grant: ExternalContinuationGrant,
    destination: DestinationRegistration | None,
    provider_request_id: str | None,
) -> ContinuationResult:
    """Cancellation or exhausted-budget recovery: reconcile, never invoke.

    **Only an in-process destination can prove absence from the action row.** Its
    effect runs in a savepoint and commits only together with its success settlement,
    so an open action means none landed. A provider's effect does not roll back with
    the transaction that invoked it, and that transaction also carried the
    ``executing`` marker and ``provider_request_id``: a crash, a raise or a lost
    finish after the call erases both and keeps the effect. So a provider destination
    is always reconciled, with the deterministic request id when the row holds none,
    and a destination that is no longer registered cannot be shown to be in-process
    or asked, so it settles ``unresolved``. Nothing that cannot prove absence settles
    ``failed`` or ``cancelled``.
    """
    cancelling = grant.mode is ContinuationMode.CANCEL_REQUESTED
    code = CONTINUATION_CANCELLED if cancelling else RETRY_BUDGET_EXHAUSTED
    absent_detail = (
        "cancelled before any effect"
        if cancelling
        else "retry budget exhausted with no effect"
    )
    if destination is None:
        return _settle_unresolved(
            s,
            OUTCOME_UNKNOWN,
            "the destination is no longer registered, so absence cannot be proven",
        )
    if destination.mode is DestinationMode.PROVIDER:
        request_id = provider_request_id or _provider_request_id(grant.action_id)
        _must(
            actions.update_action(
                s.conn,
                action_id=s.action_id,
                from_states=OPEN_STATES,
                provider_request_id=request_id,
                updated_at=s.now,
            ),
            "record the reconciled provider request id",
        )
        found = _reconcile(s, destination, request_id)
        if isinstance(found, BaseModel):
            return _settle_success(
                s, found, check_kind=operation_records.PROVIDER_STATUS
            )
        if isinstance(found, ExternalDestinationError) and (
            found.failure_class is ExternalFailureClass.OUTCOME_UNKNOWN
        ):
            return _settle_unresolved(s, found.code, found.detail)
    # Reconcile proved absence, or an in-process effect is absent with the open action.
    if cancelling:
        return _settle_cancelled(s, code, absent_detail)
    return _settle_failed(s, code, absent_detail)


def continue_approved_external_in_transaction(
    grant: ExternalContinuationGrant,
    uow: UnitOfWork,
    *,
    now: datetime,
    consumers: "ConsumerRegistry | None" = None,
    registry: OperationRegistry | None = None,
    destinations: DestinationRegistry | None = None,
) -> ContinuationResult:
    """Continue exactly the action ``grant`` names, in the caller's transaction.

    Accepts no operation name, payload, key, caller or destination of its own. It
    never dispatches, never calls ``call_in_transaction`` and never creates an
    approval. Every outcome settles atomically; the caller writes the job.
    """
    if not isinstance(grant, ExternalContinuationGrant):
        raise TypeError("continuation requires an ExternalContinuationGrant")
    conn = uow.connection
    bound = _lock_and_bind(
        conn, action_id=grant.action_id, job_id=grant.job_id, owner=grant.owner
    )
    if isinstance(bound, GrantRefused) or not _matches(grant, bound):
        detail = bound.detail if isinstance(bound, GrantRefused) else "grant mismatch"
        return ContinuationResult(
            ContinuationDisposition.FAILED_TERMINAL, f"grant_refused: {detail}"
        )
    chosen = REGISTRY if registry is None else registry
    chosen_destinations = DESTINATIONS if destinations is None else destinations
    operation = chosen.lookup(grant.operation_name)
    held = _held_caller(conn, grant.workspace_id, bound.approval)
    s = _Settle(
        conn=conn,
        uow=uow,
        action_id=grant.action_id,
        approval_id=grant.approval_id,
        operation_id=grant.operation_id,
        operation_name=grant.operation_name,
        key=grant.idempotency_key,
        digest=grant.payload_digest,
        now=now,
        held=None if isinstance(held, Refusal) else held,
        operation=operation,
    )
    body = approval_records.payload_body(conn, approval_id=grant.approval_id)
    if operation is not None and body is not None:
        try:
            s.model_input = operation.declaration.input_model.model_validate(body)
        except ValidationError:
            s.model_input = None
    found = chosen_destinations.lookup(grant.destination_ref)
    destination = None if found is None else found.registration
    if grant.mode is not ContinuationMode.RUN:
        return _recover(s, grant, destination, bound.action.provider_request_id)

    permanent = ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT
    unavailable = ExternalFailureClass.DESTINATION_UNAVAILABLE_NO_EFFECT
    if operation is None:
        return _settle_refused(
            s, permanent, OPERATION_UNKNOWN, f"{grant.operation_name} is not registered"
        )
    declaration = operation.declaration
    keyed = declaration.keyed
    if keyed is None or (keyed.request_schema_version, keyed.result_schema_version) != (
        grant.request_schema_version,
        grant.result_schema_version,
    ):
        return _settle_refused(
            s,
            permanent,
            IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
            "the declaration's keyed versions differ from the staged action's",
        )
    model_input = s.model_input
    if model_input is None:
        return _settle_refused(
            s, permanent, INVALID_APPROVAL, "the approved payload no longer validates"
        )
    if payload_digest(executable_payload(model_input)) != grant.payload_digest:
        return _settle_refused(
            s, permanent, INVALID_APPROVAL, "the approved payload digest differs"
        )
    if isinstance(held, Refusal):
        return _settle_refused(s, permanent, held.state, str(held))
    authorized = chosen.authorize(held, grant.operation_name)
    if isinstance(authorized, Refusal):
        return _settle_refused(s, permanent, authorized.state, str(authorized))
    from rheo_core.modules.readiness import ModuleNotReady, require_ready

    try:
        require_ready(conn, operation.module_id)
    except ModuleNotReady as exc:
        return _settle_refused(s, unavailable, "module_not_ready", str(exc))
    refusal = run_guards(
        held,
        uow,
        approval=bound.approval,
        model_input=model_input,
        now=now,
        guards=effective_guards_for(declaration, destinations=chosen_destinations),
    )
    if refusal is not None:
        return _settle_refused(
            s,
            unavailable if refusal.guard == "DestinationGuard" else permanent,
            GUARD_REFUSED,
            f"{refusal.guard}: {refusal.detail}",
        )
    if destination is None:
        return _settle_refused(
            s, unavailable, GUARD_REFUSED, "DestinationGuard: destination unregistered"
        )

    provider = destination.mode is DestinationMode.PROVIDER
    provider_request_id = _provider_request_id(grant.action_id) if provider else None
    _must(
        actions.update_action(
            conn,
            action_id=grant.action_id,
            from_states=OPEN_STATES,
            state="executing",
            attempts=bound.action.attempts + 1,
            provider_request_id=provider_request_id,
            updated_at=now,
        ),
        "mark the action executing",
    )
    if provider:
        prior = _reconcile(s, destination, provider_request_id)
        if isinstance(prior, BaseModel):
            return _settle_success(
                s, prior, check_kind=operation_records.PROVIDER_STATUS
            )
        if isinstance(prior, ExternalDestinationError):
            return _classified(s, grant, prior.failure_class, prior.code, prior.detail)

    view = HandlerUnitOfWork(uow, operation_id=grant.operation_id, consumers=consumers)
    savepoint = conn.begin_nested()
    try:
        output = destination.execute(held, view, model_input)
    except ExternalDestinationError as exc:
        savepoint.rollback()
        failure_class = exc.failure_class
        if not provider and failure_class is ExternalFailureClass.OUTCOME_UNKNOWN:
            # The savepoint rollback is itself the proof of absence in process.
            failure_class = ExternalFailureClass.RETRYABLE_NO_EFFECT
        return _classified(s, grant, failure_class, exc.code, exc.detail)
    except Exception as exc:
        savepoint.rollback()
        logger.warning(
            "external_destination_failed",
            extra={
                "operation": grant.operation_name,
                "destination_ref": grant.destination_ref,
                "exception_type": type(exc).__name__,
            },
        )
        return _classified(
            s,
            grant,
            ExternalFailureClass.OUTCOME_UNKNOWN
            if provider
            else ExternalFailureClass.RETRYABLE_NO_EFFECT,
            DESTINATION_FAILED,
            type(exc).__name__,
        )
    if not isinstance(output, declaration.output):
        savepoint.rollback()
        if provider:
            return _settle_unresolved(
                s, OUTCOME_UNKNOWN, "the provider returned no valid declared output"
            )
        return _settle_refused(
            s, permanent, "output_invalid", "the destination returned invalid output"
        )
    savepoint.commit()
    return _settle_success(
        s,
        output,
        check_kind=(
            operation_records.PROVIDER_STATUS
            if provider
            else operation_records.HANDLER_RETURNED
        ),
    )


def run_external_continuation(
    uow: UnitOfWork, lease: ContinuationLease
) -> ContinuationResult:
    """The registered ``core.external_action.execute`` continuation."""
    try:
        job_input = ExternalActionJobInput.model_validate(lease.payload)
    except ValidationError:
        return ContinuationResult(
            ContinuationDisposition.FAILED_TERMINAL,
            "grant_refused: the job payload is not one external action id",
        )
    grant = mint_continuation_grant(
        uow.connection,
        workspace_id=lease.workspace_id,
        job_id=lease.job_id,
        owner=lease.owner,
        external_action_id=job_input.external_action_id,
        attempts=lease.attempts,
        max_attempts=lease.max_attempts,
        mode=lease.mode,
    )
    if isinstance(grant, GrantRefused):
        return ContinuationResult(
            ContinuationDisposition.FAILED_TERMINAL, f"{grant.code}: {grant.detail}"
        )
    return continue_approved_external_in_transaction(
        grant, uow, now=lease.now, consumers=lease.consumers
    )


# --- the approval side --------------------------------------------------------------


def queue_approved_external(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    approval: approval_records.ApprovalRow,
    action: ExternalActionRow,
    now: datetime,
    registry: OperationRegistry | None = None,
    destinations: DestinationRegistry | None = None,
) -> None:
    """Queue the one continuation for a just-approved staged action.

    The approval is ``approved`` in this transaction. A binding mismatch or a failed
    held-caller rebuild raises, rolling the approval back. A guard refusal is
    durable: action/approval refused, operation failed, keyed refused, the held
    operation's ``refused`` audit row, no job.
    """
    from rheo_core.settings import resolve
    from rheo_core.settings.storage_source import TransactionBoundOverrideSource

    conn = uow.connection
    chosen = REGISTRY if registry is None else registry
    operation = chosen.lookup(approval.operation_name)
    if operation is None:
        raise OperationRefused(
            OPERATION_UNKNOWN,
            f"{approval.operation_name} is approved but not registered in this process",
        )
    declaration = operation.declaration
    body = approval_records.payload_body(conn, approval_id=approval.id)
    if body is None:
        raise OperationRefused(INVALID_APPROVAL, "the payload snapshot is gone")
    try:
        model_input = declaration.input_model.model_validate(body)
    except ValidationError:
        raise OperationRefused(
            INVALID_APPROVAL, "the stored payload no longer validates"
        ) from None
    digest = payload_digest(executable_payload(model_input))
    detail = binding_detail(
        approved_digest=approval.payload_digest,
        digest=digest,
        window_start=approval.window_start,
        window_end=approval.window_end,
        now=now,
    )
    if detail is None and digest != action.approval_payload_digest:
        detail = "the staged action is bound to another payload digest"
    if detail is not None:
        raise OperationRefused(INVALID_APPROVAL, detail)
    held = context_for_approved_execution(ctx, approval)
    refusal = run_guards(
        ctx if isinstance(held, Refusal) else held,
        uow,
        approval=approval,
        model_input=model_input,
        now=now,
        guards=effective_guards_for(declaration, destinations=destinations),
    )
    if refusal is not None:
        s = _Settle(
            conn=conn,
            action_id=action.id,
            approval_id=approval.id,
            operation_id=approval.operation_id,
            operation_name=action.operation_name,
            key=action.idempotency_key,
            digest=digest,
            now=now,
            uow=uow,
            held=None if isinstance(held, Refusal) else held,
            operation=operation,
            model_input=model_input,
        )
        text = f"{refusal.guard}: {refusal.detail}"
        _action_terminal(
            s,
            state="refused",
            failure_class=(
                ExternalFailureClass.DESTINATION_UNAVAILABLE_NO_EFFECT
                if refusal.guard == "DestinationGuard"
                else ExternalFailureClass.PERMANENT_REFUSAL_NO_EFFECT
            ),
            code=GUARD_REFUSED,
            detail=text,
        )
        _must(
            approval_records.mark_guard_refused(conn, approval_id=approval.id),
            "refuse the approval",
        )
        _must(
            operation_records.finish_held_failed(
                conn,
                operation_id=approval.operation_id,
                now=now,
                error_code=GUARD_REFUSED,
                error_text=_short(text),
            ),
            "end the held operation failed",
        )
        _envelope_error(s, state="refused", code=GUARD_REFUSED, detail=text)
        # The same held-operation row an effect-time refusal writes.
        _audit(s, AUDIT_REFUSED, required=False)
        return
    if isinstance(held, Refusal):
        raise OperationRefused(held.state, str(held))
    settings = resolve(
        workspace_id=ctx.workspace_id,
        account_id=ctx.principal.account_id,
        source=TransactionBoundOverrideSource(conn, workspace_id=ctx.workspace_id),
    )
    job_id = enqueue_job(
        conn,
        kind=EXTERNAL_ACTION_EXECUTE,
        payload={"external_action_id": str(action.id)},
        now=now,
        max_attempts=settings.get_int("work.max_attempts"),
        operation_id=None,
    )
    assert isinstance(job_id, UUID)
    _must(
        actions.attach_job(conn, action_id=action.id, job_id=job_id, now=now),
        "bind the continuation job",
    )
    _must(
        operation_records.start_held(conn, operation_id=approval.operation_id, now=now),
        "start the held operation",
    )
    if isinstance(uow, HandlerUnitOfWork):
        uow.request_due_mark()


def refuse_staged_external(
    conn: Connection, *, action: ExternalActionRow, now: datetime
) -> None:
    """A person refused a still-pending approval: no job, no destination call."""
    s = _Settle(
        conn=conn,
        action_id=action.id,
        approval_id=action.approval_id,
        operation_id=action.operation_id,
        operation_name=action.operation_name,
        key=action.idempotency_key,
        digest=action.approval_payload_digest,
        now=now,
    )
    detail = "the approval was refused before any effect"
    _action_terminal(
        s,
        state="refused",
        failure_class=ExternalFailureClass.CANCELLED_NO_EFFECT,
        code=APPROVAL_REFUSED,
        detail=detail,
    )
    _envelope_error(s, state="refused", code=APPROVAL_REFUSED, detail=detail)


# --- manual resolution --------------------------------------------------------------


def _envelope_from_unresolved(
    conn: Connection,
    *,
    action: ExternalActionRow,
    now: datetime,
    **values: object,
) -> None:
    changed = conn.execute(
        update(idempotency_result)
        .where(
            idempotency_result.c.operation_name == action.operation_name,
            idempotency_result.c.idempotency_key == action.idempotency_key,
            idempotency_result.c.state == "unresolved",
        )
        .values(updated_at=now, **values)
    ).rowcount
    if changed != 1:
        raise OperationRefused(
            "operation_state", "the keyed reservation is not unresolved"
        )


def resolve_external(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    action: ExternalActionRow,
    outcome: str,
    note: str,
    result_ref: str | None,
    terminal_check_kind: str | None,
    now: datetime,
    registry: OperationRegistry | None = None,
    destinations: DestinationRegistry | None = None,
) -> None:
    """Move one ``unresolved`` external action and every related row together."""
    conn = uow.connection
    locked = actions.get(conn, action_id=action.id, for_update=True)
    if locked is None or locked.state != "unresolved":
        raise OperationRefused(
            "operation_state", "only an unresolved external action can be resolved"
        )
    chosen = REGISTRY if registry is None else registry
    chosen_destinations = DESTINATIONS if destinations is None else destinations
    operation = chosen.lookup(locked.operation_name)
    found = chosen_destinations.lookup(locked.destination_ref)
    declaration: OperationDeclaration | None = (
        None if operation is None else operation.declaration
    )
    verified: BaseModel | None = None
    if outcome in {"succeeded", "failed"}:
        reconcile = None if found is None else found.registration.reconcile
        if reconcile is None or declaration is None:
            raise OperationRefused(
                EXTERNAL_RESOLUTION_UNVERIFIED,
                "the destination offers no reconcile to verify this resolution",
            )
        try:
            verified = reconcile(
                ctx, HandlerUnitOfWork(uow), locked.provider_request_id
            )
        except Exception as exc:
            raise OperationRefused(
                EXTERNAL_RESOLUTION_UNVERIFIED,
                f"the destination could not verify the outcome ({type(exc).__name__})",
            ) from None
    if outcome == "succeeded":
        assert declaration is not None and declaration.external is not None
        if (
            not result_ref
            or not result_ref.strip()
            or not isinstance(verified, declaration.output)
            or getattr(verified, declaration.external.result_ref_field) != result_ref
        ):
            raise OperationRefused(
                EXTERNAL_RESOLUTION_UNVERIFIED,
                "the destination does not confirm this result_ref",
            )
        _envelope_from_unresolved(
            conn,
            action=locked,
            now=now,
            state="succeeded",
            result=verified.model_dump(mode="json"),
            result_schema_version=locked.result_schema_version,
            error_code=None,
            error_detail=None,
        )
        values: dict[str, object] = {
            "state": "succeeded",
            "result_ref": result_ref,
            "result_detail": verified.model_dump(mode="json"),
            "failure_class": None,
            "error_code": None,
            "error_detail": None,
        }
    elif outcome == "failed":
        if verified is not None:
            raise OperationRefused(
                EXTERNAL_RESOLUTION_UNVERIFIED,
                "the destination reports the effect landed; it did not fail",
            )
        _envelope_from_unresolved(
            conn,
            action=locked,
            now=now,
            state="failed",
            error_code="resolved_no_effect",
            error_detail=_short(note),
        )
        values = {
            "state": "failed",
            "error_code": "resolved_no_effect",
            "error_detail": _short(note),
        }
    else:
        _envelope_from_unresolved(
            conn,
            action=locked,
            now=now,
            state="cancelled",
            error_code="resolved_cancelled",
            error_detail=_short(note),
        )
        values = {
            "state": "cancelled",
            "error_code": "resolved_cancelled",
            "error_detail": _short(note),
        }
    if not actions.update_action(
        conn,
        action_id=locked.id,
        from_states=("unresolved",),
        updated_at=now,
        terminal_at=now,
        **values,
    ):
        raise OperationRefused("operation_state", "the action left 'unresolved'")
    if not operation_records.resolve(
        conn,
        operation_id=locked.operation_id,
        outcome=outcome,
        note=note,
        now=now,
        terminal_check_kind=(
            terminal_check_kind or operation_records.PROVIDER_STATUS
            if outcome == "succeeded"
            else None
        ),
        result_ref=result_ref if outcome == "succeeded" else None,
    ):
        raise OperationRefused("operation_state", "the operation left 'unresolved'")
