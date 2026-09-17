"""The execution guards: the protocol one implements, the three the core attaches,
the rule that decides which of them an operation gets, and the one call that runs them.

``docs/architecture/confirmation-and-safety.md`` § Execution guards names **five**
guards. Three of them are the core's and are written here:

- :class:`ActorPermissionGuard` and :class:`WindowGuard` — attached to every
  ``DESTRUCTIVE``, ``EXTERNAL`` and ``FINANCIAL`` operation, unconditionally.
- :class:`RecordStateGuard` — attached to those same operations **only when the
  declaration's input names a subject**, which in this tree means an
  ``AuditSpec(subject_field=…)`` naming a field that carries a ``RecordRef``.

That is ``docs/architecture/module-contract.md`` § "Registration rules the core
enforces at startup", verbatim in its guard-attachment rule: "Class in
``DESTRUCTIVE``, ``EXTERNAL``, ``FINANCIAL``: the core attaches
``ActorPermissionGuard``, ``WindowGuard``, and, when the input names a
``subject_ref``, ``RecordStateGuard``; the module's ``guards`` list adds domain
guards and may be empty."

**The other two are not the core's and are not written here.**
``ContactPermissionGuard`` is added by Leads and ``DestinationGuard`` by the connector
owning a destination — both are *domain* guards a module supplies through its own
declaration, and neither Leads nor any connector exists in release one. There is
therefore no module to add either, and a core-side class for one would be a guard
nothing could attach.

**"Attached at registration" is implemented as "derived from the declaration."**
:func:`core_guards_for` is a pure function of the declaration, called by
``gate.execute_approved`` on the declaration the registry already resolved. A
declaration is immutable and registered once, so the set it yields at execution is the
set registration would have computed; deriving it keeps ``rheo_core.operations`` free
of any module-level import of this package, which is the import direction
``approvals/gate.py``'s own docstring fixes and which a guard table hung off
``RegisteredOperation`` would have closed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from pydantic import BaseModel
from rheo_contracts import (
    ActorKind,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)

from rheo_core.approvals.records import ApprovalRow
from rheo_core.operations.registry import REGISTRY
from rheo_core.refs.resolver import (
    LIVE,
    RESOLVERS,
    ResolverRegistry,
    Unavailable,
    resolve_in,
)
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    get_access_token,
    get_membership,
    list_access_token_operations,
)
from rheo_core.storage.postgres import get_backend


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

    **``now`` is a parameter of the protocol, never a clock read inside a guard.**
    :class:`WindowGuard`'s whole job is a time comparison; every repository and every
    seam in this tree takes its instant from its caller, so that a test can choose one
    instead of sleeping. It is the **same** instant the binding check was judged
    against one call earlier, so a guard cannot disagree with the binding about when
    "now" is.
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
        now: datetime,
    ) -> str | None: ...


GUARDED_CLASSES: Final = frozenset(
    {SafetyClass.DESTRUCTIVE, SafetyClass.EXTERNAL, SafetyClass.FINANCIAL}
)
"""The three classes the core attaches guards to.

