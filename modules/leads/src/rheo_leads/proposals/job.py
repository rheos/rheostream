"""The drafting sweep, ``leads.jobsearch_propose``: every queued proposal is drafted.

**What triggers a sweep.** A draft request (``leads.jobproposal.request``) queues one
due at once, a sweep that leaves work waiting queues its own follow-up for when that
work is due, and a schedule (every 30 minutes while job search is on, turned on by
setting the proposal profile) picks up anything a broken chain left behind. A sweep
with nothing to do reads one small table and ends.

**What it does.** One queued proposal at a time, oldest request first: claim it (count
the attempt, mark it ``drafting`` and hold it for the claim window) in a short unit
that commits at once; run the pipeline (:mod:`.pipeline`), each stage written as the
proposal's progress in its own short unit, which also renews the claim; record the
sections and findings in another short unit. No unit is open while a model runs, and
the cancellation token's heartbeat keeps the job's lease. A worker that dies mid-draft
leaves a claim that lapses, and the next sweep takes the proposal again, until its
attempts are spent.

**Failures are explicit.** A failure that is the account's or the deployment's (a usage
limit, a missing or refused login, no executable or adapter, a crashed run) gives the
attempt back and makes every queued proposal wait :data:`USAGE_DEFER`
(:data:`DEFERRING`). Every other failure counts, an unusable answer, a crashed or
truncated run and a run past its deadline included: the proposal waits the job
substrate's backoff, and the third attempt marks it ``failed`` with the reason, which
the owner sees and can request again. A proposal never simply disappears.

**A record only lands on the request it answers.** Deleting the draft, or a newer
request, while a sweep drafts makes the result stale, and it is dropped.

**Content-free.** Log lines carry counts and fixed failure words, and a database error
leaves as a fixed word. The profile and the posting reach the model's stdin; the draft
is stored as sections and findings that workspace members can read, and nowhere else.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_contracts import ContextPurpose, Role
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_scheduled_job
from rheo_core.log_config import content_free_extra
from rheo_core.refs import uuid7
from rheo_core.runtime.oneshot import (
    ADAPTER_UNAVAILABLE,
    TOKEN_REVOKE_FAILED,
    OneShotCredentialNotOwned,
    OneShotDeferred,
    OneShotFailed,
    OneShotRunner,
    oneshot_runner,
)
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import get_membership
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.backoff import backoff_for
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import StatementError

from rheo_leads import pipeline_common as h
from rheo_leads.proposals import prompts
from rheo_leads.proposals.pipeline import (
    AnswerUnusable,
    JobInput,
    Result,
    generate,
)
from rheo_leads.proposals.pipeline import (
    Role as AskRole,
)
from rheo_leads.proposals.profile import StoredProposalProfile, read_proposal_profile
from rheo_leads.scoring.tier1 import EXTENSION
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import proposals as s

_log = logging.getLogger(__name__)

PROPOSE_JOB_KIND: Final = "leads.jobsearch_propose"
PROPOSE_JOB_MAX_ATTEMPTS: Final = 8
# The model for the letter and its repair, and the one for the analysis and the
# accuracy extraction; empty leaves the CLI's default.
DRAFT_MODEL_KEY: Final = "leads.proposals.model_id"
ANALYSIS_MODEL_KEY: Final = "leads.proposals.analysis_model_id"
MODEL_KEYS: Final[Mapping[AskRole, str]] = {
    "analysis": ANALYSIS_MODEL_KEY,
    "gate": ANALYSIS_MODEL_KEY,
    "draft": DRAFT_MODEL_KEY,
    "repair": DRAFT_MODEL_KEY,
}
SCRATCH_SUBDIR: Final = "jobpropose"
DRAFT_ATTEMPTS: Final = 3
USAGE_DEFER: Final = timedelta(minutes=15)
CLAIM_MARGIN: Final = timedelta(minutes=5)
DEADLINE_KEY: Final = "runtime.max_deadline_seconds"
HARD_DEADLINE_SECONDS: Final = 3600
# A sweep starts no new draft after this long; a follow-up sweep takes over.
RUN_BUDGET: Final = timedelta(minutes=10)

READ_TARGETS: Final = (
    "message",
    EXTENSION + "platform",
    EXTENSION + "budget_min",
    EXTENSION + "budget_max",
)

DRAFTED_LOG: Final = "job_proposal_finished"
DEFERRED_LOG: Final = "job_proposal_deferred"
FAILED_LOG: Final = "job_proposal_failed"
STORAGE_FAILED: Final = "job_proposal_storage_failed"
PROFILE_INVALID_LOG: Final = "job_proposal_profile_invalid"
PROFILE_MISSING: Final = "profile_missing"
ACCOUNT_UNAVAILABLE: Final = "account_unavailable"
NO_DESCRIPTION: Final = "description_missing"
CLAIM_LAPSED: Final = "claim_lapsed"
SCHEDULE_NAME: Final = "jobsearch_propose"
PROPOSE_INTERVAL_SECONDS: Final = 1800
# Sweeps due within the same ten seconds share one job.
TRIGGER_GRANULARITY_SECONDS: Final = 10
# The failures that are the account's or the deployment's: nothing about the draft
# caused them, so the draft waits without counting an attempt. Every other failure,
# a crashed or truncated run and a run past its deadline included, counts: a cause
# the input can trigger would otherwise retry, and spend the shared limit, forever.
DEFERRING: Final = frozenset(
    {
        "usage_limited",
        "credential_invalid",
        "credential_not_owned",
        "executable_unavailable",
        ADAPTER_UNAVAILABLE,
        TOKEN_REVOKE_FAILED,
    }
)


class ProposePayload(BaseModel):
    """What a request and a follow-up enqueue: the workspace, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


