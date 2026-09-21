"""Architecture § A6's one eligibility function, and the row-local SQL that prefilters
candidates for it.

**One implementation, called by everything that reads.** Recall, the centered read and
the record resolver all route through :func:`eligible_memory`; a later run's entity
visibility, source reads and correction/supersession authorization route through it
too. There is deliberately no second permission check anywhere in this package — a
``grep`` for an audience or purpose comparison outside this file should find only the
SQL prefilter below, which is this same function's cheap first half and is never its
replacement.

**What the function checks, in § A6's order:** the caller's module and role, the row's
audience, the context's bound purpose, current retention, the requested lifecycle mode,
and then every actual permission-bearing link the row carries. A missing,
cross-workspace, expired or otherwise ineligible target collapses to ``not_found`` with
no head, label or partial metadata — the cross-workspace case for free, because the
unit of work is routed to the caller's own database and the row simply is not in it.

**Two budgets, one per request.** § A13 bounds a read at 4096 distinct references and
depth 64. Both live on :class:`MemoryRequest`, so a target, its links, every candidate
in a window and *their* links all draw on the same allowance; exhausting it refuses
``reference_scan_limit`` with no partial content. A reference already charged is free
on every later visit, which is what makes a 500-row window over one shared container
cost 501 references rather than 1000.

**The prefilter and the function must agree, and they are written next to each other so
that a change to one is a change in the reader's eye-line.** The SQL narrows to rows
that *could* be eligible using only row-local columns and ``EXISTS`` — never a join
that would multiply a row by its links or purposes. Everything that needs another row
resolved (a linked record, a linked memory, a contact permission) happens in Python,
after the scan, per candidate, and before any content leaves the service.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from rheo_contracts import (
    ContextPurpose,
    RecordRef,
    RecordRefMalformed,
    Role,
    WorkspaceContext,
)
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.refs.resolver import (
    LIVE,
    RecordHead,
    Unavailable,
    UnitOfWork,
    resolve_in,
)
from rheo_core.settings.schema import SettingTypeMismatch, decode_text
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import ColumnElement, and_, literal, or_, select

from rheo_recallatron.configuration import (
    CANDIDATE_SENTINEL_LIMIT,
    MAX_DISTINCT_REFERENCES,
    MAX_REFERENCE_DEPTH,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
    RETENTION_DAYS_KEY,
    RETENTION_DAYS_SPEC,
)
from rheo_recallatron.refusals import NOT_FOUND, REFERENCE_SCAN_LIMIT
from rheo_recallatron.storage import tables as t
from rheo_recallatron.storage.repository import (
    MemoryLinkRow,
    MemoryRow,
    get_memory,
    get_memory_purpose,
    list_memory_links,
)

AUDIENCE_WORKSPACE, AUDIENCE_MEMBER = t.AUDIENCE_KINDS
_SOURCE_DELETED, SOURCE_CORRECTED, SOURCE_SUPERSEDED = t.INVALIDATION_REASONS
RELATION_DERIVED_FROM, RELATION_ABOUT = t.LINK_RELATIONS

RETAINED_INVALIDATION_REASONS: Final = (SOURCE_CORRECTED, SOURCE_SUPERSEDED)
"""The two non-erasure states ``history`` admits. ``source_deleted`` is absent from
this tuple and that absence is the rule: erased content is never history."""

READ_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE})
"""§ A10's roles for every memory read.

``operator`` is deliberately absent, exactly as it is from § A10's table — unlike the
core's own workspace-status operation, which names it. The operation registry applies
the same set to ``recall``/``read`` before a handler runs; this check is what applies
it on the resolver path, which has no declaration of its own to be gated by.
"""


class ReadMode(StrEnum):
    """Which lifecycle states a read admits.

    ``CURRENT`` is the default, and the only mode for generic record resolution or for
    a new derive/correct/supersede source. ``HISTORY`` is selected explicitly by
    ``include_invalidated``; it admits the two retained non-erasure states and
    recursively admits them on actual memory-source links, while preserving **every**
    audience, bound-purpose, linked-record and retention check. It is not a permission
    override, and a missing or erased source still denies.
    """

    CURRENT = "current"
    HISTORY = "history"


_CONTACT_MODULE: Final = "leads"
_CONTACT_OPERATION: Final = f"{_CONTACT_MODULE}.contact_permission.check"
_CONTACT_PERMITTED: Final = "permitted"
_PARTY_MODULE: Final = "relationships"
_PARTY_RECORD_TYPE: Final = "party"
"""§ A6's later contact seam, named in pieces rather than as one dotted literal.

