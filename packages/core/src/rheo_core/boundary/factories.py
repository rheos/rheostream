"""The only place a ``WorkspaceContext`` is constructed.

Every factory returns ``WorkspaceContext | Refusal``. The repository-wide AST scan in
``tests/test_boundary.py`` asserts that the class is constructed in no file outside
this package, tests included, so the functions here are the only way to obtain a
context: the operator and harness factories, the session and token factories 0b2 added
beside them, ``context_from_operation`` for a caller rebuilt from stored provenance,
and 1a1's ``context_for_approved_execution``, which is that last one asked on an
approval's behalf.

They share one tail: the ``control.workspace`` row must be ``active`` (otherwise
``workspace_unavailable`` with the row's state as the detail), and ``enabled_modules``
is loaded from that workspace's ``core.module_state`` rows. That tail is B16's factory
clause for this run.

**Every factory also states the context's ``principal``, and none takes one from its
caller.** It carries the account the boundary actually verified and the purpose that
boundary is bound to -- null for everything but a purpose-carrying token and a rebuild
of a purpose-bound stored caller. That is what makes ``ctx.principal`` a server fact
rather than a request field; ``RESERVED_INPUT_FIELDS`` closes the other end.
"""

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from rheo_contracts import (
    ALL_OPERATIONS,
    Actor,
    ActorKind,
    AllOperations,
    Audience,
    AudienceKind,
    AuthenticatedPrincipal,
    ContextPurpose,
    Entry,
    Role,
    WorkspaceContext,
)

from rheo_core.boundary.context import (
    MEMBERSHIP_MISSING,
    OPERATION_PROVENANCE_MISSING,
    PROFILE_REQUIRED,
    SESSION_EXPIRED,
    SESSION_MISSING,
    SESSION_REVOKED,
    WORKSPACE_MISSING_DETAIL,
    WORKSPACE_UNAVAILABLE,
    WORKSPACE_UNSELECTED,
    Refusal,
)
from rheo_core.refs import uuid7
from rheo_core.settings import current_profile
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    get_access_token,
    get_membership,
    get_session_by_secret_hash,
    get_workspace,
    list_access_token_operations,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import PostgresBackend, get_backend
from rheo_core.storage.repositories import list_module_states

# A submodule import, not ``from rheo_core.tokens import resolve_token``:
# ``rheo_core.tokens``'s own ``__init__.py`` deliberately does not re-export
# ``presentation`` (see that package's docstring) precisely so this edge -- this
# file, reached from ``rheo_core.boundary``'s own ``__init__.py``, needing
# ``rheo_core.tokens.presentation``, which itself needs this file's sibling
# ``rheo_core.boundary.context`` -- resolves regardless of which package a
# caller happens to import first.
from rheo_core.tokens.presentation import resolve_token

if TYPE_CHECKING:
    # Type-only, never at runtime: ``rheo_core.approvals`` and ``rheo_core.operations``
    # both reach this package, so a runtime import either way would close a cycle.
    # ``context_for_approved_execution`` only reads attributes off the rows it is
    # handed or fetches, so the names are needed for signatures and nothing else.
    from rheo_core.approvals.records import ApprovalRow
    from rheo_core.operations.records import OperationContextProvenance

# The ``core.module_state.state`` value that makes a module enabled; one of
# ``rheo_core.storage.core_tables.MODULE_STATES``.
_MODULE_ENABLED = "enabled"


def _purpose_mismatch(detail: str) -> Refusal:
    """``Refusal(PURPOSE_MISMATCH, detail)``, with the constant read at call time.

    ``PURPOSE_MISMATCH`` lives beside ``RUNTIME_ACTOR_REQUIRED`` in
    ``rheo_core.runtime.operations``, which imports this module at module level, so a
    module-level import back would close a real cycle -- the same reason
    :func:`context_from_operation` spells ``"runtime_actor_required"`` as a literal.
    Deferred here instead of spelled twice, so there is one definition of the state
    name; it runs only on a refusal, where a module import is not the cost.
    """
    # Deferred: see this function's docstring.
    from rheo_core.runtime.operations import PURPOSE_MISMATCH

    return Refusal(PURPOSE_MISMATCH, detail)