@dataclass(frozen=True)
class Claimed:
    opportunity_id: UUID
    attempts: int
    title: str
    fields: Mapping[str, str]
    questions: str | None
    # Which request this claim answers: a delete and a new request reset the
    # attempt count, so the count alone cannot tell two requests apart.
    requested_at: datetime


class _Deferred(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _now() -> datetime:
    return datetime.now(UTC)


def _runner() -> OneShotRunner | None:
    return oneshot_runner()


@dataclass
class Hooks:
    """Injected by tests; production reads the clock and the process runner."""

    clock: Callable[[], datetime] = _now
    runner: Callable[[], OneShotRunner | None] = _runner


HOOKS = Hooks()


def queue_sweep(
    uow: UnitOfWork, *, workspace_id: UUID, now: datetime, at: datetime | None = None
) -> None:
    """Enqueue one sweep due at ``at`` (default now), rounded up to the next ten
    seconds so a burst of requests shares one job, on the caller's transaction."""
    due = at or now
    step = TRIGGER_GRANULARITY_SECONDS
    seconds = due.second + due.microsecond / 1e6
    due = due.replace(second=0, microsecond=0) + timedelta(
        seconds=-(-seconds // step) * step
    )
    enqueue_job(
        uow.connection,
        kind=PROPOSE_JOB_KIND,
        payload={"workspace_id": str(workspace_id)},
        now=now,
        max_attempts=PROPOSE_JOB_MAX_ATTEMPTS,
        next_run_at=due,
        dedupe=True,
    )
    if isinstance(uow, HandlerUnitOfWork):
        uow.request_due_mark()


def _claimable(now: datetime) -> Any:
    state = s.job_proposal
    due = or_(state.c.retry_after.is_(None), state.c.retry_after <= now)
    return or_(
        and_(state.c.state == "queued", due),
        # A claim whose holder died: its window has lapsed.
        and_(state.c.state == "drafting", state.c.retry_after <= now),
    )


def _fields(uow: UnitOfWork, identifier: UUID) -> dict[str, str]:
    state = p.opportunity_field_state
    return {
        r.target: r.value_text
        for r in uow.connection.execute(
            select(state.c.target, state.c.value_text).where(
                state.c.opportunity_id == identifier,
                state.c.target.in_(READ_TARGETS),
                state.c.value_kind == "value",
                state.c.orphaned.is_(False),
            )
        )
        if r.value_text is not None
    }


def claim(uow: UnitOfWork, *, now: datetime, window: timedelta) -> Claimed | None:
    """Count an attempt on the oldest claimable proposal and hold it for ``window``.

    A lapsed claim on a proposal that has used its attempts is marked failed instead
    of taken again: a worker that dies mid-draft every time would otherwise re-spend
    the model calls forever."""
    state, o = s.job_proposal, p.opportunity
    uow.connection.execute(
        update(state)
        .where(
            state.c.state == "drafting",
            state.c.retry_after <= now,
            state.c.attempts >= DRAFT_ATTEMPTS,
        )
        .values(
            state="failed",
            stage=None,
            step=None,
            total=None,
            retry_after=None,
            failure_kind=CLAIM_LAPSED,
            updated_at=now,
        )
    )
    row = (
        uow.connection.execute(
            select(
                state.c.opportunity_id,
                state.c.attempts,
                state.c.questions,
                state.c.requested_at,
                o.c.title,
            )
            .join(o, o.c.id == state.c.opportunity_id)
            .where(_claimable(now))
            .order_by(state.c.requested_at, state.c.opportunity_id)
            .limit(1)
            .with_for_update(of=state, skip_locked=True)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    attempts = row["attempts"] + 1
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == row["opportunity_id"])
        .values(
            state="drafting",
            attempts=attempts,
            stage=None,
            step=None,
            total=None,
            failure_kind=None,
            retry_after=now + window,
            updated_at=now,
        )
    )
    return Claimed(
        opportunity_id=row["opportunity_id"],
        attempts=attempts,
        title=row["title"],
        fields=_fields(uow, row["opportunity_id"]),
        questions=row["questions"],
        requested_at=row["requested_at"],
    )


def _mine(claimed: Claimed) -> Any:
    """Still this sweep's claim: drafting, for the same request, the same attempt."""
    state = s.job_proposal
    return and_(
        state.c.opportunity_id == claimed.opportunity_id,
        state.c.state == "drafting",
        state.c.requested_at == claimed.requested_at,
        state.c.attempts == claimed.attempts,
    )


def _current(uow: UnitOfWork, claimed: Claimed) -> Any:
    """The claimed row, locked, when it is still this sweep's claim."""
    state = s.job_proposal
    return uow.connection.execute(
        select(state.c.opportunity_id)
        .where(
            _mine(claimed),
        )
        .with_for_update()
    ).one_or_none()


def progress(
    uow: UnitOfWork,
    claimed: Claimed,
    stage: str,
    step: int,
    total: int,
    *,
    now: datetime,
    window: timedelta,
) -> None:
    state = s.job_proposal
    uow.connection.execute(
        update(state)
        .where(
            _mine(claimed),
        )
        .values(
            stage=stage,
            step=step,
            total=total,
            retry_after=now + window,
            updated_at=now,
        )
    )


def renew(
    uow: UnitOfWork, claimed: Claimed, *, now: datetime, window: timedelta
) -> None:
    """Hold the claim for another window, before each model call."""
    state = s.job_proposal
    uow.connection.execute(
        update(state).where(_mine(claimed)).values(retry_after=now + window)
    )


def _model_id(settings: Any) -> str:
    draft = settings.get_str(DRAFT_MODEL_KEY).strip() or "default"
    analysis = settings.get_str(ANALYSIS_MODEL_KEY).strip() or "default"
    return f"claude_cli:{draft}" + ("" if analysis == draft else f"+{analysis}")


def record_draft(
    uow: UnitOfWork,
    claimed: Claimed,
    result: Result,
    *,
    profile_version: int,
    model_id: str,
    now: datetime,
) -> bool:
    """Write the sections and findings; ``False`` when the claim went stale."""
    if _current(uow, claimed) is None:
        return False
    state, section = s.job_proposal, s.proposal_section
    uow.connection.execute(
        delete(section).where(section.c.opportunity_id == claimed.opportunity_id)
    )
    rows = [
        {
            "id": uuid7(),
            "opportunity_id": claimed.opportunity_id,
            "position": 0,
            "kind": "cover_letter",
            "question": None,
            "body": result.draft.letter,
            "library_answer_id": None,
            "updated_at": now,
        }
    ] + [
        {
            "id": uuid7(),
            "opportunity_id": claimed.opportunity_id,
            "position": i,
            "kind": "answer",
            "question": answer.question,
            "body": answer.answer,
            "library_answer_id": None,
            "updated_at": now,
        }
        for i, answer in enumerate(result.draft.answers, start=1)
    ]
    uow.connection.execute(insert(section), rows)
    values: dict[str, Any] = {
        "state": "ready",
        "stage": None,
        "step": None,
        "total": None,
        "findings": result.findings,
        "generated_at": now,
        "retry_after": None,
        "failure_kind": None,
        "profile_version": profile_version,
        "model_id": model_id,
        "prompt_version": f"{prompts.PROMPT_VERSION}/profile.{profile_version}",
        "updated_at": now,
    }
    if result.seeded_questions and not (claimed.questions or "").strip():
        values["questions"] = result.seeded_questions
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == claimed.opportunity_id)
        .values(**values)
    )
    return True


