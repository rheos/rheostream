"""The scoring sweep, ``leads.jobsearch_score``: every open Job search opportunity gets
a tier-1 disposition and, when it passes, a tier-2 score.

**What triggers a sweep.** The schedule (every 30 minutes while job search is on,
turned on by setting the profile), the profile operation itself, and every processed
delivery into a Job search pipeline (:func:`queue_sweep`, deduplicated to one job per
minute). A sweep with nothing to do reads two small tables and ends.

**What it does.** First a refresh: each open Job search opportunity's scoring inputs
are digested (:func:`input_digest`), and an opportunity seen for the first time, or
whose description, competition band or client metrics moved, becomes ``pending``
with a fresh attempt count. Then up to ``leads.scoring.max_per_run`` pending jobs,
newest first, one at a time: claim it (count the attempt, hold it for the claim
window) in a short unit that commits at once; decide tier 1; ask the model when tier
1 lets it through and the description is there; record the result as a
qualification in another short unit. No unit is open while the model runs, and the
cancellation token's heartbeat keeps the lease.

**Failures are explicit.** A failure that is the account's or the deployment's (a
usage limit, a missing or refused login, no executable or adapter, a crashed or
timed-out run) gives the attempt back and makes every waiting job wait
:data:`USAGE_DEFER`; the sweep stops there. Only a failure that says something about
the job or the answer (:data:`JOB_FAILURES`) counts: it waits the job substrate's
backoff, and the fifth attempt records a ``needs_information`` assessment that says
scoring failed. Setting the profile again puts failed and unscored jobs back in line
(:func:`repend_unscored`). A job never simply disappears from the queue.

**Content-free.** Log lines carry counts and fixed failure words, and a database
error leaves as a fixed word. The profile text reaches the model's stdin, and a
score's reasons quote what they cite from it (project names and recorded experience
boundaries) on the qualification record, which workspace members can read. No log
line, exception or audit row carries it.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_scheduled_job
from rheo_core.log_config import content_free_extra
from rheo_core.refs import uuid7
from rheo_core.runtime.oneshot import (
    ADAPTER_UNAVAILABLE,
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
from sqlalchemy import and_, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import StatementError

from rheo_leads import pipeline_common as h
from rheo_leads.references import ref
from rheo_leads.scoring import tier2
from rheo_leads.scoring.profile import StoredProfile, read_profile
from rheo_leads.scoring.tier1 import EXTENSION, JobFacts, Tier1Result, run_tier1
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import scoring as s

_log = logging.getLogger(__name__)

SCORE_JOB_KIND: Final = "leads.jobsearch_score"
SCORE_JOB_MAX_ATTEMPTS: Final = 8
"""The job's own retry budget, for a sweep that raised (a database fault)."""
SCORE_INTERVAL_SECONDS: Final = 1800
MODEL_ID_KEY: Final = "leads.scoring.model_id"
MAX_PER_RUN_KEY: Final = "leads.scoring.max_per_run"
SCRATCH_SUBDIR: Final = "jobscore"
SCORE_ATTEMPTS: Final = 5
"""The attempt that records a failed assessment instead of backing off again."""
USAGE_DEFER: Final = timedelta(minutes=15)
CLAIM_MARGIN: Final = timedelta(minutes=5)
"""A claimed job is held for the run's deadline plus this; a sweep that died
mid-run frees it after that, and no live run can outlast its claim."""
DEADLINE_KEY: Final = "runtime.max_deadline_seconds"
HARD_DEADLINE_SECONDS: Final = 3600
RUN_BUDGET: Final = timedelta(minutes=5)
"""No new claim starts once a sweep has run this long; a follow-up sweep goes on."""
TRIGGER_GRANULARITY: Final = timedelta(minutes=1)

RUBRIC_OBJECTIVE: Final = "Job search score"
ASSESSMENT_CREATOR: Final = "system"

SCORED_LOG: Final = "job_scoring_finished"
DEFERRED_LOG: Final = "job_scoring_deferred"
FAILED_LOG: Final = "job_scoring_failed"
ACCOUNT_LOG: Final = "job_scoring_account_unavailable"