The same three ``dispatch``'s ``_APPROVAL_CLASSES`` holds, spelled again rather than
imported: ``operations/dispatch.py`` and this package import each other only inside
functions (see ``approvals/gate.py``'s docstring), so a module-level import of that
private constant would close the one cycle the whole package is arranged to avoid.
Three enum members are cheaper than that, and they cannot drift silently — a class
moving into or out of the gated set is a change to the ratified class table, which is
code and not configuration."""


def _permission_detail(
    workspace_id: UUID,
    *,
    actor_kind: str | None,
    actor_id: UUID | None,
    roles: frozenset[Role],
    operation_name: str,
    whose: str,
    now: datetime,
) -> str | None:
    """Return why the actor no longer permits the operation, if anything."""
    if actor_kind is None:
        return f"{whose} is not recorded on the approval"
    try:
        kind = ActorKind(actor_kind)
    except ValueError:
        return f"{whose} has the unknown actor kind {actor_kind!r}"
    if kind not in {ActorKind.ACCOUNT, ActorKind.TOKEN}:
        return None
    if actor_id is None:
        return f"{whose} ({kind.value}) has no recorded id"
    with get_backend().control_engine.connect() as connection:
        account_id = actor_id
        if kind is ActorKind.TOKEN:
            token = get_access_token(connection, actor_id)
            if token is None:
                return f"{whose} token {actor_id} no longer exists"
            if token.workspace_id != workspace_id:
                return (
                    f"{whose} token {actor_id} belongs to workspace "
                    f"{token.workspace_id}, not {workspace_id}"
                )
            if token.revoked_at is not None:
                return (
                    f"{whose} token {actor_id} was revoked at "
                    f"{token.revoked_at.isoformat()}"
                )
            if token.expires_at <= now:
                return (
                    f"{whose} token {actor_id} expired at "
                    f"{token.expires_at.isoformat()}"
                )
            if operation_name not in list_access_token_operations(connection, actor_id):
                return f"{whose} token {actor_id} no longer permits {operation_name}"
            account_id = token.account_id
        membership = get_membership(
            connection, account_id=account_id, workspace_id=workspace_id
        )
    if membership is None:
        return (
            f"{whose} ({kind.value} {actor_id}) has no membership in workspace "
            f"{workspace_id} any more, so nothing permits {operation_name}"
        )
    if membership.role not in roles:
        return (
            f"{whose} ({kind.value} {actor_id}) now holds role "
            f"{membership.role.value!r}, which {operation_name} does not permit"
        )
    return None


class ActorPermissionGuard:
    """Refuse when either actor no longer permits this execution.

    The two actors the ratified row names are different principals and both are
    checked: the **gated** actor, whose call was held (``approval.actor_*``), and the
    **approving** actor, who released it (``approval.approved_by_*``). They differ for
    the design's central case — a headless token call approved by a person — and R3
    item 4 wants a revocation of *either* to bite.

    **The roles come from the process-wide registry, not from a field on this guard.**
    Holding them would make the guard a per-declaration object, and the seam this fills
    is a module constant (:data:`CORE_GUARDS`) whose whole point is that a guard is
    added by writing one and naming it there. The lookup is the same one
    ``execute_approved`` made one call earlier, and an operation the registry does not
    know **refuses** rather than passing: a permission guard that cannot find out what
    is permitted has failed, not succeeded.
    """

    @property
    def name(self) -> str:
        return "ActorPermissionGuard"

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: ApprovalRow,
        model_input: BaseModel,
        now: datetime,
    ) -> str | None:
        # Deferred: ``approvals/operations.py`` imports ``approvals/gate.py``, which
        # imports this module, so a module-level import here would close that cycle.
        # It is the approve operation's own declared roles rather than a second copy
        # of ``{owner, member}``, so re-roling that operation cannot leave this guard
        # checking the old pair.
        from rheo_core.approvals.operations import APPROVE_DECLARATION

        operation = REGISTRY.lookup(approval.operation_name)
        if operation is None:
            return (
                f"{approval.operation_name} is not registered in this process, so the "
                "roles it permits cannot be read"
            )
        gated = _permission_detail(
            ctx.workspace_id,
            actor_kind=approval.actor_kind,
            actor_id=approval.actor_id,
            roles=operation.declaration.roles,
            operation_name=approval.operation_name,
            whose="the gated actor",
            now=now,
        )
        if gated is not None:
            return gated
        return _permission_detail(
            ctx.workspace_id,
            actor_kind=approval.approved_by_kind,
            actor_id=approval.approved_by_id,
            roles=APPROVE_DECLARATION.roles,
            operation_name=APPROVE_DECLARATION.name,
            whose="the approving actor",
            now=now,
        )


class WindowGuard:
    """Refuse outside ``window_start`` to ``window_end``, inclusive at both ends.

    **It cannot fire through ``dispatch()`` today, and that is not a defect.** The
    binding tuple carries the window too (``approvals/binding.py``), and
    ``execute_approved`` checks the binding *before* it runs the guards, so an
    execution outside the window has already refused ``invalid_approval`` by the time
    this guard would see it. The two are one rule stated at two layers — the ratified
    document states it in both places — and the layer that fires first is the one a
    caller sees. This class is what makes the rule true for any execution path that
    reaches the guards without the binding check, and it is unit-tested directly
    rather than through a dispatch that cannot reach it.
    """

    @property
    def name(self) -> str:
        return "WindowGuard"

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: ApprovalRow,
        model_input: BaseModel,
        now: datetime,
    ) -> str | None:
        if approval.window_start <= now <= approval.window_end:
            return None
        return (
            f"this execution is outside the approval's window "
            f"{approval.window_start.isoformat()} to "
            f"{approval.window_end.isoformat()}"
        )


@dataclass(frozen=True, slots=True)
class RecordStateGuard:
    """Refuse when the subject is gone or has moved since the approval was created.

    ``subject_field`` is the input-model field carrying the subject's ``RecordRef`` —
    the same ``AuditSpec.subject_field`` ``dispatch``'s ``_subject_ref`` reads for the
    audit row, rather than a second way of naming a subject. It is set by
    :func:`core_guards_for`, which is also the only thing that decides whether this
    guard is attached at all.

    **The reference comes from the verified input, never from
    ``approval.subject_ref``.** That column is a second stored copy the payload digest
    does not cover, so a row tampered there alone would steer this guard at a record
    the operation will not touch — the defect ``approvals/tables.py`` records at the
    column and ``gate.py`` records at the audit write. ``approval.subject_revision`` is
    the deliberate exception and is exactly what this guard compares: it is the
    revision captured at approval time and there is nowhere else to read it from.

    ``resolvers`` exists so the guard's own logic can be exercised against a resolver
    that reports a chosen state. The default is the process-wide table, which is what
    every real attachment gets.
    """

    subject_field: str
    resolvers: ResolverRegistry = RESOLVERS

    @property
    def name(self) -> str:
        return "RecordStateGuard"

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: ApprovalRow,
        model_input: BaseModel,
        now: datetime,
    ) -> str | None:
        subject = getattr(model_input, self.subject_field, None)
        if not isinstance(subject, RecordRef):
            # Attachment is decided from the declaration and the declaration is
            # checked at registration, so this is unreachable for a registered
            # operation. Refused rather than passed: a guard that cannot find the
            # record it guards has not cleared it.
            return (
                f"the input field {self.subject_field!r} carries no record reference, "
                "so the subject's state cannot be rechecked"
            )
        head = resolve_in(subject, ctx, uow, registry=self.resolvers)
        reference = subject.format()
        if isinstance(head, Unavailable):
            return f"{reference} no longer resolves ({head.reason})"
        if head.state != LIVE:
            return f"{reference} is {head.state}"
        if head.revision != approval.subject_revision:
            return (
                f"{reference} is at revision {head.revision}, not the revision "
                f"{approval.subject_revision} that was approved"
            )
        return None


CORE_GUARDS: Final[tuple[Guard, ...]] = (ActorPermissionGuard(), WindowGuard())
"""The guards the core attaches to **every** upper-class operation.

Not the whole attachment: :class:`RecordStateGuard` is conditional and is added by
:func:`core_guards_for`, which reads this tuple by name at call time. A module
constant rather than a registry with an ``install()`` — the only way to add a core
guard is to write one and name it here, in the chunk that writes it, which is a shape
that cannot be armed by accident."""


def core_guards_for(declaration: OperationDeclaration) -> tuple[Guard, ...]:
    """The guards the core attaches to ``declaration``.

    Empty for ``read``, ``draft`` and ``mutate``: those classes never reach an
    approval, so nothing re-runs for them. For the three gated classes, the
    unconditional :data:`CORE_GUARDS` plus :class:`RecordStateGuard` when — and only
    when — the declaration's ``AuditSpec`` names a subject field, which is this tree's
    spelling of "the input names a ``subject_ref``".

    :data:`CORE_GUARDS` is read here rather than closed over, so the set stays one
    module constant with one reader.
    """
    if declaration.safety_class not in GUARDED_CLASSES:
        return ()
    spec = declaration.audit
    if spec is None or spec.subject_field is None:
        return CORE_GUARDS
    return (*CORE_GUARDS, RecordStateGuard(spec.subject_field))


def run_guards(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    approval: ApprovalRow,
    model_input: BaseModel,
    now: datetime,
    guards: tuple[Guard, ...] | None = None,
) -> GuardRefusal | None:
    """Run every guard in order; answer the first refusal, or ``None``.

    First refusal rather than all of them: the effect does not land either way, and a
    guard runs a query of its own, so continuing past a refusal spends connections to
    collect reasons nobody reads.

    **``guards`` defaults to ``None`` and resolves to :data:`CORE_GUARDS` at call
    time, not at definition time.** A default of ``CORE_GUARDS`` binds whatever the
    tuple held when this module was imported into the function object, which would
    make the set unchangeable for the life of the process — including for the tests
    that put a chosen guard in front of the effect and watch it decide. Late binding is
    what pins the *call site* rather than the runner; nothing else about the set
    changes, and there is still no ``install()`` for a module to reach.
    """
    for guard in CORE_GUARDS if guards is None else guards:
        detail = guard.check(
            ctx, uow, approval=approval, model_input=model_input, now=now
        )
        if detail is not None:
            return GuardRefusal(guard.name, detail)
    return None
