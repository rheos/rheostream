"""The tick as core's job ``finance.tick``.

The handler opens no transaction of its own on the job's unit of work, where core
writes the job row after it returns: the tick commits in short sessions of its own
(``jobs.tick`` dispatches each step on a fresh core session), and the reminder sync in
one session of its own, bound here to Current in process with the worker's consumer
registry (``wiring.current``). It checks the cancellation token before each due job,
before the prune and while a model call waits.

The worker stores ``str(exc)`` of whatever a handler raises, so every failure leaves
here as ``FinanceJobFailed`` carrying a fixed code and nothing chained; cancellation,
a lost lease and a module that is not ready pass through unchanged, from inside a due
job too (the tick is handed the same classes). The ledger is
imported inside the handler, so loading the manifest (``rheo web compose`` loads every
discovered one) does not import it.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_scheduled_job
from rheo_core.events import ConsumerRegistry
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.work.cancellation import CancellationToken, JobCancelled, JobLeaseLost

TICK_JOB_KIND: Final = "finance.tick"
TICK_MAX_ATTEMPTS: Final = 3

PASS_THROUGH: Final[tuple[type[BaseException], ...]] = (
    JobCancelled,
    JobLeaseLost,
    ModuleNotReady,
)
GENERIC: Final = "finance_tick_failed"


class TickPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


class FinanceJobFailed(RuntimeError):
    """A tick failure. The message is a fixed code and nothing else."""


def failure_codes() -> tuple[tuple[type[BaseException], str], ...]:
    """Each failure class with its code, subclasses before their bases."""
    from rheo_core.storage.backend import StorageRefusal
    from sqlalchemy.exc import SQLAlchemyError

    from rheo_finance.db import LayoutRefused, WriterBusy
    from rheo_finance.jobs.tick import SchemaBehind, SchemaUnknown
    from rheo_finance.operations import OperationError
    from rheo_finance.settings import SettingsError

    return (
        (SchemaBehind, "finance_schema_behind"),
        (SchemaUnknown, "finance_schema_unknown"),
        (WriterBusy, "finance_writer_busy"),
        (SettingsError, "finance_settings_invalid"),
        (LayoutRefused, "finance_layout_invalid"),
        (OperationError, "finance_operation_refused"),
        (StorageRefusal, "finance_workspace_unavailable"),
        (SQLAlchemyError, "finance_storage_failed"),
    )


def failure_code(exc: BaseException) -> str:
    """The code of the first entry whose class ``exc`` is, else ``GENERIC``."""
    lineage = type(exc).__mro__
    for kind, code in failure_codes():
        if kind in lineage:
            return code
    return GENERIC


def _run(
    workspace_id: UUID,
    token: CancellationToken,
    consumers: ConsumerRegistry | None,
) -> None:
    core_ctx = context_for_scheduled_job(workspace_id)
    if isinstance(core_ctx, Refusal):
        return
    from rheo_finance import db
    from rheo_finance.contracts import Role
    from rheo_finance.jobs.tick import tick
    from rheo_finance.wiring.core import context_over_core, layout_for
    from rheo_finance.wiring.current import bind_reminders
    from rheo_finance.wiring.model import runtime_model_client

    # One bound method, handed to the model client, the context and the tick alike.
    checkpoint = token.checkpoint
    bound = db.use_layout(db.check_layout(layout_for(core_ctx)))
    try:
        # Reminders go to Current in process, through the worker's registry, which
        # Current's writes publish through.
        with bind_reminders(core_ctx, consumers):
            ctx = context_over_core(
                core_ctx,
                Role.SERVICE,
                model_client=runtime_model_client(core_ctx, heartbeat=checkpoint),
                progress=checkpoint,
            )
            # The tick's per-job handler records any other error and goes on; these
            # stop the job, so no further job runs after a cancel or under another
            # lease.
            tick(ctx, checkpoint=checkpoint, pass_through=PASS_THROUGH)
    finally:
        db.release_layout(bound)


def finance_tick_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """One tick of the workspace's due finance jobs (module docstring)."""
    if not isinstance(payload, TickPayload):
        raise TypeError(f"expected a TickPayload, got {type(payload).__name__}")
    token.checkpoint()
    code: str | None = None
    try:
        _run(payload.workspace_id, token, uow.consumers)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        code = failure_code(exc)
    if code is not None:
        raise FinanceJobFailed(code) from None
