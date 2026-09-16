"""The audit seam's contract: an ``AuditSink`` protocol, and a table of installed
sinks **keyed by owning module id**.

**The dispatcher is the one caller.** Issue #45 split run 0v's audit seam and this
file is the half that stayed while the call site — where the call sits in a
transaction, what a missing registration means, whether a registration is mandatory at
all — was 0c2's to build against this contract. Run 0c2 has built it:
``rheo_core.operations.dispatch`` resolves a sink by the operation's owning module and
calls :meth:`AuditSink.record` on every non-``READ`` outcome it can write a row for,
and a module supplying its writer through ``ModuleManifest.audit_sink`` at load time
is now reached. Nothing in *this module* writes a table; ``core.audit_record`` is
written by ``rheo_core.audit.core_sink`` through the repository beside it.

**Why a table and not one global slot.** Every core mutate operation declares an
``AuditSpec`` (``operations/core_ops.py``; enumerating them here was a list that went
stale the first time a run added one, so the claim is left scoped to the module that
owns them). A single installed sink fired on ``declaration.audit is not None`` alone
would be module-blind:
a module's sink would fire on ``core.token.issue`` in a workspace that module was
never installed into, and its INSERT would raise against a schema that is not there.
Ownership is recoverable because the *registry* records it
(``RegisteredOperation.module_id``), so a caller asks which module owns the operation
it is running and then asks for that module's sink. A module supplies a **writer**; it
never supplies the condition. Run 0v's findings note carries the underlying gap — that
an ``AuditSpec`` names what to record but not who records it — as F19.

**``record``'s parameters were the minimum, and 0c2 added the four the real row
needs.** The shipped five were ``record(ctx, uow, *, operation, subject_ref)``, and
this file's own instruction was that "an outcome state, an idempotency key, a
timestamp — anything the real audit record turns out to need — is 0c2's to add against
a real requirement rather than guessed at here". Three of the four additions are
forced by a NOT NULL column of ``core.audit_record`` (``outcome``, ``safety_class``,
``request_digest`` — ``storage/work_tables.py``) and the fourth, ``operation_id``, is
nullable but exists as a foreign key to ``core.operation.id`` precisely to be used.
None was guessed at, and no idempotency key was added because nothing asked for one.

The ``uow`` is still the parameter that matters most: a sink writes in the caller's
transaction, so the caller hands it the **real** ``UnitOfWork`` and not a handler's
sealed view. On the success path that transaction is the operation's own, which is
what makes a mutation and its audit row inseparable; on a non-success path the caller
opens a short transaction of its own and hands that in.
"""

from typing import Final, Protocol
from uuid import UUID

from rheo_contracts import RecordRef, SafetyClass, WorkspaceContext

from rheo_core.audit.records import AuditOutcome
from rheo_core.storage.backend import UnitOfWork


class AuditSinkRefused(Exception):
    """A second, different sink offered under a module id that already has one.

    Raised, never returned, mirroring ``RegistrationRefused``: two sinks for one
    module is a wiring mistake found at startup, not a caller's error. Defined here
    rather than imported from ``rheo_core.operations.refusals`` because that import
    would run ``rheo_core.operations.__init__``, pointing this package at the
    operations package. That was a literal import cycle while the dispatcher still
    called a sink; it is now the seam's direction, which
    ``test_the_audit_module_does_not_import_the_dispatcher`` pins.
    """

    def __init__(self, module_id: str, detail: str) -> None:
        super().__init__(f"{module_id}: {detail}")
        self.module_id = module_id
        self.detail = detail


class AuditSink(Protocol):
    """What a module supplies so its audit row can be written.

    ``safety_class`` is the enum rather than its string so a caller cannot hand over
    a spelling the ``audit_record`` DDL does not hold, and ``outcome`` is the
    three-member :data:`~rheo_core.audit.records.AuditOutcome` for the same reason;
    both are widened to a column value by the sink, not by the caller.
    """

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
    ) -> None: ...


_SINKS: Final[dict[str, AuditSink]] = {}
"""module id -> its sink. A module-level ``Final`` table, mirroring
``OperationRegistry``'s own process-wide shape."""


def install_sink(module_id: str, sink: AuditSink) -> None:
    """Bind ``sink`` to ``module_id``.

    Installing the **identical** sink again is a no-op, mirroring
    ``OperationRegistry.register``'s documented idempotence and for the same reason:
    ``apps/core``'s FastAPI lifespan runs more than once in the same process under
    test, so ``run_startup()`` -> ``load_modules()`` installs the same sink twice. A
    *different* sink under a key that already has one is refused.
    """
    if not isinstance(module_id, str) or not module_id:
        raise ValueError("a sink is installed under a non-empty module id")
    if not callable(getattr(sink, "record", None)):
        raise TypeError("an audit sink must provide record()")
    existing = _SINKS.get(module_id)
    if existing is not None:
        if existing is sink:
            return
        raise AuditSinkRefused(module_id, "a different audit sink is already installed")
    _SINKS[module_id] = sink


def sink_for(module_id: str) -> AuditSink | None:
    """The sink installed for ``module_id``, or ``None`` if it has none.

    ``None`` rather than a no-op object, deliberately: a lookup that answers ``None``
    pre-decides nothing about what a *missing registration* means. Run 0c2 answered
    it, and answered it at the **caller** rather than here — a missing registration is
    fatal at three layers (registration refuses a non-``READ`` declaration with no
    ``AuditSpec``; ``check_audit_paths`` refuses startup; the dispatcher refuses the
    call ``audit_sink_missing``) — so this function still returns ``None`` and a
    fabricated no-op here would still be the one answer that is wrong.
    """
    return _SINKS.get(module_id)


def reset_sinks() -> None:
    """Empty the table. For tests and for a process that reloads its modules."""
    _SINKS.clear()