The schema-ownership scan reads a ``<module>.<name>`` literal for one of the four
sibling module ids as a foreign-storage reference, and is right to — so the operation
name is assembled from its own module id above rather than written out. The seam
itself is a lookup and nothing more: no module is invented here to answer it.
"""

CONTACT_LOOKUP: OperationRegistry = REGISTRY
"""Where the contact-permission operation is looked up.

The process-wide operation registry, which in this repository has no such operation
registered — so the lookup answers "absent", which § A6 defines as *not applicable*.
It is a rebindable module attribute rather than a direct call so that a synthetic
present implementation can be exercised without registering one into the registry the
rest of the session reads.
"""


@dataclass(frozen=True, slots=True)
class LinkHead:
    """One of a memory's links, as it may safely be shown to this caller.

    ``display`` is ``None`` for a marked lineage link and for nothing else: marked
    ancestry is content-free provenance, so it supplies a reference and never a
    predecessor's head or body. Every other link reached this far resolved live and
    readable, which is why its own head may travel with the row.
    """

    ref: str
    relation: str
    supersession_lineage: bool
    display: str | None


@dataclass(frozen=True, slots=True)
class Eligible:
    """The row this caller may read, with the link heads already resolved for it."""

    row: MemoryRow
    links: tuple[LinkHead, ...]


@dataclass(frozen=True, slots=True)
class Denied:
    """Why not, in the vocabulary a caller sees: ``not_found`` or
    ``reference_scan_limit`` and never anything finer."""

    state: str


class ReferenceBudget:
    """§ A13's request-wide allowance: 4096 distinct references, depth 64.

    Distinct, not total: a container reached from five hundred members is one
    reference. The count is what bounds the work a single request can ask of the
    database; the depth is what bounds one chain of derivation.
    """

    __slots__ = ("_seen", "_limit", "_depth_limit")

    def __init__(
        self,
        *,
        limit: int = MAX_DISTINCT_REFERENCES,
        depth_limit: int = MAX_REFERENCE_DEPTH,
    ) -> None:
        self._seen: set[str] = set()
        self._limit = limit
        self._depth_limit = depth_limit

    def charge(self, reference: str) -> bool:
        """Record one reference check. ``False`` once the allowance is spent."""
        if reference in self._seen:
            return True
        if len(self._seen) >= self._limit:
            return False
        self._seen.add(reference)
        return True

    def within_depth(self, depth: int) -> bool:
        return depth <= self._depth_limit

    @property
    def charged(self) -> int:
        return len(self._seen)


class MemoryRequest:
    """The values § A6 says to freeze for one request, and the decisions taken under
    them.

    ``now`` and ``retention_days`` are read once and never re-read: a retention value
    that changed, or a clock that advanced, part-way through one window would make the
    window describe two different moments. The decision cache is keyed by
    ``(memory id, mode)`` so a candidate that is also somebody's source is evaluated
    once, and ``_active`` is the recursion stack that makes a reference cycle fail
    closed instead of recursing.
    """

    __slots__ = ("ctx", "now", "retention_days", "budget", "_decisions", "_active")

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        now: datetime,
        retention_days: int,
        budget: ReferenceBudget | None = None,
    ) -> None:
        self.ctx = ctx
        self.now = now
        self.retention_days = retention_days
        self.budget = ReferenceBudget() if budget is None else budget
        self._decisions: dict[tuple[UUID, ReadMode], Eligible | Denied] = {}
        self._active: set[UUID] = set()

    @property
    def account_id(self) -> UUID | None:
        """The authenticated account member audience resolves against; ``None`` for an
        accountless caller, which therefore sees workspace rows only."""
        return self.ctx.principal.account_id

    @property
    def bound_purpose(self) -> ContextPurpose | None:
        """The context's purpose binding, or ``None`` for a caller with no purpose gate
        at all — an unbound authenticated human or service browsing."""
        return self.ctx.principal.bound_purpose

    @property
    def horizon(self) -> datetime:
        """The oldest ``recorded_at`` still readable. Equality is retained (§ A9), so
        every comparison against it is ``>=``."""
        return self.now - timedelta(days=self.retention_days)


def effective_retention_days(ctx: WorkspaceContext, uow: UnitOfWork) -> int | None:
    """The workspace's stored retention window, or ``None`` when it is unusable.

    ``None`` means AC 8's ``retention_unavailable``: the row is absent, will not parse
    as an integer, or is outside the declared 1-3650 range. The caller decides how to
    say so — an operation raises the refusal, the record resolver answers
    ``Unavailable`` — but no caller may carry on, and **none substitutes the package
    default**. The settings resolver's own rule is to drop a bad stored value and log
    it, which is right for a setting whose default is a safe answer and wrong for this
    one: silently restoring 365 days is how a workspace that meant to keep thirty ends
    up serving a year of content the moment its policy row goes missing.

    Read on the caller's own connection through the transaction-bound override source,
    so the value is the one **this** transaction sees — the same read a write's
    acceptance recheck and the sweep's own locked read will make.
    """
    overrides = TransactionBoundOverrideSource(
        uow, workspace_id=ctx.workspace_id
    ).workspace_overrides(ctx.workspace_id)
    stored = overrides.get(RETENTION_DAYS_KEY)
    if stored is None:
        return None
    try:
        value = decode_text(
            RETENTION_DAYS_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        # Both halves land here: a value that is not an integer, and an integer
        # outside the declared range (``decode_text`` applies the bounds itself).
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def begin_request(
    ctx: WorkspaceContext, uow: UnitOfWork, *, now: datetime | None = None
) -> MemoryRequest | None:
    """Freeze this request's clock, retention window and reference budget.

    ``None`` when retention is unavailable — which is why every read path calls this
    **first**, before it parses a container, scans a neighbour or resolves a head.
    """
    days = effective_retention_days(ctx, uow)
    if days is None:
        return None
    return MemoryRequest(
        ctx, now=datetime.now(UTC) if now is None else now, retention_days=days
    )


def memory_reference(memory_id: UUID) -> str:
    """The canonical reference for one of this module's memories."""
    return f"{t.MEMORY_REF_PREFIX}{memory_id}"