def _parse_purpose(purpose: str | None) -> ContextPurpose | None | Refusal:
    """The supplied job/approval purpose as the binding a rebuilt context carries.

    ``None`` stays ``None`` -- an original caller that was never purpose-bound (a web
    session, an unbound token, or an approval minted before this column was written)
    rebuilds as unbound, and the token-side agreement check below is skipped for it. A
    string outside the closed ``ContextPurpose`` set refuses rather than degrading to
    unbound, which would turn a job whose purpose this process cannot read into one
    with no memory-purpose gate at all.
    """
    if purpose is None:
        return None
    try:
        return ContextPurpose(purpose)
    except ValueError:
        return _purpose_mismatch(f"{purpose!r} is not a context purpose")


def _active_workspace_modules(
    backend: PostgresBackend, workspace_id: UUID
) -> frozenset[str] | Refusal:
    """The shared factory tail: the row must be ``active``, then the enabled set.

    The registry row is read now, on every call, exactly as ``storage.routing``
    reads it: a context is only ever built over a workspace that is servable at
    that moment.
    """
    with backend.control_engine.connect() as connection:
        row = get_workspace(connection, workspace_id)
    if row is None:
        return Refusal(WORKSPACE_UNAVAILABLE, WORKSPACE_MISSING_DETAIL)
    if row.state is not WorkspaceState.ACTIVE:
        return Refusal(WORKSPACE_UNAVAILABLE, row.state.value)
    engine = backend.pools.engine_for(row.database_name, pin=True)
    with UnitOfWork(engine, row.database_name, pool=backend.pools) as uow:
        states = list_module_states(uow.connection)
    return frozenset(
        state.module_id for state in states if state.state == _MODULE_ENABLED
    )


def context_for_operator(
    workspace_id: UUID, entry: Entry | str = Entry.CLI
) -> WorkspaceContext | Refusal:
    """The operator's context over ``workspace_id`` (the ``rheo`` command, C4).

    Actor ``operator`` with no id, role ``operator``, the full operation set, and
    **no audience**. ``docs/architecture/overview.md:74`` (the ``audience`` row of the
    context table) enumerates exactly four audience kinds, none of them an operator,
    and records that a context may carry no audience at all: of the ``job`` kind it
    says "a scheduled job has none". So every consumer already handles
    ``audience is None``, and that is the value here. It is deliberately not
    ``Audience(session, None)``: 0b2's ``context_from_session`` sets
    ``Audience(session, account_id)``, so a session-kind audience with a null id
    would leave an operator context and a real web-session context differing only by
    that null, and any later code branching on ``audience.kind == session`` would
    treat operator output as session output. Widening the ratified enum was the other
    option and is refused for the same reason ``Entry`` was not widened for tests.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    enabled = _active_workspace_modules(get_backend(), workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=workspace_id,
        actor=Actor(kind=ActorKind.OPERATOR, id=None),
        role=Role.OPERATOR,
        entry=Entry(entry),
        audience=None,
        operation_set=ALL_OPERATIONS,
        enabled_modules=enabled,
        request_id=uuid7(),
        # An operator is not an account and carries no bound purpose: the `rheo`
        # command authenticates against the host, not against `control.account`.
        principal=AuthenticatedPrincipal(account_id=None, bound_purpose=None),
    )


MEMORY_EXPIRY_OPERATIONS: Final = frozenset({"core.record.delete"})
"""The one operation a scheduled retention expiry is allowed to reach.

