"""The trusted source-unit seam: ``accept_source_unit`` and ``retire_source_unit``.

**Two module-internal entry points, and deliberately nothing else.** These are not
operations, not MCP tools, not HTTP endpoints and not a generic ingestion registry.
Nothing registers them, nothing advertises them, and no caller reaches them from
outside this distribution. In 1a1 the only things that call them are the validated
importer and a synthetic authority adapter, both of which live in the test suite: the
two real producer adapters belong to the later automatic-memory carrier and to the
migration run, and building either here would be building a producer this run is not
authorized to build.

**The ordering is the security property, and it is § A5's, in this order:**

1. take the workspace lifecycle lock, so acceptance and retirement of one unit
   serialise against each other and against the deletion coordinator;
2. verify authority — **before** any receipt is looked up, so an unverifiable caller
   cannot learn whether a key exists, and writes no receipt at all, so it cannot
   terminalize somebody else's identity;
3. mint the namespace from the **grant**, never from the unit's own claims, so a unit
   claiming a wider audience lands its outcome in the partition it actually holds;
4. read retention once, through the same function every read uses, and refuse
   ``retention_unavailable`` rather than substituting the package default;
5. check the source window, the purpose binding and the evidence, each writing the
   terminal receipt its failure deserves;
6. look the receipt up, and suppress every non-replayable state with one word;
7. recheck the retention horizon immediately before creation, and terminalize a
   born-expired unit ``noop`` rather than writing a row no read could ever return;
8. create the memory, its active receipt and its ``recorded`` event in one
   transaction.

**Step 7 is the rule this run enforces on behalf of a producer that does not exist
yet.** A source instant already outside the workspace's *current* retention window is
eligible evidence, and a memory written from it would be excluded from every read and
every export the instant it committed. Writing it would be pure audit noise with no
user-visible effect, so acceptance terminalizes ``noop``. The later carrier inherits a
settled rule instead of a silent gap.

**One word for every unreplayable receipt.** A changed digest, a missing or noncurrent
representation, and an erased, expired, noop, denied or orphaned receipt all answer
``source_unavailable``. Telling them apart would report whether a source was once
accepted and then erased, which is exactly the fact an erasure removes.

**This seam never erases a live memory.** For a unit whose representation is still
live, :func:`retire_source_unit` answers ``live_representation`` and writes nothing:
explicit erasure goes through the ordinary per-action-approved deletion operation,
under the original authorized caller, with the capture/execution/coordinator checks
that path already applies. An acceptance lease is not erasure authority.

**No audit row is written here.** The dispatcher is this tree's one audit caller, and
a module that wrote its own audit row would be the thing that makes the log
untrustworthy. A future producer reaches this seam from inside an operation or a
leased job, and *that* call's audit row is the one that covers it.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final
from uuid import UUID

from rheo_contracts import ContextPurpose, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    SourceAuthority,
    SourceMention,
    TrustedSourceUnit,
)
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import (
    AUTOMATIC_BOUND_PURPOSE,
    AUTOMATIC_PRODUCER_KINDS,
)
from rheo_recallatron.eligibility import (
    AUDIENCE_MEMBER,
    AUDIENCE_WORKSPACE,
    Denied,
    MemoryRequest,
    ReadMode,
    begin_request,
    eligible_memory,
    memory_reference,
)
from rheo_recallatron.entities import ResolvedMention, create_entity, readable_ref
from rheo_recallatron.references import canonical_ref, is_memory_ref
from rheo_recallatron.refusals import (
    AUTHORITY_UNVERIFIED,
    RETENTION_UNAVAILABLE,
    SOURCE_UNAVAILABLE,
)
from rheo_recallatron.storage import tables as t
from rheo_recallatron.storage.repository import (
    SourceReceiptRow,
    get_memory,
    get_source_receipt,
    insert_source_receipt,
    set_source_receipt_state,
)
from rheo_recallatron.writes import (
    ORIGIN_DERIVED,
    ProposedLink,
    WriteAudience,
    write_memory,
)

REPRESENTATION_MEMORY: Final = "memory"
"""The one representation type this seam writes. ``entity`` is the other member of
§ A3's vocabulary and belongs to the orphan-pruning path, which writes no receipt in
1a1."""

STATE_ACTIVE: Final = "active"
STATE_NOOP: Final = "noop"
STATE_DENIED: Final = "denied"
STATE_ERASED: Final = "erased"
STATE_EXPIRED: Final = "expired"

LIVE_REPRESENTATION: Final = "live_representation"
"""What retirement answers for a unit whose memory is still live: not a refusal, an
instruction. The caller must dispatch the ordinary approved deletion for that memory
under the original authorized caller; this seam grants no shortcut to it."""


@dataclass(frozen=True, slots=True)
class AcceptOutcome:
    """What acceptance did: a terminal state, and the memory when there is one.

    ``memory_ref`` is set only for ``active`` — a created representation, or the
    currently authorized one an exact-digest replay found. Every other state carries
    ``None``, including ``source_unavailable``, which must not reveal that a
    representation once existed.
    """

    state: str
    memory_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RetireOutcome:
    """What retirement did. ``memory_ref`` is set only for ``live_representation``,
    which is the one state whose whole content is "go and delete this properly"."""

    state: str
    memory_ref: str | None = None


# --- authority and the minted partition -----------------------------------------------


def _verified(
    ctx: WorkspaceContext,
    unit: TrustedSourceUnit,
    authority: SourceAuthority,
    now: datetime,
) -> AuthorityGrant | None:
    """The grant, or ``None`` when this caller's authority cannot be established.

    The identity half is checked here and the *policy* half is not: a unit whose
    producer kind, authority id, originating principal or workspace disagrees with the
    grant is unverifiable — there is no partition to name it in — while a unit whose
    audience, purpose or links exceed what the grant allows is a verified caller
    failing source policy, which earns a ``denied`` receipt in its own partition.
    Collapsing the two would let a caller choose where its denial lands.
    """
    grant = authority.verify(unit, workspace_id=ctx.workspace_id, now=now)
    if isinstance(grant, AuthorityRefused):
        return None
    if grant.workspace_id != ctx.workspace_id:
        return None
    if (
        grant.producer_kind != unit.producer_kind
        or grant.authority_id != unit.authority_id
        or grant.principal_account_id != unit.principal_account_id
    ):
        return None
    return grant


def mint_source_namespace(ctx: WorkspaceContext, grant: AuthorityGrant) -> str:
    """The opaque partition a verified producer writes into.

    Derived from the verified workspace, producer authority, original principal,
    immutable audience ceiling and purpose partition — never chosen by a caller, never
    probeable, and never externally resolvable. A hex digest rather than a readable
    composition, because the composition is the thing that must not be reconstructable
    from a row somebody exported.

    It is minted from the **grant** and not from the unit, which is what stops a unit
    that claims a wider audience from steering its own outcome into a partition it
    does not hold.
    """
    canonical = json.dumps(
        {
            "workspace": str(ctx.workspace_id),
            "producer_kind": grant.producer_kind,
            "authority": str(grant.authority_id),
            "principal": (
                None
                if grant.principal_account_id is None
                else str(grant.principal_account_id)
            ),
            "audience_kind": grant.audience_kind,
            "audience_id": (
                None if grant.audience_id is None else str(grant.audience_id)
            ),
            "purpose": None
            if grant.bound_purpose is None
            else grant.bound_purpose.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


# --- source policy --------------------------------------------------------------------


def _within_ceiling(unit: TrustedSourceUnit, grant: AuthorityGrant) -> bool:
    """The unit's audience is at or below the ceiling the authority verified."""
    if unit.audience_kind == AUDIENCE_MEMBER:
        if unit.audience_id is None or unit.audience_id != unit.principal_account_id:
            return False
        if grant.audience_kind == AUDIENCE_MEMBER:
            return grant.audience_id == unit.audience_id
        return True
    if unit.audience_id is not None:
        return False
    # A workspace audience is wider than a member ceiling, so a member-ceilinged
    # grant cannot admit one.
    return grant.audience_kind == AUDIENCE_WORKSPACE


