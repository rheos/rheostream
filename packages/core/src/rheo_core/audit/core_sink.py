"""The core's own ``AuditSink``: one ``core.audit_record`` row per call, in the
caller's transaction.

**Why the core needs a sink of its own at all.** A module supplies its writer through
``ModuleManifest.audit_sink`` and ``modules/loader.py`` installs it at load time. The
core is a module (``module_id = "core"``) with **no manifest**, so nothing loads one
for it; ``operations/core_ops.py``'s ``register_core_operations()`` installs
:data:`CORE_AUDIT_SINK` instead, beside the declarations it is the sink for. The test
harness does the same for ``harness``.

**One object, installed under more than one module id, and that is safe.** The sink
holds no state and reads no module identity: everything it writes comes from the
``ctx`` and the keywords it is handed, and the row lands in the workspace database the
caller's ``UnitOfWork`` is already routed to. ``install_sink`` is idempotent for the
*identical* object, which is what lets the core's registrar and a test fixture both
install it without either refusing the other.

**The clock is read here and nowhere below.** ``record`` has no ``occurred_at``
parameter — the protocol's six keywords are what the dispatcher can supply, and an
instant is not one of them — so wall time enters at this line, the way it already does
in ``operations/dispatch.py``'s mint and in ``storage/work_index.py``.
``insert_audit_record`` takes the instant explicitly and reads no clock of its own, so
the repository stays testable by choosing one.

Imports ``rheo_contracts``, the repository beside it, and ``storage.backend`` for the
``UnitOfWork`` in its own signature. **Nothing from ``rheo_core.operations``**, which
would point this package back at the one that calls it and close the cycle
``tests/test_audit_sink.py`` pins the direction of.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, SafetyClass, WorkspaceContext

from rheo_core.audit.records import AuditOutcome, insert_audit_record
from rheo_core.storage.backend import UnitOfWork


class CoreAuditSink:
    """Writes one ``core.audit_record`` row through the caller's unit of work."""

    __slots__ = ()

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        safety_class: SafetyClass,
        subject_ref: RecordRef | None,
        request_digest: bytes,
        outcome: AuditOutcome,
        operation_id: UUID | None,
    ) -> None:
        """One row, in ``uow``'s transaction, committed by whoever owns it.

        **No commit here, deliberately.** On the success path ``uow`` is the
        operation's own transaction and committing it would end the operation
        underneath its dispatcher; on a non-success path the dispatcher opened a short
        transaction for this row alone and commits it itself. Either way the decision
        about when the row becomes durable belongs to the caller that knows what else
        is in the transaction.

        **The workspace is not a column and does not need to be** (AC 23). Every
        workspace's records live in a distinct database, so ``uow`` — routed from the
        context by ``route()`` — *is* the row's workspace, and a row cannot name the
        wrong one because there is no name to get wrong.
        """
        insert_audit_record(
            uow.connection,
            occurred_at=datetime.now(UTC),
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            entry=ctx.entry.value,
            operation_name=operation,
            safety_class=safety_class.value,
            operation_id=operation_id,
            subject_ref=None if subject_ref is None else subject_ref.format(),
            request_digest=request_digest,
            outcome=outcome,
        )


CORE_AUDIT_SINK: Final = CoreAuditSink()
"""The module-level singleton every core registration installs.

One instance rather than one per registration, because ``install_sink`` refuses a
*different* sink under a key that already has one: two instances would make the second
``register_core_operations()`` call in a process — which ``apps/core``'s lifespan
performs — an ``AuditSinkRefused`` rather than the no-op it is documented to be."""