Spelled rather than imported from ``rheo_core.deletion.operations``, which reaches
``rheo_core.operations`` and back into this package -- the same cycle
:func:`_purpose_mismatch` describes, and the same answer ``context_from_operation``
already gives for ``"runtime_actor_required"``.
``tests/postgres/test_memory_retention.py`` pins this set against
``deletion.operations.RECORD_DELETE`` so the copy cannot drift.

A ``frozenset`` and never ``ALL_OPERATIONS``: this is the narrowest operation set any
factory in this file issues, and the narrowness is the whole of what the sweep's
context grants. Compare ``context_for_operator``, which carries the full set.
"""


def context_for_memory_expiry(workspace_id: UUID) -> WorkspaceContext | Refusal:
    """The context one scheduled retention expiry runs under
    (``deletion-export-migration.md`` § Retention).

    Actor ``system`` with no id, role ``service``, entry ``job``, no audience, no
    account and no bound purpose, and :data:`MEMORY_EXPIRY_OPERATIONS` as the whole
    operation set. A sweep is nobody: no person confirmed it, no account owns it, and
    it is running because a timer came round.

    **It is a value and not an authority, and the distinction is load-bearing.**
    Anything in this process may call this function, exactly as anything may call
    :func:`context_for_operator` -- which carries strictly more. What makes a
    scheduled expiry a scheduled expiry is the sealed
    :class:`~rheo_core.work.scheduled_authority.VerifiedScheduledExecution` the worker
    minted onto the handler's own unit of work, re-derived against the live
    transaction before anything is deleted. This context is what that authority *acts
    as* once it has been verified; holding one on its own deletes nothing.

    **``service`` rather than ``operator``, and the consequence is deliberate.** An
    operator role carries the deployment's own hand; a sweep carries the workspace's
    stated retention policy, which is a service of the workspace rather than an
    administrator acting in it. Neither role is one of the two an owner's *erasure*
    authorizer admits, which is why the owned-delete callables are told which
    :class:`~rheo_core.deletion.registry.Disposition` is asking rather than left to
    read a role that means something else on this path.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    enabled = _active_workspace_modules(get_backend(), workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=workspace_id,
        actor=Actor(kind=ActorKind.SYSTEM, id=None),
        role=Role.SERVICE,
        entry=Entry.JOB,
        # None, for the reason ``context_for_operator`` gives at length: ``overview.
        # md``'s context table says of the ``job`` kind that "a scheduled job has
        # none", and this is that job.
        audience=None,
        operation_set=MEMORY_EXPIRY_OPERATIONS,
        enabled_modules=enabled,
        request_id=uuid7(),
        principal=AuthenticatedPrincipal(account_id=None, bound_purpose=None),
    )


