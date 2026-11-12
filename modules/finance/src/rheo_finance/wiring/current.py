"""Reminders reach Current in process, under core (ADR-0025).

``CoreCurrent`` is what the wiring binds for ``rheo_finance.sinks.current``: each
reminder session is one core unit of work holding finance's writer lock, a restricted
service context for Current's four item operations and the consumer registry Current's
writes publish through. Finance's own reads and writes run in ``owner`` blocks as
``finance_owner`` on that transaction; each Current operation runs through
``call_in_transaction`` in a savepoint, at the session role, because neither finance
role may touch Current's or core's tables. Nothing commits the transaction but the
session's ``commit``.

A refusal leaves as a fixed ``CurrentError`` code with nothing chained, and one
content-free log line, because the row annotation that records it is rolled back.
Imported lazily (the tick job, the CLI's context), never by the manifest.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from pydantic import BaseModel
from rheo_contracts import StaleRecord, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.events import ConsumerRegistry
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs import uuid7
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal
from rheo_core.storage.backend import UnitOfWork as CoreUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import text

from rheo_finance import db
from rheo_finance.context import UnitOfWork
from rheo_finance.sinks import current

CURRENT_MODULE: Final = "current"
CURRENT_OPERATIONS: Final = frozenset(
    {
        "current.item.create",
        "current.item.get",
        "current.item.update",
        "current.item.set_status",
    }
)

_LOG = logging.getLogger(__name__)


def _refusal_code(exc: Exception) -> str | None:
    """The fixed code for an exception raised under ``call_in_transaction``, or None
    to let it pass unchanged. First match wins; the refusal's own state and detail
    are never copied, only compared."""
    if isinstance(exc, ModuleNotReady):
        return "current_unavailable"
    if isinstance(exc, OperationRefused):
        if exc.state == "record_stale":
            return "record_stale"
        # Current has no item with that id (ADR-0027: the sink files it again).
        return "not_found" if exc.state == "not_found" else "refused"
    if isinstance(exc, StaleRecord):
        return "record_stale"
    if isinstance(exc, StorageRefusal):
        return "current_unavailable"
    return None


class _Session:
    """One reminder session's transaction (``current.ReminderSession``)."""

    def __init__(
        self,
        core_uow: CoreUnitOfWork,
        view: HandlerUnitOfWork,
        service: WorkspaceContext,
    ) -> None:
        self._core = core_uow
        self._connection = core_uow.connection
        self._view = view
        self._service = service
        self._in_block = False

    @contextmanager
    def owner(self) -> Iterator[UnitOfWork]:
        """A block as ``finance_owner`` on the session's transaction; not nestable."""
        if self._in_block:
            raise RuntimeError("reminder owner blocks do not nest")
        self._in_block = True
        try:
            with db.acting_as(self._connection, db.OWNER_ROLE):
                yield UnitOfWork(self._connection)
        finally:
            self._in_block = False

    @contextmanager
    def key(self) -> Iterator[None]:
        """One reminder key's savepoint. Taken at the session role, outside any owner
        block, so rolling back to it also undoes any role set after it. Finance's
        writer lock was taken before it and stays held; Current's lifecycle lock,
        if a rolled-back key released it, is taken again by the next key's first
        write, so the order stays writer lock, then lifecycle lock."""
        if self._in_block:
            raise RuntimeError("a reminder key savepoint is taken outside owner blocks")
        with self._connection.begin_nested():
            yield

    def call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        if operation not in current.OPERATIONS:
            raise current.CurrentError("protocol_error")
        name = f"current.item.{operation}"
        code: str | None = None
        output: BaseModel | None = None
        try:
            # A savepoint: a refusal undoes the call's writes and its RESET ROLE, and
            # the transaction stays usable for the one stale retry.
            with self._connection.begin_nested():
                self._connection.execute(text("RESET ROLE"))
                output = call_in_transaction(self._service, self._view, name, inputs)
        except Exception as exc:
            code = _refusal_code(exc)
            if code is None:
                raise
        if code is not None or output is None:
            code = code or "protocol_error"
            _LOG.info(
                "finance_current_call_refused operation=%s code=%s", operation, code
            )
            # Outside the except block: nothing of the refusal is kept as context.
            raise current.CurrentError(code, logged=True) from None
        if self._in_block:
            self._connection.execute(text(f"SET LOCAL ROLE {db.OWNER_ROLE}"))
        dumped: dict[str, object] = output.model_dump(mode="json")
        return dumped

    def commit(self) -> None:
        self._core.commit()


class CoreCurrent:
    """``current.InProcessCurrent`` over core, for ``core_ctx``'s workspace.

    ``consumers`` is the worker's registry; without one (the CLI) a session refuses
    ``current_unavailable`` before any call, since Current's create would refuse."""

    def __init__(
        self, core_ctx: WorkspaceContext, *, consumers: ConsumerRegistry | None
    ) -> None:
        self._core_ctx = core_ctx
        self._consumers = consumers

    @contextmanager
    def session(self) -> Iterator[current.ReminderSession]:
        with open_unit_of_work(self._core_ctx) as core_uow:
            service = context_for_event_consumer(
                self._core_ctx.workspace_id,
                core_uow,
                request_id=uuid7(),
                operation_names=CURRENT_OPERATIONS,
            )
            if isinstance(service, Refusal):
                raise current.CurrentError("current_unavailable")
            if CURRENT_MODULE not in service.enabled_modules:
                raise current.CurrentError("not_configured")
            if self._consumers is None:
                raise current.CurrentError("current_unavailable")
            view = HandlerUnitOfWork(core_uow, consumers=self._consumers)
            # Finance's writer lock once, held to the commit or the rollback; then
            # Current's lifecycle lock on its first write. No path takes them the
            # other way round.
            with db.writer_lock(core_uow.connection):
                yield _Session(core_uow, view, service)


@contextmanager
def bind_reminders(
    core_ctx: WorkspaceContext, consumers: ConsumerRegistry | None
) -> Iterator[None]:
    """Reminders go to Current in process for the body of the ``with``."""
    token = current.use_in_process(CoreCurrent(core_ctx, consumers=consumers))
    try:
        yield
    finally:
        current.release_in_process(token)