CREDENTIAL_NOT_OWNED: Final = "credential_not_owned"
ASSESSMENT_INVALID: Final = "assessment_invalid"
USAGE_LIMITED: Final = "usage_limited"
# The failures that say something about the job or the answer, and so count an
# attempt. Every other failure (a usage limit, a missing or refused login, an
# executable or adapter that is not there, a crashed or timed-out run) is the
# account's or the deployment's, and pauses scoring without counting anything.
JOB_FAILURES: Final = frozenset(
    {
        "output_invalid",
        "no_structured_result",
        "iteration_limit",
        "approval_required",
        "tool_denied",
        ASSESSMENT_INVALID,
    }
)
STORAGE_FAILED: Final = "job_scoring_storage_failed"
PROFILE_INVALID_LOG: Final = "job_scoring_profile_invalid"

# The inputs a score depends on, by target. Competition and client metrics are
# bucketed (:func:`_bucket`), so a re-sighting that moves them a little does not
# spend a model call; the description and the price are compared whole.
DIGEST_TARGETS: Final = (
    "message",
    EXTENSION + "platform",
    EXTENSION + "job_type",
    EXTENSION + "budget_min",
    EXTENSION + "budget_max",
    EXTENSION + "client_payment_verified",
)
READ_TARGETS: Final = (
    "message",
    *(
        EXTENSION + name
        for name in (
            "platform",
            "job_type",
            "budget_min",
            "budget_max",
            "proposal_count",
            "proposal_count_exact",
            "client_payment_verified",
            "client_spend",
            "client_rating",
            "client_reviews",
            "client_hire_rate",
        )
    ),
)