def _accepted_purposes(unit: TrustedSourceUnit) -> tuple[str, ...] | None:
    """The purposes an accepted memory carries, or ``None`` for a policy failure.

    Automatic acceptance binds exactly ``internal_analysis``, for both producer kinds
    (§ A5). A unit presenting any other or additional purpose refuses before a write —
    which is the whole of AC 4's third sentence, and the reason such a memory is
    invisible to a read bound to anything else.

    Migration is the third producer kind and is not an automatic producer: it may be
    unbound and preserves each imported memory's own ratified purposes, under its
    separately verified import contract.
    """
    if unit.producer_kind in AUTOMATIC_PRODUCER_KINDS:
        if unit.bound_purpose is None or unit.bound_purpose.value != (
            AUTOMATIC_BOUND_PURPOSE
        ):
            return None
        stated = set(unit.evidence.purposes)
        if stated and stated != {ContextPurpose(AUTOMATIC_BOUND_PURPOSE)}:
            return None
        return (AUTOMATIC_BOUND_PURPOSE,)
    if not unit.evidence.purposes:
        return None
    return tuple(sorted({purpose.value for purpose in unit.evidence.purposes}))


def _producer_shape_holds(unit: TrustedSourceUnit) -> bool:
    """§ A3's per-producer field necessity, which the DDL deliberately does not carry.

    An automatic unit needs a verified originating principal *or* — for accountless
    runtime authority — a workspace audience, a bound purpose, and both source
    instants. ``claude_code_local`` additionally requires an account: an enrolled local
    bridge is somebody's machine, and a memory captured there without an account has
    no member ceiling to be held to.
    """
    if unit.producer_kind not in AUTOMATIC_PRODUCER_KINDS:
        return True
    if unit.source_recorded_at is None or unit.source_expires_at is None:
        return False
    if unit.principal_account_id is None:
        if unit.producer_kind == "claude_code_local":
            return False
        return unit.audience_kind == AUDIENCE_WORKSPACE
    return True


