"""``SpikeAuditSink``: the writer the spike hands the dispatcher.

**It writes unconditionally.** There is no ``if operation.startswith("spike.")``
here, and no operation-name check of any kind, because the scoping is not this
module's to do: ``dispatch()`` resolves the sink with
``sink_for(operation.module_id)``, so this object is only ever handed a ``spike.*``
operation. Writing that guard anyway would move the decision into module code — the
very thing FR 4 exists to disprove — and would weaken AC 11's scan from "the module
never decides" to "the module decides correctly today". **If this sink is ever handed
a ``core.*`` operation, the defect is in the dispatcher's hook, not in this file.**

The sink is handed the **real** ``UnitOfWork``, not the handler's sealed view, and it
writes on that connection inside the transaction the dispatcher is about to commit.
That is what AC 12(b) pins: a failure raised after this row is written and before the
commit must take the row with it.

``spike.audit_probe`` is not ``core.audit_record``. The real audit row, the operation
record and the outbox are run 0c's scored work and are not built here.
"""

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.storage.backend import UnitOfWork

from rheo_spike.records import write_audit_probe


class SpikeAuditSink:
    """Writes one ``spike.audit_probe`` row per audited ``spike.*`` dispatch."""

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        write_audit_probe(
            uow.connection,
            operation=operation,
            subject_ref=None if subject_ref is None else subject_ref.format(),
            actor_kind=ctx.actor.kind.value,
            actor_id=ctx.actor.id,
            request_id=ctx.request_id,
        )


SINK = SpikeAuditSink()
"""One instance, referenced by the manifest.

``install_sink`` is idempotent on *identity*, and ``apps/core``'s FastAPI lifespan
runs more than once in a single process under test, so the manifest must offer the
same object every time rather than constructing one per load.
"""
