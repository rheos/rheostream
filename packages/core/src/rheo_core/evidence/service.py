"""The eligible-evidence service: the one thing Recallatron imports from core's evidence
package (spec § Architecture, System Components item 8; FR 2, 5, 6, 9, 10; R9, R11,
R14).

Three functions. :func:`claim_units` takes one bounded, single-partition batch of
``pending`` evidence, checks each unit's authority before any model call, runs the one
extraction call, and hands back fully built :class:`ClaimedUnit` values.
:func:`settle_unit` records what the accepting module did with one of them, writing the
success audit row first when a memory was created. :func:`defer_unit` counts an
attempt on one whose acceptance the database refused, so a poison unit backs off like
an extraction failure instead of jamming the queue (#215). None of them commits: all
run in the drain job's own transaction, so memory, receipt, event, audit row and
settlement land together or not at all.

**Two words used throughout.** *Claimable* means ``state = 'pending' AND (retry_after
IS NULL OR retry_after <= now)``. *Waiting* means ``state = 'pending' AND retry_after >
now``: a row backed off after a failed extraction. A claimable row another transaction
has locked is still claimable, never waiting; ``SKIP LOCKED`` is what hides it from
this claim.

**The workspace comes from the connection.** A job handler is handed no workspace id,
and no workspace-database table stores one. :func:`_routed_workspace_id` reads
``current_database()``, inverts it with
:func:`~rheo_core.storage.provisioning.workspace_id_for_database`, and checks the
routing row names that same database; anything else raises
:class:`EvidenceWorkspaceUnresolved` before a row is read or written.

**An extraction failure stays in its partition (R11).** A provider raise, a malformed
response, or a provider that no longer resolves is caught here: every unit in the batch
counts the attempt and backs off on the job substrate's schedule, or settles
``gap/extraction_failed`` on the fifth, and the claim returns no units. The drain job
then succeeds, so the counts commit, and because ``extraction_attempts`` leads the
anchor order the next drain takes another partition first.

**Workspace-wide acceptance checks run before the model call (#217).** A claim's
extraction commits only with the drain that runs it, so a fault that rolls the whole
drain back (no audit sink for the accepting module, no usable retention policy) would
otherwise call the provider again on every retry with the same evidence. The accepting
module passes ``before_extraction``, and the claim calls it with the acceptance
context after step 3 whenever units survive it, before step 4 resolves the provider
(so it runs even when no provider resolves). What it raises leaves
:func:`claim_units` uncontained: no provider call is made, and the drain rolls back
and retries as it would have after the call.

**Content-free.** No exception, log line or audit row here carries evidence text, a
native key or anything a provider said.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityRefused,
    SanitizedEvidence,
    SourceAuthority,
    TrustedSourceUnit,
)
from sqlalchemy import ColumnElement, and_, func, or_, select, text, update

from rheo_core.boundary import (
    MEMBERSHIP_MISSING,
    Refusal,
    context_for_evidence_acceptance,
)
from rheo_core.evidence.authority import RuntimeEvidenceAuthority
from rheo_core.evidence.extract import NOOP_EVIDENCE, digest, validate_extraction
from rheo_core.evidence.providers import resolve_provider
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.backend import HandlerUnitOfWork, StorageRefusal, UnitOfWork
from rheo_core.storage.evidence_tables import (
    OUTCOME_ACCEPTANCE_FAILED,
    OUTCOME_ACTIVE,
    OUTCOME_AUTHORITY_UNVERIFIED,
    OUTCOME_EXPIRED_PENDING,
    OUTCOME_EXTRACTION_FAILED,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_core.storage.provisioning import workspace_id_for_database
from rheo_core.storage.routing import active_workspace
from rheo_core.work.backoff import backoff_for

logger = logging.getLogger("rheo_core.evidence")

EXTRACTION_MAX_ATTEMPTS: Final = 5
"""The attempt that settles a unit ``gap/extraction_failed`` instead of backing off.

