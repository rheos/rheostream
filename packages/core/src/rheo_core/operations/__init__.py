"""The service registry: operation registration, authorization and dispatch.

- ``registry.py`` — ``OperationRegistry.register(decl, handler, *, origin)`` and
  ``authorize(ctx, name) -> Authorized | Refusal``; the process-wide ``REGISTRY``.
- ``dispatch.py`` — ``dispatch(ctx, name, payload) -> OperationOutcome``: the
  ``context_required`` check, ``authorize``, input validation, one unit of work
  through ``route(ctx)``, the handler under a sealed ``HandlerUnitOfWork``, the
  owning module's audit sink, commit. No audit table, no operation record, no
  outbox (run 0c's).
- ``refusals.py`` — the state names, ``RegistrationRefused``, ``OperationRefused``,
  and the re-exported ``handler_may_not_commit`` (declared in ``storage.backend``,
  beside the ``HandlerUnitOfWork`` that raises it).
- ``core_ops.py`` — ``core.workspace.status``, ``core.settings.set``,
  ``core.settings.set_member``, ``core.work.failures`` (whose models and
  handler live in ``rheo_core.work.operations``, beside the repository they
  read), and ``register_core_operations()``.
- ``openapi.py`` — ``build_document(registry)``: the OpenAPI 3.1 document the web
  tier's generated client is built from, one path per registered operation. Pure
  pydantic, no FastAPI, so ``apps/cli`` can emit it without ``apps/core``.
"""

from rheo_core.operations.core_ops import (
    SETTINGS_SET,
    SETTINGS_SET_MEMBER,
    WORK_FAILURES,
    WORKSPACE_STATUS,
    SettingWrite,
    SettingWritten,
    WorkspaceStatus,
    WorkspaceStatusInput,
    register_core_operations,
)
from rheo_core.operations.dispatch import (
    OperationError,
    OperationOutcome,
    dispatch,
)
from rheo_core.operations.openapi import (
    GENERATED_BANNER,
    OPERATION_PATH_PREFIX,
    build_document,
    operation_path,
)
from rheo_core.operations.refusals import (
    AUTHORIZATION_STATES,
    FAILED,
    HANDLER_FAILED,
    HANDLER_MAY_NOT_COMMIT,
    INPUT_INVALID,
    MODULE_DISABLED,
    OPERATION_NOT_PERMITTED,
    OPERATION_UNKNOWN,
    OUTPUT_INVALID,
    ROLE_NOT_PERMITTED,
    SUCCEEDED,
    OperationRefused,
    RegistrationRefused,
)
from rheo_core.operations.registry import (
    CORE_MODULE_ID,
    HARNESS_MODULE_ID,
    REGISTRY,
    Authorized,
    Handler,
    OperationRegistry,
    RegisteredOperation,
    authorize,
    check_origin,
    module_id_for_origin,
    register,
    reserved_input_fields,
)

__all__ = [
    "AUTHORIZATION_STATES",
    "CORE_MODULE_ID",
    "FAILED",
    "HANDLER_FAILED",
    "HANDLER_MAY_NOT_COMMIT",
    "GENERATED_BANNER",
    "HARNESS_MODULE_ID",
    "INPUT_INVALID",
    "MODULE_DISABLED",
    "OPERATION_NOT_PERMITTED",
    "OPERATION_PATH_PREFIX",
    "OPERATION_UNKNOWN",
    "OUTPUT_INVALID",
    "REGISTRY",
    "ROLE_NOT_PERMITTED",
    "SETTINGS_SET",
    "SETTINGS_SET_MEMBER",
    "SUCCEEDED",
    "WORKSPACE_STATUS",
    "WORK_FAILURES",
    "Authorized",
    "Handler",
    "OperationError",
    "OperationOutcome",
    "OperationRefused",
    "OperationRegistry",
    "RegisteredOperation",
    "RegistrationRefused",
    "SettingWrite",
    "SettingWritten",
    "WorkspaceStatus",
    "WorkspaceStatusInput",
    "authorize",
    "build_document",
    "check_origin",
    "dispatch",
    "module_id_for_origin",
    "operation_path",
    "register",
    "register_core_operations",
    "reserved_input_fields",
]