def record_wait(uow: UnitOfWork, claimed: Claimed, *, kind: str, now: datetime) -> bool:
    """The account cannot draft now: give the attempt back, and every queued proposal
    waits with this one."""
    if _current(uow, claimed) is None:
        return False
    state = s.job_proposal
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == claimed.opportunity_id)
        .values(
            state="queued",
            attempts=max(claimed.attempts - 1, 0),
            stage=None,
            step=None,
            total=None,
        )
    )
    uow.connection.execute(
        update(state)
        .where(
            state.c.state == "queued",
            or_(
                state.c.opportunity_id == claimed.opportunity_id,
                state.c.retry_after.is_(None),
                state.c.retry_after <= now + USAGE_DEFER,
            ),
        )
        .values(retry_after=now + USAGE_DEFER, failure_kind=kind, updated_at=now)
    )
    return True


def record_failure(
    uow: UnitOfWork, claimed: Claimed, *, kind: str, now: datetime
) -> bool:
    if _current(uow, claimed) is None:
        return False
    state = s.job_proposal
    final = claimed.attempts >= DRAFT_ATTEMPTS
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == claimed.opportunity_id)
        .values(
            state="failed" if final else "queued",
            stage=None,
            step=None,
            total=None,
            failure_kind=kind,
            retry_after=None
            if final
            else now + backoff_for(claimed.attempts, jitter=None),
            updated_at=now,
        )
    )
    return True