class ScorePayload(BaseModel):
    """What the ticker and the triggers enqueue: the workspace, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


@dataclass(frozen=True)
class Claimed:
    opportunity_id: UUID
    digest: str
    attempts: int
    title: str
    fields: Mapping[str, str]
    revision: int
    preset_version: int
    rubric_slug: str
    rubric_version: int
    evidence_digest: str
    observation_refs: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    """What one claimed job ends as: an assessment, a wait, or a counted failure."""

    fit: str | None = None
    score: int | None = None
    lines: tuple[str, ...] = ()
    uncertainty: str = "high"
    completeness: str = "needs_information"
    model: bool = False
    deferred: str | None = None
    """Why scoring paused for every job: no attempt is counted."""
    failure: str | None = None


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
    uow: UnitOfWork, workspace_id: UUID, *, now: datetime, at: datetime | None = None
) -> None:
    """Enqueue one sweep due at the minute boundary after ``at`` (default ``now``),
    at most one per minute.

    On the caller's transaction, so a sweep is queued only with what triggered it.
    """
    due = ((at or now) + TRIGGER_GRANULARITY).replace(second=0, microsecond=0)
    enqueue_job(
        uow.connection,
        kind=SCORE_JOB_KIND,
        payload={"workspace_id": str(workspace_id)},
        now=now,
        max_attempts=SCORE_JOB_MAX_ATTEMPTS,
        next_run_at=due,
        dedupe=True,
    )
    if isinstance(uow, HandlerUnitOfWork):
        uow.request_due_mark()


def profile_set(uow: UnitOfWork) -> bool:
    return (
        uow.connection.execute(
            select(func.count()).select_from(s.jobsearch_profile)
        ).scalar_one()
        > 0
    )


def _bucket(name: str, value: str | None, cap: int) -> str | None:
    """A coarse value for the digest: what would change the filters or the score."""
    if value is None:
        return None
    try:
        number = Decimal(value)
    except ArithmeticError:
        return value
    if not number.is_finite():
        return None
    if name == "proposal_count":
        if number > cap:
            return "over"
        for top in (4, 10, 15, 20, 50):
            if number <= top:
                return f"<={top}"
        return f"<={cap}"
    if name == "client_spend":
        for top in (0, 499, 999, 9999, 99999):
            if number <= top:
                return f"<={top}"
        return "more"
    if name == "client_hire_rate":
        return str(int(number * 10))
    if name == "client_rating":
        return str(round(number, 1))
    if name == "client_reviews":
        return "0" if number == 0 else "few" if number < 10 else "many"
    return value


def input_digest(title: str, fields: Mapping[str, str], cap: int) -> str:
    """A fingerprint of what a score judged: the title, the description and price
    whole, the competition and client metrics by bucket."""
    facts: dict[str, Any] = {"title": title}
    facts.update({target: fields.get(target) for target in DIGEST_TARGETS})
    for name in (
        "proposal_count",
        "client_spend",
        "client_rating",
        "client_reviews",
        "client_hire_rate",
    ):
        facts[name] = _bucket(name, fields.get(EXTENSION + name), cap)
    canonical = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _open_jobs() -> Any:
    o, stage = p.opportunity, p.preset_stage
    return (
        select(o.c.id, o.c.title)
        .join(
            stage,
            and_(
                stage.c.preset_id == o.c.preset_id,
                stage.c.version == o.c.preset_version,
                stage.c.stage_id == o.c.stage_id,
            ),
        )
        .where(o.c.preset_id == h.JOBSEARCH_PRESET_ID, stage.c.kind == "open")
    )


def _fields(uow: UnitOfWork, ids: list[UUID]) -> dict[UUID, dict[str, str]]:
    state = p.opportunity_field_state
    found: dict[UUID, dict[str, str]] = {i: {} for i in ids}
    if not ids:
        return found
    for r in uow.connection.execute(
        select(state.c.opportunity_id, state.c.target, state.c.value_text).where(
            state.c.opportunity_id.in_(ids),
            state.c.target.in_(READ_TARGETS),
            state.c.value_kind == "value",
            state.c.orphaned.is_(False),
        )
    ):
        if r.value_text is not None:
            found[r.opportunity_id][r.target] = r.value_text
    return found


def refresh(uow: UnitOfWork, *, cap: int, now: datetime) -> int:
    """Make every open job whose inputs are new or moved ``pending``; the count."""
    jobs = {r.id: r.title for r in uow.connection.execute(_open_jobs())}
    fields = _fields(uow, list(jobs))
    known = {
        r.opportunity_id: r.input_digest
        for r in uow.connection.execute(
            select(s.job_score_state.c.opportunity_id, s.job_score_state.c.input_digest)
        )
    }
    changed = 0
    for identifier, title in jobs.items():
        digest = input_digest(title, fields[identifier], cap)
        if known.get(identifier) == digest:
            continue
        values = {
            "input_digest": digest,
            "state": "pending",
            "attempts": 0,
            "retry_after": None,
            "failure_kind": None,
            "updated_at": now,
        }
        # An upsert, so a sweep running beside this one cannot collide on a new row.
        uow.connection.execute(
            pg_insert(s.job_score_state)
            .values(opportunity_id=identifier, **values)
            .on_conflict_do_update(
                index_elements=[s.job_score_state.c.opportunity_id], set_=values
            )
        )
        changed += 1
    return changed


def _claimable(now: datetime) -> Any:
    state = s.job_score_state
    return and_(
        state.c.state == "pending",
        (state.c.retry_after.is_(None)) | (state.c.retry_after <= now),
    )


def claim(uow: UnitOfWork, *, now: datetime, window: timedelta) -> Claimed | None:
    """Count an attempt on the newest claimable job and hold it for ``window``;
    ``None`` when nothing is claimable."""
    state, o = s.job_score_state, p.opportunity
    open_ids = _open_jobs().with_only_columns(o.c.id)
    row = (
        uow.connection.execute(
            select(state.c.opportunity_id, state.c.input_digest, state.c.attempts)
            .join(o, o.c.id == state.c.opportunity_id)
            .where(_claimable(now), state.c.opportunity_id.in_(open_ids))
            .order_by(o.c.created_at.desc(), o.c.id.desc())
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
        .values(attempts=attempts, retry_after=now + window, updated_at=now)
    )
    record = h.row(uow, o, row["opportunity_id"])
    rubric = (
        uow.connection.execute(
            select(p.preset_rubric).where(
                p.preset_rubric.c.preset_id == record["preset_id"],
                p.preset_rubric.c.version == record["preset_version"],
            )
        )
        .mappings()
        .one()
    )
    observations: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_observation.c.observation_id).where(
                p.opportunity_observation.c.opportunity_id == record["id"]
            )
        ).scalars()
    )
    return Claimed(
        opportunity_id=record["id"],
        digest=row["input_digest"],
        attempts=attempts,
        title=record["title"],
        fields=_fields(uow, [record["id"]])[record["id"]],
        revision=record["revision"],
        preset_version=record["preset_version"],
        rubric_slug=rubric["rubric_slug"],
        rubric_version=rubric["rubric_version"],
        evidence_digest=h.evidence_digest(uow, record),
        observation_refs=tuple(ref("observation", i) for i in observations),
    )


def _completeness(job: JobFacts) -> str:
    known = sum(
        value is not None
        for value in (
            job.description,
            job.budget_max or job.budget_min,
            job.proposal_count,
            job.payment_verified,
            job.client_spend,
            job.client_rating,
            job.client_hire_rate,
        )
    )
    return "high" if known >= 6 else "medium" if known >= 4 else "low"


def _held(job: JobFacts, decided: Tier1Result) -> Outcome:
    return Outcome(
        fit="low",
        lines=("Held back by the client filters before scoring.", *decided.failed),
        uncertainty="low",
        completeness=_completeness(job),
    )


def _undescribed(job: JobFacts) -> Outcome:
    return Outcome(
        fit="needs_information",
        lines=(
            "Not scored: the posting's full description is missing.",
            "Paste the full posting on this job to score it.",
        ),
        completeness=_completeness(job),
    )


def assess(
    job: JobFacts,
    profile: StoredProfile,
    *,
    runner: OneShotRunner | None,
    workspace_id: UUID,
    heartbeat: Callable[[], None],
) -> Outcome:
    """Tier 1, then the model for a job tier 1 lets through with a description."""
    decided = run_tier1(job, profile.policy.tier1)
    if not decided.passed:
        return _held(job, decided)
    if job.description is None:
        return _undescribed(job)
    if runner is None:
        return Outcome(deferred=ADAPTER_UNAVAILABLE)
    text = tier2.source_text(job)
    catalog = tier2.evidence_catalog(profile.profile, profile.policy)
    failure: str | None = None
    answer: Mapping[str, object] | None = None
    try:
        answer = runner.run(
            workspace_id=workspace_id,
            account_id=profile.scoring_account_id,
            purpose=ContextPurpose.INTERNAL_ANALYSIS,
            task=tier2.build_task(
                job,
                profile=profile.profile,
                policy=profile.policy,
                new_client=decided.new_client,
            ),
            json_schema=tier2.response_schema(catalog, tier2.job_excerpts(text)),
            model_id_key=MODEL_ID_KEY,
            scratch_subdir=SCRATCH_SUBDIR,
            heartbeat=heartbeat,
        )
    except OneShotDeferred:
        return Outcome(deferred=USAGE_LIMITED)
    except OneShotFailed as exc:
        failure = exc.kind
    except OneShotCredentialNotOwned:
        failure = CREDENTIAL_NOT_OWNED
    if failure is not None:
        if failure in JOB_FAILURES:
            return Outcome(failure=failure)
        return Outcome(deferred=failure)
    try:
        rendered = tier2.render_assessment(answer, text, catalog, job)
    except ValueError:
        return Outcome(failure=ASSESSMENT_INVALID)
    lines = (*rendered.lines, *decided.soft)
    uncertainty = (
        "high"
        if rendered.economics == "uncertain" and len(rendered.conditions) >= 2
        else "medium"
        if rendered.economics == "uncertain" or rendered.conditions or decided.soft
        else "low"
    )
    return Outcome(
        fit=tier2.fit_band(rendered.score),
        score=rendered.score,
        lines=lines,
        uncertainty=uncertainty,
        completeness=_completeness(job),
        model=True,
    )


def _model_id(workspace_id: UUID, uow: UnitOfWork) -> str:
    settings = resolve(
        workspace_id=workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=workspace_id),
    )
    return "claude_cli:" + (settings.get_str(MODEL_ID_KEY).strip() or "default")


def record(
    ctx: WorkspaceContext,
    uow: HandlerUnitOfWork,
    claimed: Claimed,
    outcome: Outcome,
    *,
    profile_version: int,
    now: datetime,
) -> bool:
    """Write the outcome; ``False`` when the job's inputs moved while it was scored
    (the refresh made it pending again, and its next claim scores the new inputs)."""
    state = s.job_score_state
    current = uow.connection.execute(
        select(state.c.input_digest, state.c.attempts)
        .where(state.c.opportunity_id == claimed.opportunity_id)
        .with_for_update()
    ).one_or_none()
    if current is None or current.input_digest != claimed.digest:
        return False
    if outcome.deferred:
        # The cause is the account's, so every claimable job waits with this one,
        # and only this one had an attempt counted to give back.
        uow.connection.execute(
            update(state)
            .where(state.c.opportunity_id == claimed.opportunity_id)
            .values(attempts=max(current.attempts - 1, 0))
        )
        uow.connection.execute(
            update(state)
            .where((state.c.opportunity_id == claimed.opportunity_id) | _claimable(now))
            .values(
                retry_after=now + USAGE_DEFER,
                failure_kind=outcome.deferred,
                updated_at=now,
            )
        )
        return True
    if outcome.failure is not None and claimed.attempts < SCORE_ATTEMPTS:
        uow.connection.execute(
            update(state)
            .where(state.c.opportunity_id == claimed.opportunity_id)
            .values(
                retry_after=now + backoff_for(claimed.attempts, jitter=None),
                failure_kind=outcome.failure,
                updated_at=now,
            )
        )
        return True
    if outcome.failure is not None:
        outcome = Outcome(
            fit="needs_information",
            lines=(
                f"Scoring failed after {claimed.attempts} attempts "
                f"({outcome.failure}).",
                "It is tried again when the posting changes.",
            ),
            failure=outcome.failure,
        )
    identifier = uuid7()
    uow.connection.execute(
        insert(p.qualification).values(
            id=identifier,
            opportunity_id=claimed.opportunity_id,
            preset_version=claimed.preset_version,
            rubric_slug=claimed.rubric_slug,
            rubric_version=claimed.rubric_version,
            objective=RUBRIC_OBJECTIVE,
            evidence_refs=list(claimed.observation_refs),
            input_revision=claimed.revision,
            evidence_digest=claimed.evidence_digest,
            fit=outcome.fit,
            intent="needs_information",
            urgency="needs_information",
            evidence_completeness=outcome.completeness,
            explanation="\n".join(outcome.lines),
            uncertainty=outcome.uncertainty,
            model_id=_model_id(ctx.workspace_id, uow) if outcome.model else None,
            prompt_version=(
                f"{tier2.PROMPT_VERSION}/profile.{profile_version}"
                if outcome.model
                else None
            ),
            score=outcome.score,
            created_by_kind=ASSESSMENT_CREATOR,
            created_by_id=None,
            created_at=now,
        )
    )
    uow.connection.execute(
        update(state)
        .where(state.c.opportunity_id == claimed.opportunity_id)
        .values(
            state="failed" if outcome.failure is not None else "scored",
            retry_after=None,
            failure_kind=outcome.failure,
            updated_at=now,
        )
    )
    h.event(
        ctx,
        uow,
        "opportunity.qualified",
        {"id": claimed.opportunity_id, "revision": claimed.revision},
        ref("qualification", identifier),
    )
    return True


def repend_unscored(uow: UnitOfWork, *, now: datetime) -> None:
    """After the profile changes: every job that failed, or that has no model score
    yet (held by the filters, or waiting for its description), goes back to
    pending, so the new thresholds and evidence apply. Scored jobs keep their score
    until their posting changes."""
    state, qual = s.job_score_state, p.qualification
    scored = exists().where(
        qual.c.opportunity_id == state.c.opportunity_id, qual.c.score.is_not(None)
    )
    uow.connection.execute(
        update(state)
        .where((state.c.state == "failed") | ~scored)
        .values(
            state="pending",
            attempts=0,
            retry_after=None,
            failure_kind=None,
            updated_at=now,
        )
    )


def follow_up_at(uow: UnitOfWork, *, now: datetime) -> datetime | None:
    """When the next sweep is needed: now while claimable work is left, else the
    earliest wait; ``None`` when nothing is pending."""
    state = s.job_score_state
    o = p.opportunity
    open_ids = _open_jobs().with_only_columns(o.c.id)
    pending = state.c.state == "pending"
    open_pending = and_(pending, state.c.opportunity_id.in_(open_ids))
    if uow.connection.execute(
        select(func.count()).select_from(state).where(open_pending, _claimable(now))
    ).scalar_one():
        return now
    earliest: datetime | None = uow.connection.execute(
        select(func.min(state.c.retry_after)).where(open_pending)
    ).scalar_one()
    return earliest


def _account_ready(workspace_id: UUID, account_id: UUID) -> bool:
    """The scoring account is still an owner of the workspace."""
    with get_backend().control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=account_id, workspace_id=workspace_id
        )
    return membership is not None and membership.role is Role.OWNER


def score_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """One sweep (module docstring). Expected failures never raise.

    A database error is re-raised as a fixed word with nothing chained: the
    driver's message can quote bound parameters, and an explanation is one."""
    if not isinstance(payload, ScorePayload):
        raise TypeError(f"expected a ScorePayload, got {type(payload).__name__}")
    escaped = False
    try:
        _sweep(uow, payload, token)
    except StatementError:
        escaped = True
    if escaped:
        raise RuntimeError(STORAGE_FAILED)


