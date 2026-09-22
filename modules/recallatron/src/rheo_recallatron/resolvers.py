"""The record resolver for a memory reference: generic resolution under permission.

``resolve_in`` is what every other module, the audit path and the approval guards use
to turn a reference into a head without owning the record. For a memory that answer is
:func:`~rheo_recallatron.eligibility.eligible_memory` and nothing else — the resolver
adds no rule of its own, which is what keeps "what may this caller see" one
implementation rather than two that agree today.

**Always ``current`` mode.** § A6: ``current`` is "the only mode for generic record
resolution". Retained history is reachable only through the explicit memory read
operation, which is where a caller has to ask for it by name.
"""

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.refs.resolver import LIVE, RecordHead, Unavailable, UnitOfWork

from rheo_recallatron.eligibility import (
    Denied,
    ReadMode,
    begin_request,
    eligible_memory,
)
from rheo_recallatron.refusals import RETENTION_UNAVAILABLE


def resolve_memory(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    """Resolve one memory reference, or say why not without saying anything else.

    An unknown, cross-workspace, expired or otherwise ineligible record answers
    ``not_found`` with no head and no label, indistinguishably from a reference that
    never existed. A workspace whose retention policy is missing or corrupt answers
    ``retention_unavailable`` before the row is read at all (AC 8).
    """
    request = begin_request(ctx, uow)
    if request is None:
        return Unavailable(ref.format(), RETENTION_UNAVAILABLE)
    decision = eligible_memory(ctx, uow, ref.id, mode=ReadMode.CURRENT, request=request)
    if isinstance(decision, Denied):
        return Unavailable(ref.format(), decision.state)
    return RecordHead(
        ref=ref,
        display=decision.row.title,
        readable=True,
        state=LIVE,
        revision=decision.row.revision,
    )