Only the first four back-off steps are ever used: 5, 20, 80 and 320 seconds."""

MAX_UNITS_PER_JOB_KEY: Final = "automatic_memory.max_units_per_job"

EXTRACTION_FAILED_LOG: Final = "evidence_extraction_failed"


class EvidenceWorkspaceUnresolved(Exception):
    """This connection's database is not one active workspace's own. Private to this
    module: the drain job sees only that its handler raised, and retries."""


class EvidenceClaimInconsistent(Exception):
    """An invariant of the claim did not hold; nothing about the rows is named."""


class EvidenceAuditUnwritable(Exception):
    """An ``active`` settlement could not write its success audit row. Raised before
    the settlement, so the caller's transaction rolls every write of it back."""


class _ExtractionProviderMissing(Exception):
    """No provider resolves at claim time: the key changed after recording. Handled
    exactly like a provider raise."""


@dataclass(frozen=True, slots=True)
class ClaimedUnit:
    """One claimed unit, fully built by core, and the context and authority the
    accepting module calls 1a1's seam with."""

    evidence_id: UUID
    context: WorkspaceContext
    unit: TrustedSourceUnit
    authority: SourceAuthority


@dataclass(frozen=True, slots=True)
class ClaimedBatch:
    """One claim's units, and when the next drain should run (``None``: no need)."""

    units: tuple[ClaimedUnit, ...]
    follow_up_at: datetime | None


def claimable(now: datetime) -> ColumnElement[bool]:
    """*Claimable*, as the module docstring defines it. Not on the package's public
    surface; core's recovery check (``record.republish_for_stalled_drain``) reads it
    so the two never disagree."""
    return and_(
        evidence_unit.c.state == STATE_PENDING,
        or_(evidence_unit.c.retry_after.is_(None), evidence_unit.c.retry_after <= now),
    )


def _waiting(now: datetime) -> ColumnElement[bool]:
    return and_(
        evidence_unit.c.state == STATE_PENDING,
        evidence_unit.c.retry_after > now,
    )


def _routed_workspace_id(uow: UnitOfWork) -> UUID:
    """The workspace whose own database ``uow`` is open on, or
    :class:`EvidenceWorkspaceUnresolved`."""
    name = uow.connection.execute(text("SELECT current_database()")).scalar_one()
    workspace_id = workspace_id_for_database(name)
    if workspace_id is None:
        raise EvidenceWorkspaceUnresolved("the database is not a workspace database")
    try:
        row = active_workspace(workspace_id)
    except StorageRefusal:
        raise EvidenceWorkspaceUnresolved("the workspace is not active") from None
    if row.database_name != name:
        raise EvidenceWorkspaceUnresolved(
            "the routing row names another database than this connection's"
        )
    return workspace_id


def _settle_row(
    uow: UnitOfWork, evidence_id: UUID, *, state: str, outcome: str, now: datetime
) -> None:
    """Move one ``pending`` row to ``state``/``outcome``, clearing its body and
    stamping ``settled_at``: the one place a claimed row leaves ``pending``."""
    moved = uow.connection.execute(
        update(evidence_unit)
        .where(
            evidence_unit.c.id == evidence_id,
            evidence_unit.c.state == STATE_PENDING,
        )
        .values(state=state, outcome=outcome, body=None, settled_at=now)
    ).rowcount
    if moved != 1:
        raise EvidenceClaimInconsistent("a claimed evidence row was no longer pending")