def _window_state(unit: TrustedSourceUnit, now: datetime) -> str | None:
    """``None`` when the source window is usable, else the terminal state it earns.

    § A5: ``source_recorded_at <= now < source_expires_at``. Equal or reversed
    endpoints and a future source clock are malformed and earn ``denied``; a window
    that has simply closed earns ``expired``. The two are different facts and the
    receipt records which.
    """
    recorded, expires = unit.source_recorded_at, unit.source_expires_at
    if recorded is None or expires is None:
        # Only migration reaches here; ``_producer_shape_holds`` has already refused
        # an automatic unit missing either instant.
        return None
    if recorded >= expires or recorded > now:
        return STATE_DENIED
    if now >= expires:
        return STATE_EXPIRED
    return None


def _evidence_is_explicit(unit: TrustedSourceUnit) -> bool:
    """Empty, ambiguous or insufficiently explicit evidence yields ``noop`` (§ A5)."""
    return bool(unit.evidence.title.strip()) and bool(unit.evidence.body.strip())


# --- receipts -------------------------------------------------------------------------


def _receipt(
    unit: TrustedSourceUnit,
    grant: AuthorityGrant,
    namespace: str,
    *,
    state: str,
    record_id: UUID | None,
) -> SourceReceiptRow:
    """One identity-only receipt: the key, the digest, the outcome, the authority.

    No source body, label, filename, transcript or session id, no model prompt or
    output, no raw payload and no prior value. The workspace is the routed database,
    so it is not a column either.
    """
    return SourceReceiptRow(
        representation_type=REPRESENTATION_MEMORY,
        source_namespace=namespace,
        external_source_key=unit.external_source_key,
        record_id=record_id,
        payload_digest=unit.payload_digest(),
        state=state,
        producer_kind=grant.producer_kind,
        authority_id=grant.authority_id,
        principal_account_id=grant.principal_account_id,
        audience_kind=unit.audience_kind,
        audience_id=unit.audience_id,
        bound_purpose=(
            None if unit.bound_purpose is None else unit.bound_purpose.value
        ),
        source_recorded_at=unit.source_recorded_at,
        source_expires_at=unit.source_expires_at,
    )


def _terminalize(
    uow: UnitOfWork,
    unit: TrustedSourceUnit,
    grant: AuthorityGrant,
    namespace: str,
    *,
    state: str,
) -> AcceptOutcome:
    """Write the terminal receipt for a unit that produced no memory, and say so."""
    insert_source_receipt(
        uow.connection, _receipt(unit, grant, namespace, state=state, record_id=None)
    )
    return AcceptOutcome(state)


def _replayed(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    unit: TrustedSourceUnit,
    existing: SourceReceiptRow,
    *,
    request: MemoryRequest,
) -> AcceptOutcome:
    """What an already-recorded identity answers, and it performs no write.

    An ``active`` receipt whose digest matches and whose representation is still the
    currently authorized, retained, current row returns that row — with no
    created/existing discriminator, because telling a replay apart from a first
    acceptance is information about what happened before.
    """
    if (
        existing.state != STATE_ACTIVE
        or existing.record_id is None
        or existing.payload_digest != unit.payload_digest()
    ):
        return AcceptOutcome(SOURCE_UNAVAILABLE)
    decision = eligible_memory(
        ctx, uow, existing.record_id, mode=ReadMode.CURRENT, request=request
    )
    if isinstance(decision, Denied):
        return AcceptOutcome(SOURCE_UNAVAILABLE)
    return AcceptOutcome(STATE_ACTIVE, memory_reference(existing.record_id))


# --- the two entry points -------------------------------------------------------------


