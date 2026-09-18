"""The handlers behind ``core.token.issue`` and ``core.token.revoke`` (C8).

There is no separate ``revoke.py``: ``plan.md``'s C8 file map names exactly six
files under ``rheo_core/tokens/``, and ``revoke``'s handler is small enough to
live beside ``issue``'s here rather than justify a seventh file. Both are
registered in ``operations/core_ops.py``, which imports both handlers and their
input/output models from this module (never the reverse). ``rheo_core.
operations.refusals`` is a true leaf and is imported here at module level;
``rheo_core.operations.registry`` is not -- ``_role_permitted_set`` below
imports it lazily, inside the function, because ``core_ops.py`` imports this
module and ``rheo_core.operations``'s own ``__init__.py`` imports
``core_ops.py``, so a module-level import of ``operations.registry`` here
would close a real cycle (the same one ``tokens/sets.py``'s own module
docstring describes in full).

``issue``'s pipeline: resolve the issuing authority from ``ctx`` (a session
issuer's permitted set is every operation whose declared roles include the
account's role in the target workspace; an operator issuer's permitted set is
the union of the three named package sets, intersected with the *target
account's* role in the target workspace, since an operator context carries no
role of its own that means anything here) -> expand the named set or the
explicit list -> intersect with the issuer's permitted set -> strip
:data:`~rheo_core.tokens.policy.NON_TOKEN_ISSUABLE` (an **explicit** list naming
one of the six is refused ``SET_NOT_ISSUABLE`` naming the operation; a **named
package set** silently loses them -- it never contained them in the first
place, by ``sets.py``'s own construction) -> refuse ``SET_EMPTY`` if nothing
survives -> write one ``access_token`` row and one ``access_token_operation``
row per surviving name, in one control-plane transaction, with
``expires_at = now + identity.token_max_days.<kind>`` resolved under its
``min`` floor for the target workspace -> return the raw value once.

**Known, deliberate limitation -- do not attempt to fix this:** the write above
lands entirely in the control-plane database, a different database from the
workspace ``UnitOfWork`` that ``dispatch()`` opens and commits for every
operation. There is no single transaction spanning both; closing that gap is
the deferred outbox's job (0c's), not this run's. The workspace ``uow`` this
handler receives is opened and committed by ``dispatch()`` as usual, but this
handler never writes through it -- there is nothing workspace-scoped for a
token to write.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import ActorKind, Role, WorkspaceContext

from rheo_core.boundary.context import MEMBERSHIP_MISSING
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import (
    AccessTokenRow,
    get_access_token,
    get_membership,
    insert_access_token,
    insert_access_token_operations,
    revoke_access_token,
)
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.format import mint
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE
from rheo_core.tokens.sets import PACKAGE_SETS, agent_default, cli_full, read_only

SET_NOT_ISSUABLE: Final = "set_not_issuable"
"""An explicit ``operations`` list named one of the six non-token-issuable
operations. Module-local (a handler's ``OperationRefused``), not a
``boundary.context`` constant -- see ``core_ops.py``'s own comment on this
pattern for ``COMPOSITION_MISSING``/``ACTOR_REQUIRED``."""

SET_EMPTY: Final = "set_empty"
"""Nothing survived expansion, intersection and the strip below."""

SET_SELECTION_INVALID: Final = "set_selection_invalid"
"""Neither or both of ``set_name``/``operations`` were given."""

ACCOUNT_REQUIRED: Final = "account_required"
"""An operator issuance named no ``account_id``, or ``ctx``'s actor is neither an
account nor an operator (should not be reachable given this operation's
declared roles, checked anyway)."""

_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class TokenIssueInput(BaseModel):
    """Exactly one of ``set_name``/``operations``. ``account_id`` is required for
    an operator issuer (who has no account of their own) and ignored for a
    session issuer (whose own account is always the target -- never a payload
    field, the same reasoning ``core.settings.set_member`` applies to its own
    actor)."""

    model_config = _IGNORE_EXTRA

    kind: Literal["cli", "mcp"]
    set_name: str | None = None
    operations: list[str] | None = None
    account_id: UUID | None = None
    purpose: str | None = None


class TokenIssued(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: UUID
    value: str
    """The raw, rendered token value -- returned once, never stored, never
    logged."""
    kind: str
    expires_at: datetime
    operations: list[str]


class TokenRevokeInput(BaseModel):
    model_config = _IGNORE_EXTRA

    token_id: UUID


class TokenRevoked(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: UUID


def _role_permitted_set(role: Role) -> frozenset[str]:
    """Every registered operation whose declared roles include ``role`` -- the
    upper bound an issuer of that role could ever grant, independent of any
    named package set.

    A deferred, function-local import of ``REGISTRY`` -- the same reasoning
    ``tokens/sets.py``'s own module docstring gives: ``core_ops.py`` imports
    this module, and ``rheo_core.operations``'s own ``__init__.py`` imports
    ``core_ops.py``, so a module-level import of ``rheo_core.operations.
    registry`` here would close the same cycle a module-level import in
    ``sets.py`` would.
    """
    from rheo_core.operations.registry import REGISTRY

    permitted: set[str] = set()
    for name in REGISTRY.names():
        registered = REGISTRY.lookup(name)
        assert registered is not None  # names() and lookup() share one table
        if role in registered.declaration.roles:
            permitted.add(name)
    return frozenset(permitted)


def _expand(model_input: TokenIssueInput) -> frozenset[str]:
    """The named set or the explicit list, expanded. An explicit list naming a
    non-token-issuable operation is refused here, immediately, naming it --
    before any permitted-set intersection, so the refusal is specific to
    "you asked for this by name", distinct from a named set (which never
    contains one of the six to begin with)."""
    if (model_input.set_name is None) == (model_input.operations is None):
        raise OperationRefused(
            SET_SELECTION_INVALID,
            "core.token.issue takes exactly one of set_name or operations",
        )
    if model_input.operations is not None:
        requested = frozenset(model_input.operations)
        offending = sorted(requested & NON_TOKEN_ISSUABLE)
        if offending:
            raise OperationRefused(
                SET_NOT_ISSUABLE, f"{offending[0]} is not token-issuable"
            )
        return requested
    set_name = model_input.set_name
    assert set_name is not None
    resolver = PACKAGE_SETS.get(set_name)
    if resolver is None:
        raise OperationRefused(SET_NOT_ISSUABLE, f"unknown package set {set_name!r}")
    return resolver()


def _issuer_permitted_set(
    ctx: WorkspaceContext, model_input: TokenIssueInput
) -> tuple[UUID, str, frozenset[str]]:
    """``(target_account_id, issued_from, permitted_set)`` for ``ctx``'s actor.

    A session issuer (``ctx.actor.kind is ACCOUNT`` -- a real session, or the
    test harness's ``context_for_harness``) issues only for its own account,
    with every operation ``ctx.role`` could ever be granted. An operator issuer
    names the target account explicitly and is bounded by the union of the
    three package sets *for that account's own role* in this workspace, since
    ``Role.OPERATOR`` itself carries no membership-shaped permission here.
    """
    if ctx.actor.kind is ActorKind.ACCOUNT:
        assert ctx.actor.id is not None
        return ctx.actor.id, "session", _role_permitted_set(ctx.role)
    if ctx.actor.kind is ActorKind.OPERATOR:
        if model_input.account_id is None:
            raise OperationRefused(
                ACCOUNT_REQUIRED,
                "an operator issuance needs an explicit account_id",
            )
        target_account_id = model_input.account_id
        backend = get_backend()
        with backend.control_engine.connect() as connection:
            membership = get_membership(
                connection,
                account_id=target_account_id,
                workspace_id=ctx.workspace_id,
            )
        if membership is None:
            raise OperationRefused(
                MEMBERSHIP_MISSING,
                f"account {target_account_id} has no membership in workspace "
                f"{ctx.workspace_id}",
            )
        package_union = read_only() | agent_default() | cli_full()
        return (
            target_account_id,
            "operator",
            package_union & _role_permitted_set(membership.role),
        )
    raise OperationRefused(
        ACCOUNT_REQUIRED,
        f"core.token.issue cannot resolve an issuing account from actor kind "
        f"{ctx.actor.kind.value!r}",
    )


def issue_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: TokenIssueInput
) -> TokenIssued:
    requested = _expand(model_input)
    target_account_id, issued_from, issuer_permitted = _issuer_permitted_set(
        ctx, model_input
    )
    survivors = (requested & issuer_permitted) - NON_TOKEN_ISSUABLE
    if not survivors:
        raise OperationRefused(
            SET_EMPTY, "no operation survives the issuer's permitted set"
        )
    settings = resolve(workspace_id=ctx.workspace_id, source=PostgresOverrideSource())
    max_days = settings.get_int(f"identity.token_max_days.{model_input.kind}")
    expires_at = datetime.now(UTC) + timedelta(days=max_days)
    value, raw = mint(model_input.kind)
    token_hash = hashlib.sha256(raw).digest()
    operations = sorted(survivors)
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row = insert_access_token(
            connection,
            account_id=target_account_id,
            workspace_id=ctx.workspace_id,
            kind=model_input.kind,
            issued_from=issued_from,
            token_hash=token_hash,
            set_name=model_input.set_name,
            purpose=model_input.purpose,
            expires_at=expires_at,
        )
        insert_access_token_operations(
            connection, token_id=row.id, operation_names=operations
        )
    return TokenIssued(
        token_id=row.id,
        value=value,
        kind=model_input.kind,
        expires_at=expires_at,
        operations=operations,
    )


def revoke_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: TokenRevokeInput
) -> TokenRevoked:
    """Revoke a token by id, refusing ``not_found`` for one that does not exist,
    *belongs to another workspace*, or -- for a session actor -- belongs to
    another account. All three collapse to the same state and the same
    non-disclosure principle ``harness.note``'s resolver applies to a
    cross-workspace reference: a caller must not learn that a token id exists
    for a workspace or an account that is not theirs.

    ``spec.md``'s own line for this operation is "owner, member for self,
    operator for another account" -- ``_issuer_permitted_set`` above already
    honours "member for self" on the issue side (a session actor can only
    ever target ``ctx.actor.id``, never a payload-supplied account); this is
    revoke's half of the same rule. An ``ActorKind.OPERATOR`` actor is
    deliberately exempt from the ownership check below -- "operator for
    another account" is the intended behaviour there, and
    ``context_for_operator`` is constructed nowhere but the ``rheo`` CLI's own
    bootstrap.
    """
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row: AccessTokenRow | None = get_access_token(connection, model_input.token_id)
        if (
            row is None
            or row.workspace_id != ctx.workspace_id
            or (ctx.actor.kind is ActorKind.ACCOUNT and row.account_id != ctx.actor.id)
        ):
            raise OperationRefused(
                NOT_FOUND, f"no access token {model_input.token_id} in this workspace"
            )
        revoke_access_token(connection, model_input.token_id)
    return TokenRevoked(token_id=model_input.token_id)


def issue_runtime_token(
    *,
    account_id: UUID,
    workspace_id: UUID,
    purpose: str,
    expires_at: datetime,
    operations: Sequence[str],
) -> tuple[UUID, str]:
    """Mint a run-scoped ``kind='runtime'`` token. Opens the control-plane engine.

    Returns ``(token_id, raw_value)``. The value goes only onto
    ``AdapterSpawn.run_token``.
    Snapshot rows may be empty: a run with no tools is allowed.
    """
    value, raw = mint("runtime")
    token_hash = hashlib.sha256(raw).digest()
    names = sorted(operations)
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row = insert_access_token(
            connection,
            account_id=account_id,
            workspace_id=workspace_id,
            kind="runtime",
            issued_from="runtime",
            token_hash=token_hash,
            set_name=None,
            purpose=purpose,
            expires_at=expires_at,
        )
        insert_access_token_operations(
            connection, token_id=row.id, operation_names=names
        )
    return row.id, value