def context_for_harness(
    workspace_id: UUID,
    account_id: UUID,
    role: Role | str,
    entry: Entry | str = Entry.CLI,
    *,
    bound_purpose: ContextPurpose | None = None,
) -> WorkspaceContext | Refusal:
    """Test scaffolding: an account's context, ``profile = test``-gated, boundary-owned.

    Refuses ``profile_required`` under any profile other than ``test``, before any
    database read. Then reads ``control.membership`` for ``(account_id,
    workspace_id)`` and refuses ``membership_missing`` unless a row exists **with
    exactly that role**. The context carries the ratified values for an account actor
    (``overview.md`` § The workspace context): ``Actor(account, account_id)``,
    ``Audience(session, account_id)``, the full operation set, and ``entry``
    defaulting to ``cli`` because the closed ``Entry`` enum has no test value and C1
    added none. The session audience is correct here and stays: unlike
    :func:`context_for_operator`, this factory stands in for a session context, so it
    carries a session audience with a real account id.

    Why it exists: the core-operation table (``module-contract.md:96-101``) gives
    ``core.settings.set`` the role ``owner`` only and ``core.settings.set_member``
    ``owner, member``, and enumerates ``operator`` where it applies, so ``operator``
    is not an implicit superset; without this factory the settings operations and
    criterion 69 could not be driven end-to-end in 0b1.

    It does not build, stub, or pre-shape C7's ``context_from_session``: it reads no
    session table and takes no secret, host, or cookie.
    """
    if not isinstance(workspace_id, UUID) or not isinstance(account_id, UUID):
        raise TypeError("workspace_id and account_id must be UUIDs")
    wanted = Role(role)
    profile = current_profile()
    if profile != "test":
        return Refusal(
            PROFILE_REQUIRED,
            f"context_for_harness runs under profile = test only (resolved profile "
            f"is {profile!r})",
        )
    backend = get_backend()
    with backend.control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=account_id, workspace_id=workspace_id
        )
    if membership is None or membership.role is not wanted:
        return Refusal(
            MEMBERSHIP_MISSING,
            f"no control.membership row for account {account_id} in workspace "
            f"{workspace_id} with role {wanted.value!r}",
        )
    enabled = _active_workspace_modules(backend, workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=workspace_id,
        actor=Actor(kind=ActorKind.ACCOUNT, id=account_id),
        role=wanted,
        entry=Entry(entry),
        audience=Audience(kind=AudienceKind.SESSION, id=account_id),
        operation_set=ALL_OPERATIONS,
        enabled_modules=enabled,
        request_id=uuid7(),
        # Architecture A4: an account factory sets the authenticated account and an
        # unbound purpose. Browsing as a person is not done *for* a purpose, so there
        # is no memory-purpose gate to apply here. ``bound_purpose`` exists for tests
        # that stand in for a purpose-bound principal (a runtime or MCP token's) without
        # minting one, e.g. the redaction policy's per-purpose tiers (#130); it is
        # keyword-only and this factory stays ``profile = test``-gated.
        principal=AuthenticatedPrincipal(
            account_id=account_id, bound_purpose=bound_purpose
        ),
    )


def context_from_session(
    secret: bytes, host: str, entry: Entry | str = Entry.WEB
) -> WorkspaceContext | Refusal:
    """A web session's context (0b2/C7a): the contract 08's ``/auth/*`` routes and
    internal listener build against (``00-index.md`` § Coupling seams, 07 -> 08).

    Exact refusal order, followed literally because 08 builds HTTP status mapping
    against it: hash ``secret`` (SHA-256) and look up the ``session_secret`` row
    scoped to ``host`` (one join, ``get_session_by_secret_hash``) -> ``session_missing``
    if no row; else ``session_expired`` when the session's idle **or** absolute
    timeout has passed (both collapse to this one state; ``expires_at`` already
    reflects the tighter of the two, see ``rheo_core.sessions.service``); else
    ``session_revoked`` when ``revoked_at`` is not null; else ``workspace_unselected``
    when ``active_workspace_id`` is null. Then — and only then — read
    ``control.membership`` **at request time** (never anything cached on the session
    row) for ``(account_id, active_workspace_id)``, refusing ``membership_missing``
    (the existing constant, unchanged) if absent. Success runs the shared factory tail
    every 0b1 factory already runs (``_active_workspace_modules``): the workspace-active
    check (so a session pointed at a workspace gone ``unavailable`` refuses
    ``workspace_unavailable`` exactly like the other two factories, B16's clause) and
    the ``enabled_modules`` load. The context carries actor ``account`` (the session's
    account), the role from the membership row just read, audience ``session`` with
    that same account id, the full operation set, and ``entry`` as given (default
    ``web``).
    """
    backend = get_backend()
    secret_hash = hashlib.sha256(secret).digest()
    with backend.control_engine.connect() as connection:
        session_row = get_session_by_secret_hash(
            connection, secret_hash=secret_hash, host=host
        )
        if session_row is None:
            return Refusal(SESSION_MISSING)
        if session_row.expires_at <= datetime.now(UTC):
            return Refusal(SESSION_EXPIRED)
        if session_row.revoked_at is not None:
            return Refusal(SESSION_REVOKED)
        if session_row.active_workspace_id is None:
            return Refusal(WORKSPACE_UNSELECTED)
        membership = get_membership(
            connection,
            account_id=session_row.account_id,
            workspace_id=session_row.active_workspace_id,
        )
    if membership is None:
        return Refusal(
            MEMBERSHIP_MISSING,
            f"no control.membership row for account {session_row.account_id} in "
            f"workspace {session_row.active_workspace_id}",
        )
    enabled = _active_workspace_modules(backend, session_row.active_workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=session_row.active_workspace_id,
        actor=Actor(kind=ActorKind.ACCOUNT, id=session_row.account_id),
        role=membership.role,
        entry=Entry(entry),
        audience=Audience(kind=AudienceKind.SESSION, id=session_row.account_id),
        operation_set=ALL_OPERATIONS,
        enabled_modules=enabled,
        request_id=uuid7(),
        # A4 again, with the session's own account: authenticated, purpose unbound.
        principal=AuthenticatedPrincipal(
            account_id=session_row.account_id, bound_purpose=None
        ),
    )