def follow_up_at(uow: UnitOfWork, *, now: datetime) -> datetime | None:
    """When the next sweep is needed: now while claimable work is left, else the
    earliest wait or claim expiry; ``None`` when nothing is queued or drafting."""
    state = s.job_proposal
    if uow.connection.execute(
        select(func.count()).select_from(state).where(_claimable(now))
    ).scalar_one():
        return now
    earliest: datetime | None = uow.connection.execute(
        select(func.min(state.c.retry_after)).where(
            state.c.state.in_(("queued", "drafting"))
        )
    ).scalar_one()
    return earliest


def _account_ready(workspace_id: UUID, account_id: UUID) -> bool:
    with get_backend().control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=account_id, workspace_id=workspace_id
        )
    return membership is not None and membership.role is Role.OWNER


def _job_input(claimed: Claimed) -> JobInput:
    fields = claimed.fields
    return JobInput(
        title=claimed.title,
        description=fields.get("message", ""),
        platform=fields.get(EXTENSION + "platform") or "upwork",
        budget_min=fields.get(EXTENSION + "budget_min"),
        budget_max=fields.get(EXTENSION + "budget_max"),
        questions=claimed.questions,
    )


def _asker(
    runner: OneShotRunner,
    *,
    workspace_id: UUID,
    account_id: UUID,
    heartbeat: Callable[[], None],
    before: Callable[[], None],
) -> Any:
    def ask(
        role: AskRole, task: str, schema: Mapping[str, Any]
    ) -> Mapping[str, object]:
        # One stage can make three calls; each renews the claim first.
        before()
        try:
            return runner.run(
                workspace_id=workspace_id,
                account_id=account_id,
                purpose=ContextPurpose.INTERNAL_ANALYSIS,
                task=task,
                json_schema=dict(schema),
                model_id_key=MODEL_KEYS[role],
                scratch_subdir=SCRATCH_SUBDIR,
                heartbeat=heartbeat,
            )
        except OneShotDeferred as exc:
            raise _Deferred(exc.kind) from None
        except OneShotCredentialNotOwned:
            raise _Deferred("credential_not_owned") from None
        except OneShotFailed as exc:
            if exc.kind in DEFERRING:
                raise _Deferred(exc.kind) from None
            raise AnswerUnusable(exc.kind) from None

    return ask


def propose_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """One sweep (module docstring). Expected failures never raise.

    A database error is re-raised as a fixed word with nothing chained: the driver's
    message can quote bound parameters, and a draft is one."""
    if not isinstance(payload, ProposePayload):
        raise TypeError(f"expected a ProposePayload, got {type(payload).__name__}")
    escaped = False
    try:
        _sweep(uow, payload, token)
    except StatementError:
        escaped = True
    if escaped:
        raise RuntimeError(STORAGE_FAILED)


