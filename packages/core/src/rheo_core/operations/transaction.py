"""Synchronous registered calls inside an existing transaction.

Consumers receive only their subscription's explicit operation names. This path
never opens or commits a transaction and cannot execute approval-gated or deferred
operations. A failure propagates to the caller, rolling back the entire delivery.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import Idempotency, RecordRef, SafetyClass, WorkspaceContext

from rheo_core.audit import AUDIT_SUCCEEDED
from rheo_core.boundary.context import Refusal
from rheo_core.operations.dispatch import (
    _audit_write,
    _request_digest,
    record_operation_audit,
)
from rheo_core.operations.idempotency import (
    CanonicalRequest,
    IdempotencyRow,
    RetainedReplay,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import (
    REGISTRY,
    OperationRegistry,
    RegisteredOperation,
)
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.refs.short import validation_context

if TYPE_CHECKING:
    from rheo_core.modules.execution import DestinationRegistry

DESTINATION_GUARD_REFUSED = "destination_guard_refused"


@dataclass(frozen=True, slots=True)
class StagedExternalAction:
    """The durable identities created, or replayed, for one exact keyed request.

    ``retained`` is ``None`` for a fresh stage. On a replay it is the retained
    envelope's outcome: ``pending`` while the action is unsettled, otherwise the
    stored success or terminal error, which the caller returns instead of a hold.
    """

    action_id: UUID
    approval_id: UUID
    operation_id: UUID
    state: str
    created: bool
    retained: RetainedReplay | None = None


def call_in_transaction(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    name: str,
    payload: Mapping[str, object],
    *,
    registry: OperationRegistry | None = None,
) -> BaseModel:
    authorized = (REGISTRY if registry is None else registry).authorize(ctx, name)
    if isinstance(authorized, Refusal):
        raise OperationRefused(authorized.state, authorized.detail)
    operation = authorized.operation
    declaration = operation.declaration
    if declaration.long_running or declaration.safety_class not in {
        SafetyClass.READ,
        SafetyClass.MUTATE,
        SafetyClass.DRAFT,
    }:
        raise OperationRefused(
            "transaction_call_forbidden",
            "operation requires its own execution boundary",
        )
    audit = _audit_write(operation, request_digest=_request_digest(payload))
    if audit is None and declaration.safety_class is not SafetyClass.READ:
        raise OperationRefused("audit_sink_missing", "operation audit sink unavailable")
    try:
        model = declaration.input_model.model_validate(
            payload, context=validation_context(ctx, uow)
        )
    except ValidationError:
        raise OperationRefused("input_invalid", "invalid service input") from None
    from rheo_core.modules.readiness import require_ready

    require_ready(uow.connection, name.split(".")[0])
    output = operation.handler(ctx, uow, model)
    if not isinstance(output, declaration.output):
        raise OperationRefused("output_invalid", "invalid service output")
    if audit is not None:
        record_operation_audit(
            ctx,
            uow,
            operation,
            request_digest=_request_digest(payload),
            model_input=model,
            outcome=AUDIT_SUCCEEDED,
            operation_id=None,
        )
    return output


def stage_external_in_transaction(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    name: str,
    payload: Mapping[str, object] | BaseModel,
    *,
    now: datetime,
    source_ref: str | None = None,
    registry: OperationRegistry | None = None,
    destinations: "DestinationRegistry | None" = None,
) -> StagedExternalAction:
    """Atomically reserve and hold one KEYED EXTERNAL/FINANCIAL request.

    The caller owns the transaction. This function never commits and never invokes a
    destination callback or operation handler.
    """
    from rheo_core.approvals import records as approval_records
    from rheo_core.approvals.binding import (
        executable_payload,
        payload_bytes,
        payload_digest,
    )
    from rheo_core.audit import AUDIT_REFUSED
    from rheo_core.modules.execution import DESTINATIONS
    from rheo_core.modules.readiness import require_ready
    from rheo_core.operations.external_actions import (
        create_pending,
        has_action_for_key,
    )
    from rheo_core.operations.idempotency import (
        IDEMPOTENCY_KEY_REUSED,
        IdempotencyContractError,
        canonical_request,
        reserve,
    )
    from rheo_core.operations.records import (
        AUDIENCE_NONE,
        mark_approval_required,
        mint,
    )
    from rheo_core.refs import uuid7
    from rheo_core.refs.resolver import Unavailable, resolve_in
    from rheo_core.settings import resolve
    from rheo_core.settings.storage_source import TransactionBoundOverrideSource

    chosen_registry = REGISTRY if registry is None else registry
    destination_registry = DESTINATIONS if destinations is None else destinations
    authorized = chosen_registry.authorize(ctx, name)
    if isinstance(authorized, Refusal):
        raise OperationRefused(authorized.state, authorized.detail)
    operation = authorized.operation
    declaration = operation.declaration
    if declaration.safety_class not in {
        SafetyClass.EXTERNAL,
        SafetyClass.FINANCIAL,
    }:
        raise OperationRefused(
            "external_staging_forbidden", "operation is not EXTERNAL or FINANCIAL"
        )
    if declaration.idempotency is not Idempotency.KEYED or declaration.keyed is None:
        raise OperationRefused(
            "external_staging_forbidden", "external staging requires KEYED idempotency"
        )
    external = declaration.external
    if external is None:
        raise OperationRefused(
            "external_staging_forbidden", "external operation has no ExternalSpec"
        )
    raw = (
        payload.model_dump(mode="python") if isinstance(payload, BaseModel) else payload
    )
    try:
        model_input = declaration.input_model.model_validate(
            raw, context=validation_context(ctx, uow)
        )
    except ValidationError:
        raise OperationRefused("input_invalid", "invalid service input") from None
    destination_ref = getattr(model_input, external.destination_field, None)
    if not isinstance(destination_ref, str) or not destination_ref.strip():
        raise OperationRefused("input_invalid", "destination must be nonblank")
    if destination_ref != external.destination_ref:
        raise OperationRefused(
            "input_invalid", "destination does not match declaration"
        )
    body = executable_payload(model_input)
    settings = resolve(
        workspace_id=ctx.workspace_id,
        account_id=ctx.principal.account_id,
        source=TransactionBoundOverrideSource(
            uow.connection, workspace_id=ctx.workspace_id
        ),
    )
    byte_length = payload_bytes(body)
    limit = settings.get_int("approvals.max_payload_bytes")
    if byte_length > limit:
        raise OperationRefused(
            "approval_payload_too_large",
            f"the validated payload is {byte_length} bytes, over the bound of {limit}",
        )
    try:
        request = canonical_request(declaration, model_input)
    except IdempotencyContractError as exc:
        raise OperationRefused("input_invalid", str(exc)) from exc

    require_ready(uow.connection, operation.module_id)

    retention_days = settings.get_int("work.idempotency_retention_days")
    operation_id = uuid7()
    # Concurrent first reservations of one key serialize on the idempotency_result
    # primary key (operation_name, idempotency_key): a second INSERT ... ON CONFLICT
    # DO NOTHING waits for the first transaction and, once it commits, sees the
    # conflict and takes the replay branch, so only one caller reaches
    # ``create_pending``. One savepoint spans the reservation, the replay and the whole
    # create path through the audit row: any refusal or error raised inside it (the
    # retired-key history, an unavailable destination, a missing audit sink) rolls
    # back every row staging wrote, even when the caller catches the refusal and
    # commits its own transaction. It is released only when staging returns.
    with uow.connection.begin_nested():
        reservation = reserve(
            uow.connection,
            declaration=declaration,
            request=request,
            operation_id=operation_id,
            now=now,
            retained_until=now + timedelta(days=retention_days),
        )
        if not reservation.created:
            return _replay_staged(
                ctx,
                uow,
                operation,
                reservation.row,
                request,
                model_input=model_input,
                digest=payload_digest(body),
            )

        # The primary key stops remembering a key once the retention sweep deletes its
        # envelope, but the settled external action keeps the operation name and key as
        # history. After that boundary another effect needs a new intentional key.
        if has_action_for_key(
            uow.connection, operation_name=declaration.name, idempotency_key=request.key
        ):
            raise OperationRefused(
                IDEMPOTENCY_KEY_REUSED,
                "this idempotency key already staged an external action whose retained "
                "result has expired; another effect requires a new key",
            )
        # Availability gates only a new reservation: a same-identity replay above
        # returns the original identifiers or retained outcome even while the
        # destination is down.
        destination_problem = destination_registry.availability_problem(
            ctx,
            uow,
            destination_ref=destination_ref,
            operation_name=declaration.name,
        )
        if destination_problem is not None:
            raise OperationRefused(
                DESTINATION_GUARD_REFUSED,
                f"DestinationGuard: {destination_problem}",
            )

        subject_ref: RecordRef | None = None
        if (
            declaration.audit is not None
            and declaration.audit.subject_field is not None
        ):
            candidate = getattr(model_input, declaration.audit.subject_field, None)
            if isinstance(candidate, RecordRef):
                subject_ref = candidate
        subject_text = None if subject_ref is None else subject_ref.format()
        subject_revision: int | None = None
        if subject_ref is not None:
            head = resolve_in(subject_ref, ctx, uow)
            subject_revision = None if isinstance(head, Unavailable) else head.revision

        mint(
            uow.connection,
            operation_id=operation_id,
            name=declaration.name,
            safety_class=declaration.safety_class.value,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            entry=ctx.entry.value,
            audience_kind=(
                AUDIENCE_NONE if ctx.audience is None else ctx.audience.kind.value
            ),
            audience_id=None if ctx.audience is None else ctx.audience.id,
            now=now,
        )
        digest = payload_digest(body)
        window_seconds = min(
            settings.get_int("approvals.default_window_seconds"),
            settings.get_int("approvals.max_window_seconds"),
        )
        approval_id = approval_records.create(
            uow.connection,
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            operation_name=declaration.name,
            operation_id=operation_id,
            destination_ref=destination_ref,
            subject_ref=subject_text,
            subject_revision=subject_revision,
            payload_digest=digest,
            body=body,
            byte_length=byte_length,
            purpose=(
                None
                if ctx.principal.bound_purpose is None
                else ctx.principal.bound_purpose.value
            ),
            window_start=now,
            window_end=now + timedelta(seconds=window_seconds),
        )
        if not mark_approval_required(
            uow.connection, operation_id=operation_id, approval_id=approval_id
        ):
            raise RuntimeError("staged operation could not enter approval_required")
        action = create_pending(
            uow.connection,
            operation_id=operation_id,
            approval_id=approval_id,
            operation_name=declaration.name,
            subject_ref=subject_text,
            source_ref=source_ref if source_ref is not None else subject_text,
            destination_ref=destination_ref,
            approval_payload_digest=digest,
            idempotency_key=request.key,
            request_schema_version=declaration.keyed.request_schema_version,
            result_schema_version=declaration.keyed.result_schema_version,
            now=now,
        )
        if not record_operation_audit(
            ctx,
            uow,
            operation,
            request_digest=digest,
            model_input=model_input,
            outcome=AUDIT_REFUSED,
            operation_id=operation_id,
        ):
            raise OperationRefused(
                "audit_sink_missing",
                "external staging requires an installed audit sink",
            )
        return StagedExternalAction(
            action.id, approval_id, operation_id, action.state, True
        )


def _replay_staged(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    operation: RegisteredOperation,
    row: IdempotencyRow,
    request: CanonicalRequest,
    *,
    model_input: BaseModel,
    digest: bytes,
) -> StagedExternalAction:
    """Answer a repeat of a retained keyed request from its envelope and action.

    Writes only the audit row, so the caller's transaction gains nothing else.
    """
    from rheo_core.audit import AUDIT_FAILED, AUDIT_REFUSED, AuditOutcome
    from rheo_core.operations.external_actions import get_for_key
    from rheo_core.operations.idempotency import (
        IDEMPOTENCY_KEY_REUSED,
        IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
        IdempotencySchemaIncompatible,
        replay,
    )

    declaration = operation.declaration
    try:
        retained = replay(row, declaration=declaration, request=request)
    except IdempotencySchemaIncompatible as exc:
        raise OperationRefused(
            IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
            "the retained keyed result is incompatible with this declaration",
        ) from exc
    if retained.error_code == IDEMPOTENCY_KEY_REUSED:
        raise OperationRefused(
            IDEMPOTENCY_KEY_REUSED,
            retained.error_detail or IDEMPOTENCY_KEY_REUSED,
        )
    action = get_for_key(
        uow.connection,
        operation_name=declaration.name,
        idempotency_key=request.key,
    )
    if action is None:
        raise OperationRefused(
            IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
            "the retained external request has no durable action",
        )
    if action.operation_id != retained.operation_id:
        raise OperationRefused(
            IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
            "the retained external request belongs to a different operation",
        )
    # A pending repeat is still a hold, audited as the first stage was; a settled one
    # is audited by its retained outcome, as the synchronous keyed replay does.
    outcome: AuditOutcome
    if retained.state == "pending":
        outcome = AUDIT_REFUSED
    elif retained.state == "succeeded":
        outcome = AUDIT_SUCCEEDED
    elif row.state in {"refused", "cancelled"}:
        outcome = AUDIT_REFUSED
    else:
        outcome = AUDIT_FAILED
    if not record_operation_audit(
        ctx,
        uow,
        operation,
        request_digest=digest,
        model_input=model_input,
        outcome=outcome,
        operation_id=action.operation_id,
    ):
        raise OperationRefused(
            "audit_sink_missing", "external staging requires an installed audit sink"
        )
    return StagedExternalAction(
        action.id,
        action.approval_id,
        action.operation_id,
        action.state,
        False,
        retained,
    )
