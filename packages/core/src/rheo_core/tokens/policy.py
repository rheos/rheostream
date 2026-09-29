"""The non-token-issuable policy (B7): a policy constant, not an approval subsystem.

Checked by name, in two places: ``issue.py`` strips it from any set a token could
ever carry (an explicit list naming one is refused; a named package set never
contained one in the first place, by ``sets.py``'s own construction), and
``presentation.py``/``context_from_token`` refuses any presented token whose
snapshot somehow contains one of these eight names anyway (a row that cannot arise
from ``issue()`` itself -- only from a row inserted directly, outside it -- checked
regardless, so the rule holds at both ends).

The policy is by name, so it held before the approval and standing-grant operations
were registered, not only once they were. ``core.evidence_enrollment.create`` and
``.rotate`` joined in run 1a4b: each mints a bridge token itself, the reason
``core.token.issue`` is here. ``.revoke`` did not join; it only removes capability.
"""

from typing import Final

NON_TOKEN_ISSUABLE: Final[frozenset[str]] = frozenset(
    {
        "core.approval.approve",
        "core.approval.refuse",
        "core.evidence_enrollment.create",
        "core.evidence_enrollment.rotate",
        "core.standing_grant.create",
        "core.standing_grant.revoke",
        "core.token.issue",
        "core.token.revoke",
    }
)
"""The eight operation names no token may ever carry. Literal strings, not imported
constants: ``core.token.issue``/``core.token.revoke`` are also declared in
``operations/core_ops.py`` as ``TOKEN_ISSUE``/``TOKEN_REVOKE``, but importing them
from there would route ``rheo_core.tokens.policy`` -> ``rheo_core.operations.
core_ops`` -> ``rheo_core.tokens.issue`` -> ``rheo_core.tokens.policy``, a real
import cycle. Duplicating two short literal strings is cheaper than that cycle;
``core_ops.py`` carries a comment pointing back here so the two stay in lockstep."""


PERSON_HELD_TOKEN_KINDS: Final[frozenset[str]] = frozenset({"cli"})
"""The token kinds a person holds, and the only ones an act reserved for a person
accepts. An **allow-list**, deliberately: ``mcp`` and ``runtime`` are made for a model,
and a token kind added later is refused by every check that reads this until someone
decides it belongs here, rather than admitted until someone remembers to deny it.

Read by ``audit/operations.py`` (the ``core.audit.list`` opt-ins) and
``work/operations.py`` (``core.work.retry``, ``.skip`` and ``.replay``). A context that
is not a token at all (a session, the operator CLI) is not a token-kind question and
neither check applies to it. A token whose control-plane row is gone is refused, so
the rule fails closed."""
