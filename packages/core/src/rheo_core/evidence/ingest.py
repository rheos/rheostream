"""``core.evidence.ingest``: the one operation an enrolled bridge token can call (spec
§ Architecture → System Components, item 4; FR 6, FR 7, FR 8).

The bridge sends local Claude Code turns and the gaps it knows it cannot deliver. The
handler checks the token against its enrollment, asks the unchanged recording gate,
clamps each clock to the server's, and hands everything to :func:`record_gaps` and
:func:`record_evidence`, the one writer of evidence rows. The answer is exactly those
functions' four tuples of native keys: no text, no length, no count.

**Identity comes from the enrollment row, never from the context.** The context's
actor is the bridge token; every row's ``authority_id`` is the enrollment's id and its
speaker and audience are the enrollment's account.

**Content-free.** Nothing here logs the payload. ``record_evidence`` re-sanitizes every
record with the server's own ``sanitize()``, whatever sanitizer the bridge ran, which
closes the stale-bridge-sanitizer question (Edge Case 9) without a version check.
"""

from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from rheo_contracts import ActorKind, ContextPurpose, WorkspaceContext
from sqlalchemy import select
from sqlalchemy.engine import Row

from rheo_core.evidence.local_authority import (
    ENROLLMENT_INACTIVE,
    ENROLLMENT_MISMATCH,
    LOCAL_PRODUCER_KIND,
)
from rheo_core.evidence.record import (
    NewEvidence,
    NewGap,
    record_evidence,
    record_gaps,
    recording_allowed,
)
from rheo_core.evidence.sanitize import MAX_INPUT_CHARS, strip_unstorable
from rheo_core.operations.refusals import OUTPUT_INVALID, OperationRefused
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import get_access_token
from rheo_core.storage.evidence_enrollment_tables import (
    FINGERPRINT_PATTERN,
    STATE_ACTIVE,
    evidence_enrollment,
)
from rheo_core.storage.postgres import get_backend

EVIDENCE_INGEST: Final = "core.evidence.ingest"
"""``tokens/issue.py`` spells it again as ``BRIDGE_TOKEN_OPERATIONS``."""

INGEST_MAX_RECORDS: Final = 1000
INGEST_MAX_GAPS: Final = 1000
"""Request-size guards, not tunable limits: the ``automatic_memory.*`` budgets inside
``record_evidence`` govern what is admitted. Over either answers ``input_invalid``."""

RECORD_KEY_PATTERN: Final = "^cc1:[0-9a-f]{64}$"
GAP_KEY_PATTERN: Final = "^cc1g:[0-9a-f]{64}$"

_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")
_FINGERPRINT: Final = Field(pattern=FINGERPRINT_PATTERN)


class IngestRecord(BaseModel):
    model_config = _IGNORE_EXTRA

    native_key: str = Field(pattern=RECORD_KEY_PATTERN)
    recorded_at: AwareDatetime
    text: str = Field(max_length=MAX_INPUT_CHARS)

    @field_validator("text", mode="before")
    @classmethod
    def _scrub_unstorable(cls, value: object) -> object:
        """Scrub NUL and lone surrogates before pydantic-core's ``str`` check, which
        refuses a lone surrogate and so the whole request, and before ``max_length``,
        which then bounds the scrubbed text. Anything but a ``str`` passes through for
        pydantic to refuse normally."""
        return strip_unstorable(value) if isinstance(value, str) else value


class IngestGap(BaseModel):
    model_config = _IGNORE_EXTRA

    native_key: str = Field(pattern=GAP_KEY_PATTERN)
    reason: Literal["source_truncated", "expired_pending"]
    recorded_at: AwareDatetime


class IngestInput(BaseModel):
    model_config = _IGNORE_EXTRA

    machine_fingerprint: str = _FINGERPRINT
    project_fingerprint: str = _FINGERPRINT
    records: list[IngestRecord] = Field(max_length=INGEST_MAX_RECORDS)
    gaps: list[IngestGap] = Field(max_length=INGEST_MAX_GAPS)


class IngestResult(BaseModel):
    """``record_evidence``'s four tuples, with the gap keys appended to ``gapped``."""

    model_config = ConfigDict(frozen=True)

    accepted: tuple[str, ...]
    deferred: tuple[str, ...]
    gapped: tuple[str, ...]
    dropped: tuple[str, ...]


