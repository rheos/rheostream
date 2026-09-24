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

**Retention is a check that usually is not there.** Recallatron never auto-forgets a
memory by age (§ A9): unless the workspace has explicitly turned
``retention.expire_by_age`` on, :attr:`MemoryRequest.horizon` is ``None`` and no age
predicate is built at all — not a wide default window, no predicate. Even with the gate
on, the predicate covers ``workspace``-audience rows only; a member-private memory is
outside the mechanism in both states, because the sweep can never remove one and a row
hidden forever with no deletion record is worse than a row kept.

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    RETENTION_EXPIRE_BY_AGE_KEY,
    RETENTION_EXPIRE_BY_AGE_SPEC,
    RetentionPolicy,
)
from rheo_recallatron.references import entity_container
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


LIFECYCLE_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER})
"""§ A10's roles for ``correct``, ``supersede`` and deleting a memory.

Narrower than :data:`READ_ROLES` by exactly one member: ``service`` may read and may
write, and may not change or erase what is already recorded. One name for the three
because § A10's table gives them one list — unlike the read and write sets, which
agree today and are declared apart because the table gives *them* two.

It is also what the manifest's ``delete_roles`` is built from, so "who may delete a
memory" is declared and enforced from one place rather than from a declaration and a
check that happen to agree.
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

    ``now`` and ``retention`` are read once and never re-read: a retention value that
    changed, or a clock that advanced, part-way through one window would make the
    window describe two different moments. The decision cache is keyed by
    ``(memory id, mode)`` so a candidate that is also somebody's source is evaluated
    once, and ``_active`` is the recursion stack that makes a reference cycle fail
    closed instead of recursing.
    """

    __slots__ = ("ctx", "now", "retention", "budget", "_decisions", "_active")

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        now: datetime,
        retention: RetentionPolicy,
        budget: ReferenceBudget | None = None,
    ) -> None:
        self.ctx = ctx
        self.now = now
        self.retention = retention
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
    def horizon(self) -> datetime | None:
        """The oldest ``recorded_at`` still readable, or ``None`` for no horizon.

        ``None`` is the default posture (§ A9): this workspace has not turned
        age-based expiry on, so no age predicate exists to apply. Every caller that
        builds one omits it entirely rather than substituting a default window.

        When there is a horizon, equality is retained (§ A9), so every comparison
        against it is ``>=`` for kept and ``<`` for expired.
        """
        return self.retention.horizon(self.now)


def expire_by_age_enabled(ctx: WorkspaceContext, uow: UnitOfWork) -> bool:
    """Does this workspace delete memories by age at all? (§ A9's gate.)

    **Absent or unparseable means ``false``**, which is this key's own rule and the
    exact opposite of the window's below. Falling back to the package default here is
    the *retaining* direction, so it is safe; falling back for the window would widen
    a policy at the moment it went missing, so it is not. A workspace enabled before
    this key existed therefore holds no row, resolves ``false``, and needs no backfill.

    Read through the **transaction-bound** override source on the caller's own
    connection, exactly as :func:`effective_retention` reads the window — never through
    the process-wide layered resolver, which would open a second connection and could
    disagree with the value this transaction sees.
    """
    return expire_by_age_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )


def expire_by_age_in(stored_rows: Mapping[str, str]) -> bool:
    """§ A9's gate, decided from override rows somebody else already read.

    The same rule as :func:`expire_by_age_enabled`, taking the rows rather than the
    context and unit of work that produce them. The export collector is the caller
    that needs this shape: A12 hands it a settings source already bound to the source
    snapshot, so it holds the rows and has no ``WorkspaceContext`` to rebuild one
    from — and reopening a source of its own is exactly the second view that
    ``TransactionBoundOverrideSource`` exists to prevent.
    """
    stored = stored_rows.get(RETENTION_EXPIRE_BY_AGE_KEY)
    if stored is None:
        return False
    try:
        value = decode_text(
            RETENTION_EXPIRE_BY_AGE_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        return False
    return value is True


def retention_in(stored_rows: Mapping[str, str]) -> RetentionPolicy | None:
    """:func:`effective_retention`'s three answers, from rows already read.

    Same three answers, same asymmetry between the gate and the window, and the same
    refusal to substitute the package default for a window row that should be there
    and is not. See :func:`expire_by_age_in` for why the rows arrive rather than the
    connection.
    """
    if not expire_by_age_in(stored_rows):
        return RetentionPolicy.unbounded()
    stored = stored_rows.get(RETENTION_DAYS_KEY)
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
    return RetentionPolicy.of_days(value)


def effective_retention(
    ctx: WorkspaceContext, uow: UnitOfWork
) -> RetentionPolicy | None:
    """This workspace's retention posture, or ``None`` when it is unusable.

    Three answers, and the third is why this is not an ``int | None`` any more:

    - :meth:`~rheo_recallatron.configuration.RetentionPolicy.unbounded` — the gate is
      off, which is the package default. There is no horizon, so no read, write,
      acceptance or sweep applies an age predicate. The window row is **not read at
      all** in this state, so a missing or corrupt one cannot refuse anything.
    - :meth:`~rheo_recallatron.configuration.RetentionPolicy.of_days` — the gate is on
      and the stored window parses inside its declared range.
    - ``None`` — AC 8's ``retention_unavailable``, reachable only while the gate is on:
      the window row is absent, will not parse as an integer, or is outside the
      declared 1-3650 range. The caller decides how to say so — an operation raises the
      refusal, the record resolver answers ``Unavailable`` — but no caller may carry
      on, and **none substitutes the package default**. The settings resolver's own
      rule is to drop a bad stored value and log it, which is right for a setting whose
      default is a safe answer and wrong for this one: silently restoring 365 days is
      how a workspace that meant to keep thirty ends up serving a year of content the
      moment its policy row goes missing.

    Both rows are read on the caller's own connection through the transaction-bound
    override source, so the values are the ones **this** transaction sees — the same
    read a write's acceptance recheck and the sweep's own locked read will make.
    """
    return retention_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )


