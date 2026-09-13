"""The audit seam: an ``AuditSink`` protocol, a no-op default, and a table of
installed sinks **keyed by owning module id**.

``dispatch()`` calls ``sink_for(operation.module_id).record(...)`` when, and only
when, the operation's declaration carries an ``AuditSpec``. Everything else about the
audit row — the table, its columns, the operation record, the outbox — is run 0c's,
and nothing here writes a core table: ``NullAuditSink`` is the production default and
does exactly what ``main`` does today, which is nothing.

**Why a table and not one global slot.** Every core mutate operation already declares
``AuditSpec(subject_field=None)`` (``operations/core_ops.py``: ``core.settings.set``,
``core.settings.set_member``, ``core.token.issue``, ``core.token.revoke``). A single
installed sink fired on ``declaration.audit is not None`` alone is module-blind: a
module's sink would fire on ``core.token.issue`` in a workspace that module was never
installed into, its INSERT would raise against a schema that is not there, and
``dispatch()``'s ``except Exception`` would roll back and return ``failed`` — turning
a shipped, un-editable test red. Ownership is recoverable because the *registry*
records it (``RegisteredOperation.module_id``), so the dispatcher asks which module
owns the operation it is about to run and then asks for that module's sink. A module
supplies a **writer**; it never supplies the condition. Run 0v's findings note carries
the underlying gap — that an ``AuditSpec`` names what to record but not who records it
— as F19.

**The signature is exactly what the one call site uses.** ``record(ctx, uow, *,
operation, subject_ref)`` — two keyword parameters, no more. ``outcome_state`` has one
possible value at the only call site (the operation is about to succeed, or the sink is
never reached), and the ``AuditSpec`` itself is redundant with the ``subject_ref``
``dispatch()`` has already resolved from it. 0c2 must inherit no guessed shape: if the
real audit record needs an outcome, 0c2 adds the parameter against a real requirement
rather than finding it pre-decided here.

The sink is handed the **real** ``UnitOfWork``, not the handler's sealed view: the sink
is core, not a module, and it writes in the same transaction the dispatcher is about to
commit.
"""

from typing import Final, Protocol

from rheo_contracts import RecordRef, WorkspaceContext

from rheo_core.storage.backend import UnitOfWork


class AuditSinkRefused(Exception):
    """A second, different sink offered under a module id that already has one.

    Raised, never returned, mirroring ``RegistrationRefused``: two sinks for one
    module is a wiring mistake found at startup, not a caller's error. Defined here
    rather than imported from ``rheo_core.operations.refusals`` because that import
    would run ``rheo_core.operations.__init__``, which imports ``dispatch``, which
    imports this module — a real cycle.
    """

    def __init__(self, module_id: str, detail: str) -> None:
        super().__init__(f"{module_id}: {detail}")
        self.module_id = module_id
        self.detail = detail


class AuditSink(Protocol):
    """What a module supplies so the dispatcher can write its audit row."""

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None: ...


class NullAuditSink:
    """The production default: what ``main`` does today, which is nothing."""

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        return None


NULL_SINK: Final[AuditSink] = NullAuditSink()
"""One shared instance, so ``sink_for`` returns the same object every time."""

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


def sink_for(module_id: str) -> AuditSink:
    """The sink installed for ``module_id``, or :data:`NULL_SINK`.

    ``core`` never has one installed in this run, so every ``core.*`` dispatch
    resolves to the no-op and behaves exactly as ``main`` does today.
    """
    return _SINKS.get(module_id, NULL_SINK)


def reset_sinks() -> None:
    """Empty the table. For tests and for a process that reloads its modules."""
    _SINKS.clear()