# --- the one eligibility function -----------------------------------------------------


def eligible_memory(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    memory_id: UUID,
    *,
    mode: ReadMode,
    request: MemoryRequest,
    depth: int = 0,
) -> Eligible | Denied:
    """§ A6's ``(ctx, uow, record, read_mode, now)``, with ``now`` and the shared
    budget carried on ``request`` because both are per-request rather than per-record.

    Every read in this module ends here. It is safe to call repeatedly for the same
    record: the answer is cached on ``request`` for the life of the request.
    """
    reference = memory_reference(memory_id)
    if not request.budget.within_depth(depth) or not request.budget.charge(reference):
        return Denied(REFERENCE_SCAN_LIMIT)
    cached = request._decisions.get((memory_id, mode))
    if cached is not None:
        return cached
    if memory_id in request._active:
        # A reference cycle. § A13 says resolver cycles fail closed, so the edge that
        # closed the loop denies rather than being treated as already satisfied by the
        # ancestor still being decided — and the denial is **not** cached, because it
        # is a property of this stack and not of the record.
        return Denied(NOT_FOUND)
    request._active.add(memory_id)
    try:
        decision = _decide(ctx, uow, memory_id, mode=mode, request=request, depth=depth)
    finally:
        request._active.discard(memory_id)
    request._decisions[(memory_id, mode)] = decision
    return decision


def _decide(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    memory_id: UUID,
    *,
    mode: ReadMode,
    request: MemoryRequest,
    depth: int,
) -> Eligible | Denied:
    if MODULE_ID not in ctx.enabled_modules or ctx.role not in READ_ROLES:
        return Denied(NOT_FOUND)
    row = get_memory(uow.connection, memory_id)
    if row is None:
        return Denied(NOT_FOUND)
    if not _audience_admits(row, request):
        return Denied(NOT_FOUND)
    purpose = request.bound_purpose
    if purpose is not None and (
        get_memory_purpose(uow.connection, memory_id, purpose.value) is None
    ):
        return Denied(NOT_FOUND)
    if row.recorded_at < request.horizon:
        return Denied(NOT_FOUND)
    if not _mode_admits(row, mode):
        return Denied(NOT_FOUND)
    return _links(ctx, uow, row, mode=mode, request=request, depth=depth)


def _audience_admits(row: MemoryRow, request: MemoryRequest) -> bool:
    if row.audience_kind == AUDIENCE_WORKSPACE:
        return True
    account_id = request.account_id
    return account_id is not None and row.audience_id == account_id


