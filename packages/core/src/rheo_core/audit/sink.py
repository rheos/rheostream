"""The audit seam's contract: an ``AuditSink`` protocol, and a table of installed
sinks **keyed by owning module id**.

**Nothing calls a sink.** Issue #45 split run 0v's audit seam and this file is the
half that stays: the protocol and the registry are substrate, and the caller — where
the call sits in a transaction, what a missing registration means, whether a
registration is mandatory at all — is 0c2's to build against this contract. A module
supplies its writer through ``ModuleManifest.audit_sink`` at load time, and until 0c2
that writer is never reached. Nothing here writes a core table either; the audit
table itself is 0c's.

**Why a table and not one global slot.** Every core mutate operation already declares
``AuditSpec(subject_field=None)`` (``operations/core_ops.py``: ``core.settings.set``,
``core.settings.set_member``, ``core.token.issue``, ``core.token.revoke``). A single
installed sink fired on ``declaration.audit is not None`` alone would be module-blind:
a module's sink would fire on ``core.token.issue`` in a workspace that module was
never installed into, and its INSERT would raise against a schema that is not there.
Ownership is recoverable because the *registry* records it
(``RegisteredOperation.module_id``), so a caller asks which module owns the operation
it is running and then asks for that module's sink. A module supplies a **writer**; it
never supplies the condition. Run 0v's findings note carries the underlying gap — that
an ``AuditSpec`` names what to record but not who records it — as F19.

**``record``'s parameters are the minimum, not a finished shape.** ``record(ctx, uow,
*, operation, subject_ref)`` carries who and where, what ran, and what it was about.
An outcome state, an idempotency key, a timestamp — anything the real audit record
turns out to need — is 0c2's to add against a real requirement rather than guessed at
here. The ``uow`` is the parameter that matters most: a sink writes in the caller's
transaction, so the caller hands it the **real** ``UnitOfWork`` and not a handler's
sealed view.
"""

from typing import Final, Protocol

from rheo_contracts import RecordRef, WorkspaceContext

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
    """What a module supplies so its audit row can be written."""

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
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
    pre-decides nothing about what a *missing registration* means. Whether that is a
    silent skip, a refusal, or a startup-time requirement is one of the cases 0c2 is
    scored on, and returning a fabricated no-op here would answer it for them.
    """
    return _SINKS.get(module_id)


def reset_sinks() -> None:
    """Empty the table. For tests and for a process that reloads its modules."""
    _SINKS.clear()