def _opened(ctx: WorkspaceContext, uow: UnitOfWork, now: datetime) -> MemoryRequest:
    """The one retention read, frozen at ``now``, refusing rather than defaulting."""
    request = begin_request(ctx, uow, now=now)
    if request is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE, "this workspace has no usable retention policy"
        )
    return request


def accept_source_unit(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    unit: TrustedSourceUnit,
    *,
    authority: SourceAuthority,
    consumers: ConsumerRegistry,
    now: datetime | None = None,
) -> AcceptOutcome:
    """Accept one trusted source unit, in the caller's own transaction.

    ``consumers`` is a required keyword rather than read off the unit of work: this is
    not an operation, so no dispatcher hands it a sealed view, and a publish whose
    fan-out depended on whether the caller happened to be inside a dispatch would be
    two different behaviours wearing one name.

    Creation, the active receipt and the ``recorded`` event commit together with
    whatever else the caller's transaction holds — its audit row included, when the
    caller is inside one.
    """
    lock_workspace_lifecycle(uow.connection)
    moment = datetime.now(UTC) if now is None else now
    grant = _verified(ctx, unit, authority, moment)
    if grant is None:
        return AcceptOutcome(AUTHORITY_UNVERIFIED)
    namespace = mint_source_namespace(ctx, grant)
    request = _opened(ctx, uow, moment)

    # The receipt lookup sits **after** authority, which is the ordering § A5 names,
    # and **before** the policy checks, which it does not — because a key that already
    # has an outcome is answered by that outcome, and re-deciding policy for it would
    # try to insert a second receipt over its own primary key.
    existing = get_source_receipt(
        uow.connection, REPRESENTATION_MEMORY, namespace, unit.external_source_key
    )
    if existing is not None:
        return _replayed(ctx, uow, unit, existing, request=request)

    denial = _policy_state(unit, grant, moment)
    if denial is not None:
        return _terminalize(uow, unit, grant, namespace, state=denial)

    recorded_at = unit.source_recorded_at
    if recorded_at is None:
        # Reachable only for a migration unit with no source instants: there is no
        # server clock a preserved row may borrow, because its ``recorded_at`` is the
        # thing the import is supposed to preserve.
        return _terminalize(uow, unit, grant, namespace, state=STATE_DENIED)
    if recorded_at < request.horizon:
        # The acceptance-time recheck, immediately before creation and against the
        # same horizon every read applies. A born-expired row would be invisible the
        # instant it committed, so none is written.
        return _terminalize(uow, unit, grant, namespace, state=STATE_NOOP)

    links = _verified_links(ctx, uow, unit, request=request)
    if links is None:
        return _terminalize(uow, unit, grant, namespace, state=STATE_DENIED)
    purposes = _accepted_purposes(unit)
    assert purposes is not None  # _policy_state refused a None above
    written = write_memory(
        ctx,
        uow,
        kind=unit.evidence.kind,
        title=unit.evidence.title.strip(),
        body=unit.evidence.body,
        audience=WriteAudience(unit.audience_kind, unit.audience_id),
        purposes=purposes,
        confidence=unit.evidence.confidence,
        occurred_at=unit.evidence.occurred_at,
        origin=ORIGIN_DERIVED,
        recorded_at=recorded_at,
        recorded_by_kind=(
            "service" if unit.principal_account_id is None else "account"
        ),
        recorded_by_id=unit.principal_account_id,
        links=links,
        mentions=tuple(
            _mention(uow, mention, recorded_at) for mention in unit.evidence.mentions
        ),
        consumers=consumers,
        source_namespace=namespace,
        external_source_key=unit.external_source_key,
    )
    insert_source_receipt(
        uow.connection,
        _receipt(
            unit,
            grant,
            namespace,
            state=STATE_ACTIVE,
            record_id=canonical_ref(written.ref).id,
        ),
    )
    return AcceptOutcome(STATE_ACTIVE, written.ref)


def _policy_state(
    unit: TrustedSourceUnit, grant: AuthorityGrant, now: datetime
) -> str | None:
    """The terminal state this unit's own policy failures earn, or ``None``.

    Order matters only in what a reader is told, never in what is written: every
    branch here writes exactly one content-free receipt and no memory.
    """
    if not _producer_shape_holds(unit):
        return STATE_DENIED
    if not _within_ceiling(unit, grant):
        return STATE_DENIED
    if unit.bound_purpose != grant.bound_purpose:
        return STATE_DENIED
    if _accepted_purposes(unit) is None:
        return STATE_DENIED
    window = _window_state(unit, now)
    if window is not None:
        return window
    missing = set(grant.required_links) - set(unit.evidence.links)
    if missing:
        return STATE_DENIED
    if not _evidence_is_explicit(unit):
        # Not a denial: nothing was wrong with the caller, there was simply nothing
        # explicit enough to record. A conservative no-op is the ratified outcome.
        return STATE_NOOP
    return None


