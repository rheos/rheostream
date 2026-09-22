"""The boundary refusal and the refusal states this run raises at the boundary.

A factory returns either a ``WorkspaceContext`` or a :class:`Refusal` whose ``state``
is one of the string constants below. They are plain string constants, not an enum:
0b2 adds ``session_missing``, ``session_expired``, ``session_revoked``,
``workspace_unselected`` and the token states beside them, and an open enum that
later runs must extend is the wrong shape for a set that grows by boundary.

``Refusal`` is also what the operation registry returns from ``authorize`` and what
the dispatcher folds into an ``OperationOutcome``: one refusal type at every seam,
so a caller never has to know which layer said no.
"""

from dataclasses import dataclass
from typing import Final

CONTEXT_REQUIRED: Final = "context_required"
"""No ``WorkspaceContext`` was supplied: ``None``, or an object that is not one."""

WORKSPACE_UNAVAILABLE: Final = "workspace_unavailable"
"""The ``control.workspace`` row is not ``active``; ``detail`` carries its state."""

MEMBERSHIP_MISSING: Final = "membership_missing"
"""No ``control.membership`` row for the account, or not with the role asked for."""

PROFILE_REQUIRED: Final = "profile_required"
"""A test-profile-only factory was called under another profile."""

WORKSPACE_MISSING_DETAIL: Final = "missing"
"""The ``workspace_unavailable`` detail when there is no registry row at all."""

# --- 0b2 additions: sessions, the identity flow, and (declared here, raised in 08) ----
# ``session_missing``, ``session_expired``, ``session_revoked`` and
# ``workspace_unselected`` are raised by ``context_from_session`` in this run (C7a).
# ``not_a_member``, ``invalid_return``, ``invalid_state``, ``invalid_grant`` and
# ``signup_closed`` are raised by 08's HTTP routes and by ``accounts.py``'s signup
# check; they are declared here, the one file both C7 halves share for boundary
# refusal states, rather than in 08's own map. 09 appends the token states beside
# these later; this file reserves no space for them.

SESSION_MISSING: Final = "session_missing"
"""No ``control.session_secret`` row hashes to the presented session secret."""

SESSION_EXPIRED: Final = "session_expired"
"""The session's idle or absolute timeout has passed; both report this one state."""

SESSION_REVOKED: Final = "session_revoked"
"""``control.session.revoked_at`` is not null (a logout, on any host)."""

WORKSPACE_UNSELECTED: Final = "workspace_unselected"
"""The session has no ``active_workspace_id`` yet."""

NOT_A_MEMBER: Final = "not_a_member"
"""A workspace switch named a workspace the account holds no membership in."""

INVALID_RETURN: Final = "invalid_return"
"""``/auth/continue``'s ``return`` host is outside ``application_hosts()``."""

INVALID_STATE: Final = "invalid_state"
"""The OAuth ``state`` returned by the provider does not match the cookie set at
``begin``."""

INVALID_GRANT: Final = "invalid_grant"
"""A session grant code is unknown, used, expired, for the wrong host, or its nonce
does not match; all four collapse to this one state so a caller cannot distinguish an
attack from a stale link."""

SIGNUP_CLOSED: Final = "signup_closed"
"""An unknown identity tried to sign in while signup is closed (an account already
exists and ``identity.allow_signup`` is not ``true``)."""

# --- 09 (C8) appends the token states below, after 08's nine above --------------
# Raised by ``rheo_core.tokens.presentation.resolve_token`` and returned through
# ``context_from_token`` (``boundary/factories.py``). Appended after 07's nine
# session/grant constants above, per this run's ordering rule: do not touch,
# reorder or renumber those.

TOKEN_MALFORMED: Final = "token_malformed"
"""The presented value does not parse as ``rheo_<kind>_<43 chars>``, or its hash
matches no ``access_token`` row -- deliberately the same state for both: a miss
gives an attacker no signal about which credential space they guessed into."""

TOKEN_EXPIRED: Final = "token_expired"
"""``access_token.expires_at`` has passed."""

TOKEN_REVOKED: Final = "token_revoked"
"""``access_token.revoked_at`` is not null."""

TOKEN_WRONG_KIND: Final = "token_wrong_kind"
"""The token's ``kind`` is not one the presenting surface accepts (``api``:
``cli``, ``mcp``; ``mcp``: ``mcp``, ``runtime``)."""

TOKEN_SCOPE_INVALID: Final = "token_scope_invalid"
"""The token's snapshotted operation set contains one of the six
non-token-issuable operations -- reachable only from a row inserted outside
``core.token.issue``, checked anyway so the rule holds at both ends."""

# --- 1a1 appends the purpose-binding state below, after 09's five token states -------
# Raised by ``rheo_core.tokens.presentation.resolve_token`` when a presented token's
# stored ``access_token.purpose`` cannot become the ``bound_purpose`` its context
# would carry. Appended rather than inserted, per this file's ordering rule.

OPERATION_PROVENANCE_MISSING: Final = "operation_provenance_missing"
"""``context_for_approved_execution`` could not read the ``core.operation`` row its
approval names, so the original caller's audience and entry are unknown.

Unreachable by construction -- ``hold_for_approval`` mints the record and the approval
in one transaction and nothing deletes a `core.operation` row -- and refused rather
than defaulted anyway: an execution whose audience had to be guessed is how an output
reaches somebody it was never resolved for."""

TOKEN_PURPOSE_INVALID: Final = "token_purpose_invalid"
"""The presented token's ``purpose`` column is not a usable binding: a ``runtime``
token with no purpose at all (one is minted with it, always, so a null means the row
was written outside ``issue_runtime_token``), or any token whose stored string is not
a ``ContextPurpose`` member. Refused rather than downgraded to "unbound": a runtime
token that lost its purpose would otherwise present as a token with no
memory-purpose gate."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """A named refusal. ``detail`` is safe to show and never carries a secret."""

    state: str
    detail: str | None = None

    def __str__(self) -> str:
        return self.state if self.detail is None else f"{self.state}: {self.detail}"