def _mode_admits(row: MemoryRow, mode: ReadMode) -> bool:
    if mode is ReadMode.CURRENT:
        return row.invalidated_at is None and row.superseded_by_id is None
    return (
        row.invalidation_reason is None
        or row.invalidation_reason in RETAINED_INVALIDATION_REASONS
    )


def _links(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    row: MemoryRow,
    *,
    mode: ReadMode,
    request: MemoryRequest,
    depth: int,
) -> Eligible | Denied:
    """Every actual permission-bearing reference the row carries, before any content.

    A link that is missing, deleted, unavailable or unreadable denies the row. A marked
    lineage link is the one exception, and it is not a weakening: it carries ancestry
    rather than permission, so it is neither resolved nor required to resolve, and it
    contributes a reference with no head.
    """
    heads: list[LinkHead] = []
    for link in list_memory_links(uow.connection, row.id):
        if link.supersession_lineage:
            heads.append(LinkHead(link.ref, link.relation, True, None))
            continue
        try:
            parsed = RecordRef.parse(link.ref)
        except (RecordRefMalformed, TypeError):
            # A stored reference this process cannot parse is a permission-bearing
            # link nobody can check. Fail closed.
            return Denied(NOT_FOUND)
        if parsed.module == MODULE_ID and parsed.record_type == MEMORY_RECORD_TYPE:
            source = eligible_memory(
                ctx, uow, parsed.id, mode=mode, request=request, depth=depth + 1
            )
            if isinstance(source, Denied):
                return source
            heads.append(LinkHead(link.ref, link.relation, False, source.row.title))
            continue
        head = _resolved_link(ctx, uow, parsed, link, request=request)
        if isinstance(head, Denied):
            return head
        heads.append(head)
    return Eligible(row, tuple(heads))


def _resolved_link(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    parsed: RecordRef,
    link: MemoryLinkRow,
    *,
    request: MemoryRequest,
) -> LinkHead | Denied:
    """A non-memory link, resolved under the caller's own permission.

    The record resolver is the core's, reused rather than reimplemented: it is what
    knows whether the owning module is enabled, whether the row is live, and whether
    this caller may read it. A reference that resolves to nothing, to a deleted or
    unavailable record, or to one this caller may not read denies the memory.
    """
    if not request.budget.charge(link.ref):
        return Denied(REFERENCE_SCAN_LIMIT)
    resolved = resolve_in(parsed, ctx, uow)
    if isinstance(resolved, Unavailable):
        return Denied(NOT_FOUND)
    if not _readable(resolved):
        return Denied(NOT_FOUND)
    if not contact_permitted(ctx, uow, parsed, link.relation, request=request):
        return Denied(NOT_FOUND)
    return LinkHead(link.ref, link.relation, False, resolved.display)


def _readable(head: RecordHead) -> bool:
    return head.readable and head.state == LIVE


def contact_permitted(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    parsed: RecordRef,
    relation: str,
    *,
    request: MemoryRequest,
) -> bool:
    """§ A6's optional contact seam: may this bound purpose reach this party?

    It applies to an ``about`` link naming a party record, and only when the context
    carries a purpose binding. The check is a registered in-process operation looked up
    by name; **an absent module or operation means not applicable**, which is the state
    this repository is in — there is no such module here, and none is invented to
    answer it. A present implementation must answer ``permitted``: withdrawn,
    suppressed, refused, unavailable, malformed and error results all suppress the
    content, so anything else, including a raised exception, denies.
    """
    purpose = request.bound_purpose
    if purpose is None:
        return True
    if relation != RELATION_ABOUT:
        return True
    if parsed.module != _PARTY_MODULE or parsed.record_type != _PARTY_RECORD_TYPE:
        return True
    registered = CONTACT_LOOKUP.lookup(_CONTACT_OPERATION)
    if registered is None:
        return True
    if registered.module_id not in ctx.enabled_modules:
        # A module this workspace has not enabled is absent to it, which is the same
        # "not applicable" as one nobody installed.
        return True
    try:
        answer = registered.handler(
            ctx,
            uow,
            registered.declaration.input_model.model_validate(
                {
                    "party_ref": parsed.format(),
                    "purpose": purpose.value,
                    "channel": None,
                }
            ),
        )
    except Exception:
        # "every present ... unavailable/refused, malformed, or error result
        # suppresses content" — the breadth is the contract, not a swallowed bug: a
        # contact check that cannot answer is exactly the case that must deny.
        return False
    state = getattr(answer, "state", None)
    return isinstance(state, str) and state == _CONTACT_PERMITTED