def _sweep(
    uow: HandlerUnitOfWork, payload: ScorePayload, token: CancellationToken
) -> None:
    token.checkpoint()
    ctx = context_for_scheduled_job(payload.workspace_id)
    if isinstance(ctx, Refusal):
        return
    hooks = HOOKS
    now = hooks.clock()
    with open_unit_of_work(ctx) as short:
        enabled = h.jobsearch_enabled(ctx, short)
        try:
            profile = read_profile(short) if enabled else None
        except ValidationError:
            # Its text would quote the policy; the owner sets the profile again.
            profile = None
            _log.warning(PROFILE_INVALID_LOG)
        if profile is not None:
            refresh(short, cap=profile.policy.tier1.max_proposals, now=now)
        settings = resolve(
            workspace_id=ctx.workspace_id,
            source=TransactionBoundOverrideSource(short, workspace_id=ctx.workspace_id),
        )
        budget = settings.get_int(MAX_PER_RUN_KEY)
        window = (
            timedelta(
                seconds=min(settings.get_int(DEADLINE_KEY), HARD_DEADLINE_SECONDS)
            )
            + CLAIM_MARGIN
        )
        short.commit()
    if profile is None:
        return
    if not _account_ready(ctx.workspace_id, profile.scoring_account_id):
        _log.warning(ACCOUNT_LOG)
        return
    runner = hooks.runner()
    counts = {"scored": 0, "held": 0, "failed": 0, "deferred": 0}
    started = now
    for _ in range(budget):
        token.checkpoint()
        now = hooks.clock()
        if now - started >= RUN_BUDGET:
            break
        with open_unit_of_work(ctx) as short:
            claimed = claim(short, now=now, window=window)
            short.commit()
        if claimed is None:
            break
        job = JobFacts.from_fields(claimed.title, claimed.fields)
        token.checkpoint(force=True)
        outcome = assess(
            job,
            profile,
            runner=runner,
            workspace_id=ctx.workspace_id,
            heartbeat=token.checkpoint,
        )
        token.checkpoint(force=True)
        now = hooks.clock()
        with open_unit_of_work(ctx) as short:
            record(
                ctx,
                HandlerUnitOfWork(short, consumers=uow.consumers),
                claimed,
                outcome,
                profile_version=profile.version,
                now=now,
            )
            short.commit()
        if outcome.deferred:
            counts["deferred"] += 1
            _log.warning(
                DEFERRED_LOG,
                extra=content_free_extra({"failure_kind": outcome.deferred}),
            )
            break
        if outcome.failure is not None:
            counts["failed"] += 1
            _log.warning(
                FAILED_LOG,
                extra=content_free_extra(
                    {"failure_kind": outcome.failure, "attempts": claimed.attempts}
                ),
            )
        elif outcome.model:
            counts["scored"] += 1
        else:
            counts["held"] += 1
    _log.info(SCORED_LOG, extra=content_free_extra(counts))
    now = hooks.clock()
    due = follow_up_at(uow, now=now)
    if due is not None:
        queue_sweep(uow, ctx.workspace_id, now=now, at=max(due, now))