def context_from_token(value: str, surface: str) -> WorkspaceContext | Refusal:
    """A bearer token's context (0b2/C8): the ``api`` surface (``apps/core``) and
    the MCP facade's ``session.py`` both build against this.

    Implements none of the refusal chain itself: ``rheo_core.tokens.presentation.
    resolve_token`` is the single implementation (parse, hash lookup,
    kind-for-surface, expiry, revocation, non-token-issuable scope,
    membership-at-presentation -- see that module's own docstring for the exact
    order). This function only turns a :class:`~rheo_core.tokens.presentation.
    ResolvedToken` into the one object the AST scan in ``tests/test_boundary.py``
    allows this package to build, running the shared factory tail
    (``_active_workspace_modules``) exactly as every other factory does.

    Success: actor ``Actor(token, token_id)``, audience ``Audience(token,
    token_id)``, role from the token account's membership row ``resolve_token`` read,
    operation set the snapshot as a ``frozenset[str]``, entry the presenting
    surface (``api`` or ``mcp``).
    """
    resolved = resolve_token(value, surface)
    if isinstance(resolved, Refusal):
        return resolved
    enabled = _active_workspace_modules(get_backend(), resolved.workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=resolved.workspace_id,
        actor=Actor(kind=ActorKind.TOKEN, id=resolved.token_id),
        role=resolved.role,
        entry=Entry(surface),
        audience=Audience(kind=AudienceKind.TOKEN, id=resolved.token_id),
        operation_set=resolved.operation_set,
        enabled_modules=enabled,
        request_id=uuid7(),
        # The account behind the token and whatever purpose the row was minted with:
        # ``resolve_token`` has already refused a runtime token missing one and any
        # token whose stored string is not a ``ContextPurpose``.
        principal=AuthenticatedPrincipal(
            account_id=resolved.account_id, bound_purpose=resolved.bound_purpose
        ),
    )