def _verified_links(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    unit: TrustedSourceUnit,
    *,
    request: MemoryRequest,
) -> tuple[ProposedLink, ...] | None:
    """Every canonical link the unit carries, each re-authorized under this caller.

    ``None`` when any of them does not resolve: a copied restriction that cannot be
    checked is not a restriction, and a memory carrying one would be denied on every
    later read anyway — better to record the denial than to write a row nobody can
    see.
    """
    proposed: list[ProposedLink] = []
    for link in unit.evidence.links:
        try:
            parsed = canonical_ref(link.ref)
        except OperationRefused:
            return None
        if is_memory_ref(parsed):
            decision = eligible_memory(
                ctx, uow, parsed.id, mode=ReadMode.CURRENT, request=request
            )
            if isinstance(decision, Denied):
                return None
        elif not readable_ref(ctx, uow, parsed.format()):
            return None
        proposed.append(ProposedLink(parsed.format(), link.relation))
    return tuple(proposed)


def _mention(uow: UnitOfWork, mention: SourceMention, now: datetime) -> ResolvedMention:
    """One fresh entity per mentioned name, exactly as the manual path creates one.

    A trusted producer never *selects* an entity: selection needs an eligible mention
    or a readable backing ref the producer would have to have probed for, and probing
    is the thing the non-probing rule exists to forbid. So every mention on a unit is
    a creation, and two units naming the same person create two entities.
    """
    row = create_entity(
        uow.connection,
        kind=mention.kind,
        name=mention.name,
        backing_ref=mention.backing_ref,
        now=now,
    )
    return ResolvedMention(
        entity_id=row.id,
        role=mention.role,
        kind=row.kind,
        name=row.name,
        backing_ref=row.ref,
    )


def retire_source_unit(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    unit: TrustedSourceUnit,
    *,
    authority: SourceAuthority,
    now: datetime | None = None,
) -> RetireOutcome:
    """Retire one source unit, writing at most a content-free terminal receipt.

    For a unit with **no live representation** this writes an ``erased`` receipt —
    identity and outcome only — so a later replay of the same key cannot resurrect
    what the source withdrew.

    For a unit whose representation is still live it writes **nothing** and answers
    ``live_representation``. Erasing a live memory is a per-action-approved deletion
    under the original authorized caller; this seam's acceptance lease is not erasure
    authority, and granting itself one here is exactly the bypass § A5 forbids.
    """
    lock_workspace_lifecycle(uow.connection)
    moment = datetime.now(UTC) if now is None else now
    grant = _verified(ctx, unit, authority, moment)
    if grant is None:
        return RetireOutcome(AUTHORITY_UNVERIFIED)
    namespace = mint_source_namespace(ctx, grant)
    existing = get_source_receipt(
        uow.connection, REPRESENTATION_MEMORY, namespace, unit.external_source_key
    )
    if (
        existing is not None
        and existing.state == STATE_ACTIVE
        and existing.record_id is not None
        and get_memory(uow.connection, existing.record_id) is not None
    ):
        return RetireOutcome(LIVE_REPRESENTATION, memory_reference(existing.record_id))
    if existing is None:
        insert_source_receipt(
            uow.connection,
            _receipt(unit, grant, namespace, state=STATE_ERASED, record_id=None),
        )
    else:
        # The original id is retained where there was one: § A3 allows an erased or
        # expired receipt to keep it as a pre-creation tombstone, and dropping it
        # would lose the only evidence that the identity ever had a representation.
        set_source_receipt_state(
            uow.connection,
            REPRESENTATION_MEMORY,
            namespace,
            unit.external_source_key,
            state=STATE_ERASED,
        )
    return RetireOutcome(STATE_ERASED)


_SEAM_RECEIPT_STATES: Final = (
    STATE_ACTIVE,
    STATE_NOOP,
    STATE_DENIED,
    STATE_ERASED,
    STATE_EXPIRED,
)
"""The five states this seam can write. ``orphaned`` is the sixth and is entity-only,
so nothing here produces it."""

if not set(_SEAM_RECEIPT_STATES) <= set(t.RECEIPT_STATES):
    # At import, and raised rather than asserted: the service's vocabulary and the
    # database's check constraint are two spellings of one list, and a divergence
    # only shows up as a failed INSERT on whichever path writes the odd one out.
    raise RuntimeError("a receipt state this seam writes is not in the DDL vocabulary")