def _active_enrollment(uow: UnitOfWork, token_id: UUID) -> Row[Any] | None:
    """The active enrollment holding ``token_id``, read ``FOR SHARE``, or ``None``."""
    return uow.connection.execute(
        select(evidence_enrollment)
        .where(
            evidence_enrollment.c.token_id == token_id,
            evidence_enrollment.c.state == STATE_ACTIVE,
        )
        .with_for_update(read=True)
    ).one_or_none()


def _token_kind(token_id: UUID) -> str | None:
    with get_backend().control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    return None if row is None else row.kind


def ingest_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: IngestInput
) -> IngestResult:
    """Check, gate, clamp, record; each step a plain early exit."""
    # Wiring, not a recording condition: ``dispatch()`` always hands a handler unit of
    # work, so anything else is a miswired caller. Refused loudly rather than folded
    # into the all-deferred answer, which would make it look like an opted-out
    # deployment (``core.runtime.run`` makes the same check).
    if not isinstance(uow, HandlerUnitOfWork):
        raise OperationRefused(
            OUTPUT_INVALID, "core.evidence.ingest needs a handler unit of work"
        )
    # 1. A token actor whose token an active enrollment holds.
    if ctx.actor.kind is not ActorKind.TOKEN or ctx.actor.id is None:
        raise OperationRefused(ENROLLMENT_INACTIVE, "no active enrollment")
    enrollment = _active_enrollment(uow, ctx.actor.id)
    if enrollment is None:
        raise OperationRefused(ENROLLMENT_INACTIVE, "no active enrollment")

    # 2. The presented machine and directory, the account and the purpose are the
    #    enrollment's. The purpose check can only fail for a hand-made or malformed
    #    token: the enroll path always mints the token under the enrollment's purpose.
    bound = ctx.principal.bound_purpose
    if (
        model_input.machine_fingerprint != enrollment.machine_fingerprint
        or model_input.project_fingerprint != enrollment.project_fingerprint
        or ctx.principal.account_id != enrollment.account_id
        or bound is None
        or bound.value != enrollment.purpose
    ):
        raise OperationRefused(ENROLLMENT_MISMATCH, "the enrollment does not match")

    # 3. The unchanged recording gate. Off: every key deferred and nothing else, the
    #    same four-tuple shape whichever condition failed (AC 15, no discriminator).
    record_keys = tuple(r.native_key for r in model_input.records)
    gap_keys = tuple(g.native_key for g in model_input.gaps)
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    )
    if not recording_allowed(
        ctx, uow, token_kind=_token_kind(ctx.actor.id), settings=settings
    ):
        return IngestResult(
            accepted=(), deferred=record_keys + gap_keys, gapped=(), dropped=()
        )

    # 4. Clamp every clock to the server's, before anything is recorded. A laptop
    #    clock ahead of the server would otherwise store ``source_recorded_at > now``,
    #    which Recallatron's ``_window_state`` settles ``denied`` for good. Clamping,
    #    not refusing: refusing would lose a turn to a few seconds of skew, and the
    #    native key, not the clock, is the row's identity, so a replay carrying the
    #    same (even still-skewed) key lands on the already-held row regardless.
    now = datetime.now(UTC)
    account_id: UUID = enrollment.account_id
    purpose = ContextPurpose(enrollment.purpose)

    # 5. Gaps, then records, through the one writer. Identity from the enrollment.
    held_gaps = record_gaps(
        ctx,
        uow,
        [
            NewGap(
                producer_kind=LOCAL_PRODUCER_KIND,
                authority_id=enrollment.id,
                native_key=g.native_key,
                speaker_account_id=account_id,
                audience_kind="member",
                audience_id=account_id,
                purpose=purpose,
                recorded_at=min(g.recorded_at, now),
                reason=g.reason,
            )
            for g in model_input.gaps
        ],
        now=now,
    )
    attempt = record_evidence(
        ctx,
        uow,
        [
            NewEvidence(
                producer_kind=LOCAL_PRODUCER_KIND,
                authority_id=enrollment.id,
                native_key=r.native_key,
                speaker_account_id=account_id,
                audience_kind="member",
                audience_id=account_id,
                purpose=purpose,
                recorded_at=min(r.recorded_at, now),
                raw_text=r.text,
            )
            for r in model_input.records
        ],
        now=now,
    )

    # 6. Exactly the attempt's four tuples, the gap keys appended to ``gapped``.
    return IngestResult(
        accepted=attempt.accepted,
        deferred=attempt.deferred,
        gapped=attempt.gapped + held_gaps,
        dropped=attempt.dropped,
    )
