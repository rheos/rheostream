"""Whether an acting principal is a person speaking in their own words.

Only a person's own statement is evidence an automatic memory may be built from. A
model's output, or a task a model wrote for another model, is not, however it reads.
The rule is one allow-list and one predicate:

- an ``account`` actor is a person signed in;
- a ``token`` actor is a person only when its kind is in
  :data:`~rheo_core.tokens.policy.PERSON_HELD_TOKEN_KINDS` (today exactly ``cli``). The
  allow-list is imported, never restated, so the acts reserved for a person and the
  text attributed to one move together. ``mcp`` and ``runtime`` tokens are made for a
  model and are never attributable; a token kind added later is excluded until someone
  adds it to that allow-list;
- every other actor kind (``operator``, ``system``, ``connection``) is not.

A script holding its owner's ``cli`` token reads as the owner typing. That is the same
trust boundary every ``cli``-reserved act already accepts, and a named residual rather
than something this predicate can see.

Pure: no database, no settings, no clock.
"""

from rheo_core.tokens.policy import PERSON_HELD_TOKEN_KINDS

_ACCOUNT = "account"
_TOKEN = "token"


def is_human_attributable(actor_kind: str, token_kind: str | None) -> bool:
    """True when the actor's words may be recorded as a person's own statement.

    ``actor_kind`` is ``ctx.actor.kind``; an ``ActorKind`` is a ``StrEnum`` and compares
    equal to its string value, so the enum and the plain string both work.
    ``token_kind`` is the presented token's ``access_token.kind``, or ``None`` when the
    actor is not a token.
    """
    if actor_kind == _ACCOUNT:
        return True
    if actor_kind == _TOKEN:
        return token_kind is not None and token_kind in PERSON_HELD_TOKEN_KINDS
    return False