def context_from_operation(
    workspace_id: UUID,
    *,
    actor_kind: str,
    actor_id: UUID | None,
    audience_kind: str,
    audience_id: UUID | None,
    entry: str,
    purpose: str | None,
) -> WorkspaceContext | Refusal:
    """Rebuild a stored caller's ``WorkspaceContext`` from copied provenance fields.

    ACCOUNT uses membership of ``(actor_id, workspace_id)`` and ``ALL_OPERATIONS``.
    TOKEN uses the access-token row for ``actor_id``, membership of that token's
    account, and the snapshot as the operation set. OPERATOR / SYSTEM / CONNECTION
    refuse so the job can handshake ``runtime_actor_required``. Audience comes from
    the arguments, not an invented session.

    ``purpose`` is the binding the *stored* caller carried -- a runtime job's
    ``payload.purpose``, or an approval's ``core.approval.purpose``. It is
    ``str | None`` rather than ``str`` because both of this function's callers can
    legitimately have none: an approval minted for a caller that was not purpose-bound
    (a web session, or any approval written before 1a1 began storing the column) must
    still rebuild. ``None`` means unbound and skips the agreement check below; it is
    never a way to *drop* a binding, because the check that would have caught a
    dropped one is exactly the one the token branch runs.

    **A ``TOKEN`` actor's supplied purpose is checked against the token's own stored
    ``access_token.purpose``**, and a disagreement refuses ``purpose_mismatch``. The
    supplied value travels in a job payload, which is durable rows a later writer
    could edit; the token row is the mint-time record. Without the comparison, editing
    one JSON field would re-bind a runtime job to a purpose its token never carried.

    Every successful branch supplies ``principal``: the account this function actually
    resolved, and the purpose it actually parsed. Neither is taken from a caller.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    try:
        kind = ActorKind(actor_kind)
        audience = Audience(kind=AudienceKind(audience_kind), id=audience_id)
        entry_value = Entry(entry)
    except ValueError:
        return Refusal(
            "runtime_actor_required",
            f"core.runtime.run cannot rebuild actor {actor_kind!r}",
        )
    if kind in {ActorKind.OPERATOR, ActorKind.SYSTEM, ActorKind.CONNECTION}:
        return Refusal(
            "runtime_actor_required",
            f"core.runtime.run cannot run as actor kind {kind.value!r}",
        )
    # After the actor-kind refusals and before anything reads the database: an
    # operator/system/connection job still handshakes ``runtime_actor_required``
    # rather than being told about its purpose, which is the order
    # ``tests/test_boundary.py`` pins.
    bound_purpose = _parse_purpose(purpose)
    if isinstance(bound_purpose, Refusal):
        return bound_purpose
    backend = get_backend()
    operation_set: frozenset[str] | AllOperations
    account_id: UUID
    if kind is ActorKind.ACCOUNT:
        if actor_id is None:
            return Refusal("runtime_actor_required", "an account actor needs an id")
        with backend.control_engine.connect() as connection:
            membership = get_membership(
                connection, account_id=actor_id, workspace_id=workspace_id
            )
        if membership is None:
            return Refusal(
                MEMBERSHIP_MISSING,
                f"no control.membership row for account {actor_id} in workspace "
                f"{workspace_id}",
            )
        role = membership.role
        actor = Actor(kind=ActorKind.ACCOUNT, id=actor_id)
        operation_set = ALL_OPERATIONS
        account_id = actor_id
    elif kind is ActorKind.TOKEN:
        if actor_id is None:
            return Refusal("runtime_actor_required", "a token actor needs an id")
        with backend.control_engine.connect() as connection:
            token_row = get_access_token(connection, actor_id)
            operations = (
                list_access_token_operations(connection, actor_id)
                if token_row is not None
                else frozenset()
            )
            membership = (
                None
                if token_row is None
                else get_membership(
                    connection,
                    account_id=token_row.account_id,
                    workspace_id=workspace_id,
                )
            )
        if token_row is None or token_row.workspace_id != workspace_id:
            return Refusal(
                "runtime_actor_required",
                "the token has no backing account in this workspace",
            )
        if membership is None:
            return Refusal(
                MEMBERSHIP_MISSING,
                f"no control.membership row for account {token_row.account_id} in "
                f"workspace {workspace_id}",
            )
        if (token_row.purpose or None) != (
            None if bound_purpose is None else bound_purpose.value
        ):
            return _purpose_mismatch(
                f"the stored purpose {purpose!r} disagrees with what access token "
                f"{actor_id} was minted with"
            )
        role = membership.role
        actor = Actor(kind=ActorKind.TOKEN, id=actor_id)
        operation_set = operations
        account_id = token_row.account_id
    else:
        return Refusal(
            "runtime_actor_required",
            f"core.runtime.run cannot run as actor kind {kind.value!r}",
        )
    enabled = _active_workspace_modules(backend, workspace_id)
    if isinstance(enabled, Refusal):
        return enabled
    return WorkspaceContext(
        workspace_id=workspace_id,
        actor=actor,
        role=role,
        entry=entry_value,
        audience=audience,
        operation_set=operation_set,
        enabled_modules=enabled,
        request_id=uuid7(),
        principal=AuthenticatedPrincipal(
            account_id=account_id, bound_purpose=bound_purpose
        ),
    )


def context_for_approved_execution(
    ctx: WorkspaceContext, approval: "ApprovalRow"
) -> WorkspaceContext | Refusal:
    """The **original held caller's** context, rebuilt for an approved execution.

    ``core.approval.approve`` runs inside the *approver's* context, and before 1a1 the
    gated handler ran under it too: a member's held call executed with an owner's role,
    operation set and audience once the owner released it. This factory is what makes
    "the approver decides, the original caller acts" true of the handler and not only
    of :class:`~rheo_core.approvals.guards.ActorPermissionGuard`'s two field reads.

    **It takes nothing identifying from ``ctx`` except ``ctx.workspace_id``.** The
    actor is the approval's own ``actor_kind``/``actor_id``; the audience and entry
    come from the ``core.operation`` row ``hold_for_approval`` minted, read through
    :func:`~rheo_core.operations.records.get_context_provenance`; the purpose is the
    approval's own column. Reading the workspace off ``ctx`` is not a loophole -- the
    approval was read through a connection already routed to that workspace, so it is
    the workspace either way, and threading a second copy of it would only create
    somewhere for the two to disagree.

    The rebuild itself is :func:`context_from_operation`, unchanged and shared with
    the worker: both are the same question -- "who was this, really?" -- asked of
    stored provenance rather than of a live credential, and a second implementation
    would be a second set of membership rules to keep in step.

    ``purpose=approval.purpose`` may be ``None``, and must be: an approval held by a
    caller that was not purpose-bound, or one minted before this column had a writer,
    still has to execute. ``context_from_operation`` reads that as unbound and skips
    the agreement check; a *bound* original caller still has its token's stored
    purpose compared against the column.
    """
    provenance = _approval_provenance(ctx, approval)
    if isinstance(provenance, Refusal):
        return provenance
    return context_from_operation(
        ctx.workspace_id,
        actor_kind=approval.actor_kind,
        actor_id=approval.actor_id,
        audience_kind=provenance.audience_kind,
        audience_id=provenance.audience_id,
        entry=provenance.entry,
        purpose=approval.purpose,
    )


def _approval_provenance(
    ctx: WorkspaceContext, approval: "ApprovalRow"
) -> "OperationContextProvenance | Refusal":
    """The held ``core.operation`` row's audience and entry, or a refusal naming it.

    Deferred imports for the cycle this package's own docstring and
    ``approvals/gate.py``'s describe: ``rheo_core.operations`` imports this module
    through its registrar. The read is a separate narrow accessor rather than a
    widening of ``OperationRow``, whose published shape is deliberately narrower than
    the row.

    A missing row refuses rather than defaulting the audience: an approval whose
    operation record has gone is not an execution whose audience can be guessed, and
    guessing one is exactly how an output reaches somebody it was not resolved for.
    """
    # Both deferred: see this function's docstring.
    from rheo_core.operations.records import get_context_provenance
    from rheo_core.storage.routing import open_unit_of_work

    with open_unit_of_work(ctx) as uow:
        provenance = get_context_provenance(
            uow.connection, operation_id=approval.operation_id
        )
    if provenance is None:
        return Refusal(
            OPERATION_PROVENANCE_MISSING,
            f"approval {approval.id} names the operation record "
            f"{approval.operation_id}, which is not in this workspace",
        )
    return provenance