def _sweep(
    uow: HandlerUnitOfWork, payload: ProposePayload, token: CancellationToken
) -> None:
    token.checkpoint()
    ctx = context_for_scheduled_job(payload.workspace_id)
    if isinstance(ctx, Refusal):
        return
    hooks = HOOKS
    started = hooks.clock()
    with open_unit_of_work(ctx) as short:
        enabled = h.jobsearch_enabled(ctx, short)
        profile: StoredProposalProfile | None = None
        try:
            profile = read_proposal_profile(short) if enabled else None
        except ValidationError:
            _log.warning(PROFILE_INVALID_LOG)
        settings = resolve(
            workspace_id=ctx.workspace_id,
            source=TransactionBoundOverrideSource(short, workspace_id=ctx.workspace_id),
        )
        window = (
            timedelta(
                seconds=min(settings.get_int(DEADLINE_KEY), HARD_DEADLINE_SECONDS)
            )
            + CLAIM_MARGIN
        )
        model_id = _model_id(settings)
        short.commit()
    if not enabled:
        return
    runner = hooks.runner()
    counts = {"drafted": 0, "failed": 0, "deferred": 0}
    while True:
        token.checkpoint()
        now = hooks.clock()
        if now - started >= RUN_BUDGET:
            break
        with open_unit_of_work(ctx) as short:
            claimed = claim(short, now=now, window=window)
            short.commit()
        if claimed is None:
            break
        outcome = _draft_one(
            ctx.workspace_id,
            claimed,
            profile,
            runner=runner,
            token=token,
            window=window,
            ctx=ctx,
        )
        now = hooks.clock()
        with open_unit_of_work(ctx) as short:
            if isinstance(outcome, Result):
                assert profile is not None
                record_draft(
                    short,
                    claimed,
                    outcome,
                    profile_version=profile.version,
                    model_id=model_id,
                    now=now,
                )
                counts["drafted"] += 1
            elif isinstance(outcome, _Deferred):
                record_wait(short, claimed, kind=outcome.kind, now=now)
                counts["deferred"] += 1
            else:
                record_failure(short, claimed, kind=outcome, now=now)
                counts["failed"] += 1
            short.commit()
        if isinstance(outcome, _Deferred):
            _log.warning(
                DEFERRED_LOG, extra=content_free_extra({"failure_kind": outcome.kind})
            )
            break
        if isinstance(outcome, str):
            _log.warning(
                FAILED_LOG,
                extra=content_free_extra(
                    {"failure_kind": outcome, "attempts": claimed.attempts}
                ),
            )
    _log.info(
        DRAFTED_LOG, extra=content_free_extra({"unit_count": sum(counts.values())})
    )
    now = hooks.clock()
    due = follow_up_at(uow, now=now)
    if due is not None:
        queue_sweep(uow, workspace_id=ctx.workspace_id, now=now, at=max(due, now))


def _draft_one(
    workspace_id: UUID,
    claimed: Claimed,
    profile: StoredProposalProfile | None,
    *,
    runner: OneShotRunner | None,
    token: CancellationToken,
    window: timedelta,
    ctx: Any,
) -> Result | _Deferred | str:
    """The draft, a wait for the whole account, or a counted failure's word."""
    if profile is None:
        return PROFILE_MISSING
    job = _job_input(claimed)
    if not job.description.strip():
        return NO_DESCRIPTION
    if runner is None:
        return _Deferred(ADAPTER_UNAVAILABLE)
    if not _account_ready(workspace_id, profile.drafting_account_id):
        return ACCOUNT_UNAVAILABLE

    def report(stage: str, step: int, total: int) -> None:
        token.checkpoint(force=True)
        now = HOOKS.clock()
        with open_unit_of_work(ctx) as short:
            progress(short, claimed, stage, step, total, now=now, window=window)
            short.commit()

    def hold() -> None:
        token.checkpoint(force=True)
        with open_unit_of_work(ctx) as short:
            renew(short, claimed, now=HOOKS.clock(), window=window)
            short.commit()

    ask = _asker(
        runner,
        workspace_id=workspace_id,
        account_id=profile.drafting_account_id,
        heartbeat=token.checkpoint,
        before=hold,
    )
    try:
        return generate(job, profile, ask, report)
    except _Deferred as exc:
        return exc
    except AnswerUnusable as exc:
        return exc.kind