def begin_request(
    ctx: WorkspaceContext, uow: UnitOfWork, *, now: datetime | None = None
) -> MemoryRequest | None:
    """Freeze this request's clock, retention posture and reference budget.

    ``None`` when retention is unavailable — which is why every read path calls this
    **first**, before it parses a container, scans a neighbour or resolves a head. With
    the gate off that answer is unreachable: the posture is simply unbounded.
    """
    retention = effective_retention(ctx, uow)
    if retention is None:
        return None
    return MemoryRequest(
        ctx, now=datetime.now(UTC) if now is None else now, retention=retention
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
    if age_expired(row, request):
        return Denied(NOT_FOUND)
    if not _mode_admits(row, mode):
        return Denied(NOT_FOUND)
    return _links(ctx, uow, row, mode=mode, request=request, depth=depth)


def _audience_admits(row: MemoryRow, request: MemoryRequest) -> bool:
    return audience_admits(row, request.account_id)


def subject_to_age(row: MemoryRow) -> bool:
    """Is this row inside the age mechanism at all? (§ A9's member exemption.)

    Only ``workspace``-audience rows are. A ``member``-audience memory is never
    selected by the sweep, never age-filtered on read, and never counted by export's
    unswept-content refusal — in either gate state.

    **The read half is what makes the exemption coherent rather than merely kind.**
    Two independent reasons agree that the *sweep* cannot take a member row: it runs
    under an accountless SYSTEM context that could not authorize the deletion of
    member-private content, and a workspace operator's data-minimization setting is
    not authority over one member's own record — the ordinary approved deletion path
    already covers that. Given the sweep will never remove it, age-*filtering* it on
    read would strand it permanently unreadable, with no deletion record and no route
    back except turning the gate off. Hiding a row forever without removing it is
    worse than either alternative.

    One predicate, named once, so the SQL prefilter below and
    :func:`eligible_memory` cannot come to disagree about which rows the mechanism
    covers.
    """
    return row.audience_kind == AUDIENCE_WORKSPACE


def age_expired(row: MemoryRow, request: MemoryRequest) -> bool:
    """Is this row past a horizon that actually applies to it?

    ``False`` whenever there is no horizon (the gate is off) or the row sits outside
    the mechanism (member audience). Those are not "passed the check"; they are "there
    was no check", which is why this is one function rather than a comparison against
    a substituted default.
    """
    horizon = request.horizon
    if horizon is None or not subject_to_age(row):
        return False
    return row.recorded_at < horizon


def audience_admits(row: MemoryRow, account_id: UUID | None) -> bool:
    """§ A4's audience test over one row, for one authenticated account.

    Taken as the bare account id rather than as a :class:`MemoryRequest` so the
    deletion authorizer below can ask it too: that path has no request to build,
    because it reads no retention window and resolves no content. Keeping it here is
    what keeps this file the only one in the package that compares an audience.
    """
    if row.audience_kind == AUDIENCE_WORKSPACE:
        return True
    return account_id is not None and row.audience_id == account_id


def deletion_admits(ctx: WorkspaceContext, uow: UnitOfWork, row: MemoryRow) -> bool:
    """§ A7's content-free deletion check: module, role, audience, bound purpose.

    **Deliberately not :func:`eligible_memory`**, and the three differences are the
    whole reason this function exists rather than a ``mode=`` on that one:

    - **No retention horizon.** An expired row is the one the scheduled sweep
      exists to erase, so a deletion authorizer that applied the horizon would
      refuse exactly the rows retention wants gone.
    - **No lifecycle mode.** A retained ``source_corrected`` or
      ``source_superseded`` row is still somebody's to forget.
    - **No link resolution.** Resolving a row's links is how a *read* decides
      whether its content may travel; a deletion returns no content, and a memory
      whose linked record has since been deleted must not become unerasable because
      of it.

    What it keeps is every rule that says *whose* record this is: the module has to
    be enabled here, the caller has to hold one of § A10's lifecycle roles, the
    row's audience has to admit the caller, and a bound context has to be inside the
    row's purpose set. It answers a bare boolean and never the row, which is what
    makes the authorizer built on it content-free by construction.

    **The owner passes the audience check for a member-audience memory, and reads
    none of it** (§ A8). Erasure authority and read authority are distinct, and this
    is the point of a content-free authorizer: the owner authorizes, the coordinator
    removes, and nothing in the path returns a title, a body, a count or even
    confirmation of who the member is. § A9's retention exemption is what makes it
    necessary rather than merely convenient — a member-audience memory is never
    swept, so approved deletion is its *only* removal path, and requiring the
    member's own account would leave a departed member's memories unremovable by
    anyone: an unbounded retention obligation with no operator remedy. The member
    still erases their own directly; the owner's route exists so the workspace is
    never stuck, and it stays per-action approved and audited like every other
    destructive one.
    """
    if MODULE_ID not in ctx.enabled_modules or ctx.role not in LIFECYCLE_ROLES:
        return False
    if ctx.role is not Role.OWNER and not audience_admits(
        row, ctx.principal.account_id
    ):
        return False
    purpose = ctx.principal.bound_purpose
    return purpose is None or (
        get_memory_purpose(uow.connection, row.id, purpose.value) is not None
    )


def expiry_admits(
    ctx: WorkspaceContext, uow: UnitOfWork, row: MemoryRow
) -> bool | None:
    """§ A9's scheduled-expiry check: is this row the sweep's to remove, right now?

    The counterpart of :func:`deletion_admits` for the one deletion path with no
    person behind it, and the differences are the whole reason it is a second function
    rather than a flag on that one.

    **It asks nothing about the caller's identity.** No role, no bound purpose, and no
    audience *membership*. A retention window is the workspace's stated policy rather
    than a person's reach, and the sweep has no account and never will. What stands in
    for those checks is the sealed scheduled execution the core verified against the
    live transaction before it ever reached this module — see
    :class:`~rheo_core.deletion.registry.Disposition`.

    **It does ask which rows the mechanism covers**, which is not the same question.
    § A9 puts ``member``-audience memories outside age-based expiry entirely, so
    :func:`subject_to_age` refuses one here however old it is: the accountless sweep
    could not authorize erasing member-private content anyway, and an operator's
    data-minimization setting is not authority over one member's own record. Approved
    deletion — which an owner may authorize content-free, see :func:`deletion_admits`
    — is that row's removal path.

    **And it asks the one thing a caller check never does**: whether the row is
    actually past a horizon that applies, re-read here under the workspace lifecycle
    lock the core wrapper already holds. That is § A9's "locks and rechecks target
    expiry immediately before deletion", and it has to live in this module because
    core knows nothing about which column carries a memory's clock.

    ``False`` when the gate is off: there is no horizon, so nothing is the sweep's.
    ``None`` is AC 8's ``retention_unavailable``, reachable only while the gate is on:
    the window row is missing, unparseable or out of range, and a sweep that carried
    on would be deleting against a window nobody stated. Fail closed, exactly as every
    read does.
    """
    if MODULE_ID not in ctx.enabled_modules:
        return False
    if not subject_to_age(row):
        return False
    request = begin_request(ctx, uow)
    if request is None:
        return None
    # ``<``, not ``<=``: § A9 retains the boundary instant. A memory recorded exactly
    # ``days`` ago is still readable, so it is not the sweep's yet either — the two
    # comparisons are the same one, and :func:`age_expired` is where it lives.
    return age_expired(row, request)


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
    conditions: list[ColumnElement[bool]] = [audience]
    horizon = request.horizon
    if horizon is not None:
        # The gate is on, so an age predicate exists — and it covers workspace-audience
        # rows only (§ A9). A member row is admitted by age unconditionally, which is
        # :func:`subject_to_age`'s rule expressed in SQL; the two must agree, or the
        # prefilter would drop a row :func:`eligible_memory` would have allowed and no
        # caller could ever see it.
        conditions.append(
            or_(
                memory.c.audience_kind != AUDIENCE_WORKSPACE,
                memory.c.recorded_at >= horizon,
            )
        )
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
    """Which memories belong to the container the caller named, as one ``EXISTS``.

    **Two rules, chosen by the container's own shape.** An entity reference
    (``recallatron.memory_entity:<uuid>``) is never a ``memory_link`` row — mentions
    live in ``memory_mention`` — so its members are the memories with a mention row
    naming it. Every other container keeps the exact stored link
    ``memory_link.ref = container_ref``: exact, not "any link this memory has",
    because a looser test would let a member of one container answer for another. A
    marked lineage row satisfies it like any other, which is § A6's "a marked lineage
    ref may establish membership without resolving predecessor content".

    Membership is only ever a narrowing. Whether the entity itself may be seen at all
    is the read handler's question, asked before this one.
    """
    entity_id = entity_container(container_ref)
    if entity_id is not None:
        return (
            select(literal(1))
            .where(
                t.memory_mention.c.memory_id == t.memory.c.id,
                t.memory_mention.c.entity_id == entity_id,
            )
            .exists()
        )
    return (
        select(literal(1))
        .where(
            t.memory_link.c.memory_id == t.memory.c.id,
            t.memory_link.c.ref == container_ref,
        )
        .exists()
    )


def is_container_member(uow: UnitOfWork, memory_id: UUID, container_ref: str) -> bool:
    """Step 3's existence check for one already-authorized target, by the same two
    rules as :func:`container_membership`."""
    entity_id = entity_container(container_ref)
    if entity_id is not None:
        statement = select(literal(1)).where(
            t.memory_mention.c.memory_id == memory_id,
            t.memory_mention.c.entity_id == entity_id,
        )
    else:
        statement = select(literal(1)).where(
            t.memory_link.c.memory_id == memory_id,
            t.memory_link.c.ref == container_ref,
        )
    return uow.connection.execute(statement.limit(1)).first() is not None


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
