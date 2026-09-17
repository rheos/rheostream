"""The presented-token refusal chain (C8), called from ``boundary/factories.py``'s
``context_from_token`` -- which is the actual public entry point (the coupling
seam this module and that function share, ``00-index.md`` § Coupling seams).

This module does not construct a ``WorkspaceContext`` itself: the AST scan in
``tests/test_boundary.py`` asserts that type is built only under
``rheo_core/boundary/``, so :func:`resolve_token` stops one step short and returns
:class:`ResolvedToken` (or a ``Refusal``) instead; ``context_from_token`` is the
thin wrapper that runs the shared factory tail and builds the object. The chain's
logic lives here, once, not duplicated in ``factories.py``.

Exact order (pinned by this run's checkpoint, and matched by ``format.py``'s own
docstring): parse (``TOKEN_MALFORMED`` on a bad shape) -> hash lookup
(``TOKEN_MALFORMED`` when no row -- a session secret presented here is malformed
by construction, not "not found", so an attacker probing this boundary gets no
signal about which credential space they guessed into) -> kind accepted by the
surface (``TOKEN_WRONG_KIND``) -> ``TOKEN_EXPIRED`` -> ``TOKEN_REVOKED`` ->
snapshot loaded and checked against ``NON_TOKEN_ISSUABLE`` (``TOKEN_SCOPE_INVALID``
-- reachable only from a row written outside ``core.token.issue``; checked anyway
so the rule holds at both ends) -> ``control.membership`` read at presentation
time, never cached (``MEMBERSHIP_MISSING``) -> ``last_used_at`` touched. Every
refusal happens before any handler runs and inside no workspace transaction --
that is what "no partial effect" means mechanically (B6).
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from rheo_contracts import Role

from rheo_core.boundary.context import (
    MEMBERSHIP_MISSING,
    TOKEN_EXPIRED,
    TOKEN_MALFORMED,
    TOKEN_REVOKED,
    TOKEN_SCOPE_INVALID,
    TOKEN_WRONG_KIND,
    Refusal,
)
from rheo_core.storage.control_plane import (
    get_access_token_by_hash,
    get_membership,
    list_access_token_operations,
    touch_access_token_last_used,
)
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.format import parse
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE

_SURFACE_ACCEPTS: Final[dict[str, frozenset[str]]] = {
    "api": frozenset({"cli", "mcp"}),
    "mcp": frozenset({"mcp", "runtime"}),
}
"""Which token ``kind``s each bearer surface accepts (``format.py``'s docstring,
``presentation.py``'s own checkpoint). An unrecognised surface accepts nothing --
every registered kind refuses ``TOKEN_WRONG_KIND`` -- rather than raising, since
the only two callers (``apps/core``'s ``api`` route and ``apps/mcp``'s
``session.py``) always pass one of the two keys above."""


@dataclass(frozen=True, slots=True)
class ResolvedToken:
    """Everything ``context_from_token`` needs to build a ``WorkspaceContext``,
    short of the object itself: the account, the workspace, the account's role
    (from the membership row just read, at presentation time), and the snapshot
    as the eventual context's operation set."""

    token_id: UUID
    account_id: UUID
    workspace_id: UUID
    role: Role
    operation_set: frozenset[str]


def resolve_token(value: str, surface: str) -> ResolvedToken | Refusal:
    """The chain described in the module docstring, over one control-plane
    transaction (so the ``last_used_at`` touch on success is atomic with the
    reads that justified it)."""
    parsed = parse(value)
    if parsed is None:
        return Refusal(TOKEN_MALFORMED)
    token_hash = hashlib.sha256(parsed.raw).digest()
    backend = get_backend()
    with backend.control_engine.begin() as connection:
        row = get_access_token_by_hash(connection, token_hash)
        if row is None:
            # A session secret presented here is malformed by construction, not
            # "not found" -- see the module docstring.
            return Refusal(TOKEN_MALFORMED)
        if row.kind not in _SURFACE_ACCEPTS.get(surface, frozenset()):
            return Refusal(TOKEN_WRONG_KIND)
        if row.expires_at <= datetime.now(UTC):
            return Refusal(TOKEN_EXPIRED)
        if row.revoked_at is not None:
            return Refusal(TOKEN_REVOKED)
        snapshot = list_access_token_operations(connection, row.id)
        if snapshot & NON_TOKEN_ISSUABLE:
            return Refusal(TOKEN_SCOPE_INVALID)
        membership = get_membership(
            connection, account_id=row.account_id, workspace_id=row.workspace_id
        )
        if membership is None:
            return Refusal(
                MEMBERSHIP_MISSING,
                f"no control.membership row for account {row.account_id} in "
                f"workspace {row.workspace_id}",
            )
        touch_access_token_last_used(connection, row.id)
    return ResolvedToken(
        token_id=row.id,
        account_id=row.account_id,
        workspace_id=row.workspace_id,
        role=membership.role,
        operation_set=snapshot,
    )
