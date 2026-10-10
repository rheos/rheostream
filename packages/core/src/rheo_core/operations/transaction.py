"""Synchronous registered calls inside an existing transaction.

Consumers receive only their subscription's explicit operation names. This path
never opens or commits a transaction and cannot execute approval-gated or deferred
operations. A failure propagates to the caller, rolling back the entire delivery.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError
from rheo_contracts import SafetyClass, WorkspaceContext

from rheo_core.audit import AUDIT_SUCCEEDED
from rheo_core.boundary.context import Refusal
from rheo_core.operations.dispatch import (
    _audit_write,
    _request_digest,
    record_operation_audit,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import UnitOfWork


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
        model = declaration.input_model.model_validate(payload)
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