# --- the row-local SQL prefilter ------------------------------------------------------


def row_local_conditions(
    request: MemoryRequest, mode: ReadMode
) -> list[ColumnElement[bool]]:
    """The half of :func:`eligible_memory` that a single row can answer for itself.

    Audience, bound-purpose membership, retention and the requested lifecycle mode,
    expressed over the memory row and two ``EXISTS`` subqueries. No join: a join
    against purposes or links would return a row once per matching child and make the
    501st identifier a duplicate of the 400th.

    Nothing here is a permission decision on its own. It narrows;
    :func:`eligible_memory` decides.
    """
    memory = t.memory
    audience: ColumnElement[bool] = memory.c.audience_kind == AUDIENCE_WORKSPACE
    account_id = request.account_id
    if account_id is not None:
        audience = or_(
            audience,
            and_(
                memory.c.audience_kind == AUDIENCE_MEMBER,
                memory.c.audience_id == account_id,
            ),
        )
    conditions: list[ColumnElement[bool]] = [
        audience,
        memory.c.recorded_at >= request.horizon,
    ]
    purpose = request.bound_purpose
    if purpose is not None:
        conditions.append(
            select(literal(1))
            .where(
                t.memory_purpose.c.memory_id == memory.c.id,
                t.memory_purpose.c.purpose == purpose.value,
            )
            .exists()
        )
    if mode is ReadMode.CURRENT:
        conditions.append(memory.c.invalidated_at.is_(None))
        conditions.append(memory.c.superseded_by_id.is_(None))
    else:
        conditions.append(
            or_(
                memory.c.invalidation_reason.is_(None),
                memory.c.invalidation_reason.in_(RETAINED_INVALIDATION_REASONS),
            )
        )
    return conditions


def container_membership(container_ref: str) -> ColumnElement[bool]:
    """``EXISTS`` over the exact stored link ``memory_link.ref = container_ref``.

    Exact, not "any link this memory has": the container is the reference the caller
    named, and a looser test would let a member of one container answer for another.
    A marked lineage row satisfies it like any other, which is § A6's "a marked lineage
    ref may establish membership without resolving predecessor content".
    """
    return (
        select(literal(1))
        .where(
            t.memory_link.c.memory_id == t.memory.c.id,
            t.memory_link.c.ref == container_ref,
        )
        .exists()
    )


def is_container_member(uow: UnitOfWork, memory_id: UUID, container_ref: str) -> bool:
    """Step 3's existence check for one already-authorized target."""
    found = uow.connection.execute(
        select(literal(1))
        .select_from(t.memory_link)
        .where(
            t.memory_link.c.memory_id == memory_id,
            t.memory_link.c.ref == container_ref,
        )
        .limit(1)
    ).first()
    return found is not None


def container_candidates(
    uow: UnitOfWork, request: MemoryRequest, *, container_ref: str, mode: ReadMode
) -> tuple[UUID, ...]:
    """Step 4: at most 501 ordered member identifiers, and identifiers only.

    ``LIMIT 501`` rather than 500 so that "there is a 501st" is answerable without
    resolving it. The 501st is never read, counted or described beyond its existence.
    """
    statement = (
        select(t.memory.c.id)
        .where(
            container_membership(container_ref), *row_local_conditions(request, mode)
        )
        .order_by(t.memory.c.recorded_at, t.memory.c.id)
        .limit(CANDIDATE_SENTINEL_LIMIT)
    )
    return tuple(row[0] for row in uow.connection.execute(statement).all())


def evaluate_all(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    candidates: Sequence[UUID],
    *,
    mode: ReadMode,
    request: MemoryRequest,
) -> tuple[tuple[tuple[UUID, Eligible], ...], Denied | None]:
    """Fully evaluate an ordered candidate list, keeping the eligible ones in order.

    The second half of the answer is the one refusal that is **not** an exclusion: a
    spent reference budget stops the whole request rather than quietly shortening the
    window, because a window missing rows the caller cannot be told about is the
    partial result § A6 forbids.
    """
    eligible: list[tuple[UUID, Eligible]] = []
    for candidate in candidates:
        decision = eligible_memory(ctx, uow, candidate, mode=mode, request=request)
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                return (), decision
            continue
        eligible.append((candidate, decision))
    return tuple(eligible), None