def _age_expired(uow: UnitOfWork, *, now: datetime) -> None:
    """Step 1: every expired ``pending`` row this transaction can lock becomes
    ``gap/expired_pending``; a row another drain holds is skipped, never waited on."""
    expired = (
        select(evidence_unit.c.id)
        .where(
            evidence_unit.c.state == STATE_PENDING,
            evidence_unit.c.source_expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    uow.connection.execute(
        update(evidence_unit)
        .where(evidence_unit.c.id.in_(expired))
        .values(
            state=STATE_GAP,
            outcome=OUTCOME_EXPIRED_PENDING,
            body=None,
            settled_at=now,
        )
    )


# The claim order, in both the anchor and the partition query. ``id`` last only breaks
# ties, so the anchor is always the partition's first row and the result of the
# ``LIMIT n + 1`` read always holds it.
def _claim_order() -> tuple[Any, ...]:
    return (
        evidence_unit.c.extraction_attempts,
        evidence_unit.c.source_recorded_at,
        evidence_unit.c.id,
    )


def _anchor(uow: UnitOfWork, *, now: datetime) -> Any:
    """Step 2's anchor: the first claimable row nobody else holds, locked."""
    return uow.connection.execute(
        select(
            evidence_unit.c.id,
            evidence_unit.c.speaker_account_id,
            evidence_unit.c.audience_kind,
            evidence_unit.c.audience_id,
            evidence_unit.c.purpose,
        )
        .where(claimable(now))
        .order_by(*_claim_order())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).one_or_none()


def _partition_rows(
    uow: UnitOfWork, anchor: Any, *, now: datetime, limit: int
) -> Sequence[Any]:
    """Up to ``limit`` claimable rows of the anchor's partition, locked.

    ``IS NOT DISTINCT FROM`` on all four columns: a workspace ceiling has a null
    ``audience_id``, and ``=`` never matches null.
    """
    return uow.connection.execute(
        select(
            evidence_unit.c.id,
            evidence_unit.c.producer_kind,
            evidence_unit.c.authority_id,
            evidence_unit.c.native_key,
            evidence_unit.c.speaker_account_id,
            evidence_unit.c.audience_kind,
            evidence_unit.c.audience_id,
            evidence_unit.c.purpose,
            evidence_unit.c.source_recorded_at,
            evidence_unit.c.source_expires_at,
            evidence_unit.c.body,
            evidence_unit.c.extraction_attempts,
        )
        .where(
            claimable(now),
            evidence_unit.c.speaker_account_id.is_not_distinct_from(
                anchor.speaker_account_id
            ),
            evidence_unit.c.audience_kind.is_not_distinct_from(anchor.audience_kind),
            evidence_unit.c.audience_id.is_not_distinct_from(anchor.audience_id),
            evidence_unit.c.purpose.is_not_distinct_from(anchor.purpose),
        )
        .order_by(*_claim_order())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()


def _unit(row: Any, evidence: SanitizedEvidence) -> TrustedSourceUnit:
    """The row as a unit, every authority field core's, ``evidence`` the only input."""
    return TrustedSourceUnit(
        producer_kind=row.producer_kind,
        authority_id=row.authority_id,
        principal_account_id=row.speaker_account_id,
        audience_kind=row.audience_kind,
        audience_id=row.audience_id,
        bound_purpose=ContextPurpose(row.purpose),
        source_recorded_at=row.source_recorded_at,
        source_expires_at=row.source_expires_at,
        external_source_key=row.native_key,
        evidence=evidence,
    )


def _earliest_waiting(
    uow: UnitOfWork, *, now: datetime, lockable_only: bool
) -> datetime | None:
    """The earliest ``retry_after`` among waiting rows, or ``None``.

    ``lockable_only`` leaves out rows another transaction holds (``FOR KEY SHARE SKIP
    LOCKED``, the weakest lock a ``FOR UPDATE`` holder still blocks), and locks only
    the one row it answers from: an empty claim never schedules a follow-up from rows
    a drain holding them schedules for itself.
    """
    if not lockable_only:
        earliest: datetime | None = uow.connection.execute(
            select(func.min(evidence_unit.c.retry_after)).where(_waiting(now))
        ).scalar_one()
        return earliest
    first: datetime | None = uow.connection.execute(
        select(evidence_unit.c.retry_after)
        .where(_waiting(now))
        .order_by(evidence_unit.c.retry_after)
        .limit(1)
        .with_for_update(key_share=True, skip_locked=True)
    ).scalar_one_or_none()
    return first


def _follow_up_at(
    uow: UnitOfWork, *, now: datetime, taken: Sequence[UUID], more: bool
) -> datetime | None:
    """Step 5: ``now`` while claimable work remains outside this claim, else the
    earliest waiting row's ``retry_after`` (this claim's backed-off rows included),
    else ``None``. The claimable read takes no lock, so a row another drain holds
    counts; that costs at most one extra empty drain."""
    if more:
        return now
    other = uow.connection.execute(
        select(evidence_unit.c.id)
        .where(claimable(now), evidence_unit.c.id.not_in(taken))
        .limit(1)
    ).first()
    if other is not None:
        return now
    return _earliest_waiting(uow, now=now, lockable_only=False)


def _back_off(
    uow: UnitOfWork, rows: Sequence[Any], *, final_outcome: str, now: datetime
) -> None:
    """Count the attempt on every row; back each off, or settle it
    ``gap/<final_outcome>`` on the last attempt. The row stays ``pending`` while it
    backs off, so ``settled_at`` stays null. ``rows`` need only ``id`` and
    ``extraction_attempts``."""
    for row in rows:
        attempts = row.extraction_attempts + 1
        values: dict[str, object] = {"extraction_attempts": attempts}
        if attempts < EXTRACTION_MAX_ATTEMPTS:
            values["retry_after"] = now + backoff_for(attempts, jitter=None)
        uow.connection.execute(
            update(evidence_unit).where(evidence_unit.c.id == row.id).values(**values)
        )
        if attempts >= EXTRACTION_MAX_ATTEMPTS:
            _settle_row(
                uow,
                row.id,
                state=STATE_GAP,
                outcome=final_outcome,
                now=now,
            )


def claim_units(
    uow: HandlerUnitOfWork,
    *,
    now: datetime,
    before_extraction: Callable[[WorkspaceContext], None] | None = None,
    mention_kinds: Sequence[str] = (),
) -> ClaimedBatch:
    """Claim one partition of pending evidence on this workspace's own database.

    Steps, in order: resolve the workspace from the connection (0); age expired
    ``pending`` rows without waiting (1); anchor on the first claimable row and lock
    up to ``automatic_memory.max_units_per_job`` of its ``(speaker, audience kind,
    audience id, purpose)`` partition, plus one row that only signals more (2); settle
    every row with no acceptance context or a refused authority pre-check
    ``settled/authority_unverified`` before any model call (3); run one extraction
    over what remains, containing any failure to this partition (4); and return the
    surviving units with the follow-up instant (5).

    ``before_extraction``, when given, is called with the acceptance context between
    steps 3 and 4 whenever step 3 leaves units to extract, before the provider is
    resolved, so it runs even when none resolves. It checks what the accepting
    module's acceptance needs regardless of the unit, and raises when that is
    missing; the raise is not contained (#217).

    ``mention_kinds`` is the entity-kind vocabulary the accepting module keeps. It is
    handed to the provider on the batch, unread by core, so the model can be told the
    allowed kinds (#222).
    """
    workspace_id = _routed_workspace_id(uow)
    _age_expired(uow, now=now)

    anchor = _anchor(uow, now=now)
    if anchor is None:
        return ClaimedBatch(
            units=(),
            follow_up_at=_earliest_waiting(uow, now=now, lockable_only=True),
        )

    settings = resolve(
        workspace_id=workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=workspace_id),
    )
    max_units = settings.get_int(MAX_UNITS_PER_JOB_KEY)
    rows = _partition_rows(uow, anchor, now=now, limit=max_units + 1)
    if anchor.id not in {row.id for row in rows}:
        raise EvidenceClaimInconsistent("the partition read did not return its anchor")
    more = len(rows) > max_units
    rows = rows[:max_units]
    taken = [row.id for row in rows]

    # Step 3. Every row of one partition shares its speaker and purpose, so one
    # acceptance context serves the whole claim, and its membership_missing refusal
    # (the speaker is no longer a member) leaves no row with a context. Any other
    # refusal (the workspace became unavailable) says nothing about these rows, so
    # it raises and leaves them pending for the job's retry rather than settling
    # them for good. The pre-check then runs per row on a placeholder unit: the
    # authority reads only the unit's identity fields.
    context = context_for_evidence_acceptance(
        workspace_id,
        account_id=anchor.speaker_account_id,
        purpose=ContextPurpose(anchor.purpose),
    )
    if isinstance(context, Refusal) and context.state != MEMBERSHIP_MISSING:
        raise EvidenceWorkspaceUnresolved("no acceptance context for the workspace")
    authority = RuntimeEvidenceAuthority(uow)
    verified: list[Any] = []
    for row in rows:
        if isinstance(context, WorkspaceContext) and not isinstance(
            authority.verify(
                _unit(row, NOOP_EVIDENCE), workspace_id=workspace_id, now=now
            ),
            AuthorityRefused,
        ):
            verified.append(row)
            continue
        _settle_row(
            uow,
            row.id,
            state=STATE_SETTLED,
            outcome=OUTCOME_AUTHORITY_UNVERIFIED,
            now=now,
        )

    if isinstance(context, Refusal) or not verified:
        return ClaimedBatch(
            units=(), follow_up_at=_follow_up_at(uow, now=now, taken=taken, more=more)
        )

    # #217: a workspace-wide acceptance fault raises here, before the provider is
    # resolved or called, rather than after it in a drain that rolls the extraction
    # back.
    if before_extraction is not None:
        before_extraction(context)

    # Step 4. The provider never touches the database, so the transaction is still
    # healthy after any exception it raises; the failure path's writes must commit.
    batch = digest([row.body for row in verified], mention_kinds=mention_kinds)
    failure: str | None = None
    candidates: dict[str, SanitizedEvidence] = {}
    try:
        provider = resolve_provider()
        if provider is None:
            raise _ExtractionProviderMissing
        candidates = validate_extraction(batch, provider.extract(batch))
    except Exception as exc:  # any provider fault is contained to this partition
        failure = type(exc).__name__
    if failure is not None:
        _back_off(uow, verified, final_outcome=OUTCOME_EXTRACTION_FAILED, now=now)
        # The type name and a count only: a provider may fill a message with text.
        logger.warning(
            EXTRACTION_FAILED_LOG,
            extra={"error_type": failure, "unit_count": len(verified)},
        )
        return ClaimedBatch(
            units=(), follow_up_at=_follow_up_at(uow, now=now, taken=taken, more=more)
        )

    # Step 5.
    units = tuple(
        ClaimedUnit(
            evidence_id=row.id,
            context=context,
            unit=_unit(row, candidates[item.item_id]),
            authority=authority,
        )
        for row, item in zip(verified, batch.items, strict=True)
    )
    return ClaimedBatch(
        units=units, follow_up_at=_follow_up_at(uow, now=now, taken=taken, more=more)
    )


def settle_unit(
    uow: HandlerUnitOfWork,
    claimed: ClaimedUnit,
    *,
    outcome: str,
    memory_ref: str | None,
    audit_module_id: str,
    now: datetime,
) -> None:
    """Settle ``claimed``'s row with the seam's ``outcome``, in the caller's
    transaction.

    For ``active``, first the success audit row, through ``audit_module_id``'s sink;
    no sink raises :class:`EvidenceAuditUnwritable` before the row settles, so the
    caller's rollback takes the memory, receipt, event and settlement with it. An
    ``active`` outcome without a ``memory_ref`` is an invariant violation and raises.
    Every other outcome writes no audit row.
    """
    if outcome == OUTCOME_ACTIVE:
        if memory_ref is None:
            raise ValueError("an active settlement needs the created memory's ref")
        # Deferred, the approvals/gate.py direction-preserving pattern: dispatch
        # reaches far into core, and this package must import cold without it.
        from rheo_core.operations.dispatch import record_evidence_acceptance_audit

        if not record_evidence_acceptance_audit(
            claimed.context,
            uow,
            module_id=audit_module_id,
            subject_ref=RecordRef.parse(memory_ref),
            request_digest=claimed.unit.payload_digest(),
        ):
            raise EvidenceAuditUnwritable("no audit sink for the accepting module")
    _settle_row(uow, claimed.evidence_id, state=STATE_SETTLED, outcome=outcome, now=now)


def defer_unit(uow: HandlerUnitOfWork, claimed: ClaimedUnit, *, now: datetime) -> None:
    """Count a failed acceptance of ``claimed`` as one attempt, in the caller's
    transaction.

    For a unit whose acceptance the database refused (a constraint or a bad value):
    the caller has rolled its own savepoint back, so no memory was written, and this
    backs the row off on the extraction schedule, or settles it
    ``gap/acceptance_failed`` on the fifth attempt. Without it a poison unit would
    roll back the whole drain with ``extraction_attempts`` still 0 and anchor every
    later claim (R11).

    The attempt count is re-read rather than taken from the claim: the row is locked
    by this transaction, so the read is current. A row that is no longer ``pending``
    is an invariant violation and raises :class:`EvidenceClaimInconsistent`.
    """
    row = uow.connection.execute(
        select(evidence_unit.c.id, evidence_unit.c.extraction_attempts).where(
            evidence_unit.c.id == claimed.evidence_id,
            evidence_unit.c.state == STATE_PENDING,
        )
    ).one_or_none()
    if row is None:
        raise EvidenceClaimInconsistent("a claimed evidence row was no longer pending")
    _back_off(uow, [row], final_outcome=OUTCOME_ACCEPTANCE_FAILED, now=now)
