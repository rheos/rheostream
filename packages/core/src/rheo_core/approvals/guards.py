"""The execution-guard seam: the protocol a guard implements, the set the core
attaches, and the one call that runs them.

**Empty in this chunk, and explicitly empty.** ``docs/architecture/
confirmation-and-safety.md`` § Execution guards names five guards —
``ActorPermissionGuard``, ``WindowGuard``, ``RecordStateGuard``,
``ContactPermissionGuard``, ``DestinationGuard`` — and none of them is written here.
What is written is the step in the execution path where they run: after the approval
is found valid, before the effect. :data:`CORE_GUARDS` is an empty tuple and
:func:`run_guards` loops over it, so the step exists, is called on every execution,
and passes trivially.

**Why an empty seam rather than nothing at all.** The alternative is a later run
inserting the call site as well as the guards, and the call site is the part that is
easy to put in the wrong place: a guard that runs before the binding check, or after
the handler, is a guard that does not guard. Fixing the position now — with a
demonstrated execution running through it — is what the later run inherits.

**It is not an armed-but-unused mechanism.** Nothing registers into
:data:`CORE_GUARDS` at runtime and there is no registry to register into: the tuple is
a module constant, so the only way to add a guard is to write one and name it there,
in the chunk that writes it. A guard table with an ``install()`` would be the shape
this run rejects elsewhere; a constant empty tuple cannot be armed by accident.
"""

from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import BaseModel
from rheo_contracts import WorkspaceContext

from rheo_core.approvals.records import ApprovalRow
from rheo_core.storage.backend import UnitOfWork


@dataclass(frozen=True, slots=True)
class GuardRefusal:
    """A guard that refused: which guard, and a showable reason.

    The guard's name is carried separately from the detail because the ratified
    behaviour is that a guard failure leaves the approval ``refused`` **with the guard
    named** — a name inside a sentence is not a name a later reader can index on.
    """

    guard: str
    detail: str


class Guard(Protocol):
    """A recheck run at execution time, after approval and before effect.

    ``name`` is the guard's own name as the refusal records it. ``check`` answers
    ``None`` when the guard permits the execution and a detail string when it does
    not; the runner turns the second into a :class:`GuardRefusal` carrying
    :attr:`name`, so no guard has to remember to name itself in its own answer.
    """

    @property
    def name(self) -> str: ...

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: ApprovalRow,
        model_input: BaseModel,
    ) -> str | None: ...


CORE_GUARDS: Final[tuple[Guard, ...]] = ()
"""The guards the core attaches to every upper-class operation.

Empty until the chunk that writes them. A module's own guards arrive through the
operation declaration, which declares no ``guards`` field yet
(``rheo_contracts.manifest``'s ``OperationDeclaration`` says so in its own
docstring), so there is exactly one place a guard can come from today and it is this
tuple."""


def run_guards(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    approval: ApprovalRow,
    model_input: BaseModel,
    guards: tuple[Guard, ...] = CORE_GUARDS,
) -> GuardRefusal | None:
    """Run every guard in order; answer the first refusal, or ``None``.

    First refusal rather than all of them: the effect does not land either way, and a
    guard runs a query of its own, so continuing past a refusal spends connections to
    collect reasons nobody reads.
    """
    for guard in guards:
        detail = guard.check(ctx, uow, approval=approval, model_input=model_input)
        if detail is not None:
            return GuardRefusal(guard.name, detail)
    return None
