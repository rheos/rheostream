"""The Scoring view: where recent jobs are in scoring, and the model call log.

``leads.ui.scoring`` answers one bounded read:

- ``status``: what scoring is doing now. ``off`` (job search is off), ``no_profile``
  (nothing to score against), ``running`` (a model call is in flight: its purpose,
  job and start), ``paused`` (the account's usage limit, or another account-side
  failure, holds every waiting job until a time), or ``idle`` with the last call.
- the queue's totals: claimable jobs (``queued_total``), jobs waiting out a backoff,
  failed jobs, and an upper bound on the time to clear (the median of recent scoring
  calls for every queued job, though tier 1 holds some without a call).
- ``batches``: Job search jobs created in the last :data:`WINDOW`, grouped into
  imports by the gaps between them, each with its stage (:func:`stage_of`).
- ``calls``: the newest model calls, and ``summary``: calls, tokens, median time,
  failures and deferrals by purpose and model over :data:`SUMMARY_WINDOW`.
- ``feed_runs``: the job feed's latest :data:`MAX_FEED_RUNS` runs from its run
  history, with why their detail reads failed.

Everything is content-free apart from job titles, which the job queue already
shows: no posting text, prompt or answer. Tokens and times, never money: the calls
run on a subscription login, not a metered API.

``leads.jobscoring.retry_failed`` is the owner's re-queue: open jobs whose scoring
failed go back in line with a fresh attempt count, as setting the profile again does.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Annotated, Any, Final
from uuid import UUID

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.redaction.tiers import SensitivityTier, Tiered
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import and_, extract, func, select, true, update

from rheo_leads import model_calls
from rheo_leads import pipeline_common as h
from rheo_leads.pipeline_contracts import Empty, Strict
from rheo_leads.references import ref
from rheo_leads.scoring import job as scoring
from rheo_leads.storage import jobfeed as jf
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import scoring as s
from rheo_leads.storage import tables as t

WINDOW: Final = timedelta(hours=48)
SUMMARY_WINDOW: Final = timedelta(days=7)
BATCH_GAP: Final = timedelta(minutes=10)
"""A gap longer than this between two jobs' arrival starts a new import."""
MAX_JOBS: Final = 500
MAX_CALLS: Final = 100
ETA_SAMPLE: Final = 30
STALE: Final = timedelta(seconds=scoring.HARD_DEADLINE_SECONDS) + scoring.CLAIM_MARGIN
"""A call still ``running`` after this never finished: its worker stopped."""
STAGES: Final = (
    "scoring",
    "queued",
    "waiting",
    "retrying",
    "paused",
    "scored",
    "needs_posting",
    "failed",
    "held",
    "closed",
    "untracked",
)


class ViewOutput(Strict):
    data: Annotated[dict[str, Any], Tiered(SensitivityTier.RESTRICTED)]


class RetryOutput(Strict):
    count: int


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def stage_of(
    job: Mapping[str, Any], *, running: bool, claimable: bool, now: datetime
) -> str:
    """Where one job is: a model call in flight for it, its scoring state, and its
    current assessment."""
    if running:
        return "scoring"
    state = job["state"]
    if state is None:
        return "untracked" if job["open"] else "closed"
    if state == "failed":
        return "failed"
    if state == "pending":
        if not job["open"]:
            return "closed"
        if claimable:
            return "queued"
        kind = job["failure_kind"]
        if kind in scoring.JOB_FAILURES:
            return "retrying"
        if kind is not None:
            return "paused"
        # Claimed by a sweep with no call recorded: tier 1 is deciding, or the
        # sweep stopped and the claim runs out at ``retry_after``.
        return "waiting"
    if job["score"] is not None:
        return "scored"
    explanation = job["explanation"] or ""
    if explanation.startswith(scoring.HELD_LINE):
        return "held"
    if explanation.startswith(scoring.UNDESCRIBED_LINE):
        return "needs_posting"
    return "failed" if job["fit"] == "needs_information" else "held"


def _claim_order(uow: UnitOfWork, now: datetime) -> tuple[list[UUID], int]:
    """The first claimable open jobs in the order the sweep claims them (newest
    first, so the window's jobs lead), and how many are claimable in all."""
    state, o = s.job_score_state, p.opportunity
    open_ids = scoring._open_jobs().with_only_columns(o.c.id)
    claimable = and_(scoring._claimable(now), state.c.opportunity_id.in_(open_ids))
    first: list[UUID] = list(
        uow.connection.execute(
            select(state.c.opportunity_id)
            .join(o, o.c.id == state.c.opportunity_id)
            .where(claimable)
            .order_by(o.c.created_at.desc(), o.c.id.desc())
            .limit(MAX_JOBS + 1)
        ).scalars()
    )
    total: int = uow.connection.execute(
        select(func.count()).select_from(state).where(claimable)
    ).scalar_one()
    return first, total


def _running(uow: UnitOfWork, now: datetime) -> list[dict[str, Any]]:
    """Calls in flight, newest first."""
    call = s.model_call
    return [
        dict(r)
        for r in uow.connection.execute(
            select(call.c.purpose, call.c.opportunity_id, call.c.started_at)
            .where(call.c.outcome == "running", call.c.started_at >= now - STALE)
            .order_by(call.c.started_at.desc())
        ).mappings()
    ]


def _jobs(uow: UnitOfWork, now: datetime) -> list[dict[str, Any]]:
    o, stage, state, qual = (
        p.opportunity,
        p.preset_stage,
        s.job_score_state,
        p.qualification,
    )
    # The current assessment, once per job (the ``qualification_current`` index).
    current = (
        select(qual.c.score, qual.c.fit, qual.c.explanation, qual.c.created_at)
        .where(qual.c.opportunity_id == o.c.id)
        .order_by(
            qual.c.input_revision.desc(), qual.c.created_at.desc(), qual.c.id.desc()
        )
        .limit(1)
        .lateral("current")
    )
    query = (
        select(
            o.c.id,
            o.c.title,
            o.c.created_at,
            (stage.c.kind == "open").label("open"),
            t.observation.c.transport.label("source"),
            state.c.state,
            state.c.attempts,
            state.c.retry_after,
            state.c.failure_kind,
            current.c.score,
            current.c.fit,
            current.c.explanation,
            current.c.created_at.label("assessed_at"),
        )
        .join(
            stage,
            and_(
                stage.c.preset_id == o.c.preset_id,
                stage.c.version == o.c.preset_version,
                stage.c.stage_id == o.c.stage_id,
            ),
        )
        .outerjoin(state, state.c.opportunity_id == o.c.id)
        .outerjoin(t.observation, t.observation.c.id == o.c.source_observation_id)
        .outerjoin(current, true())
        .where(
            o.c.preset_id == h.JOBSEARCH_PRESET_ID,
            o.c.created_at >= now - WINDOW,
        )
        .order_by(o.c.created_at.desc(), o.c.id.desc())
        .limit(MAX_JOBS)
    )
    return [dict(r) for r in uow.connection.execute(query).mappings()]


def _batches(
    jobs: Sequence[Mapping[str, Any]],
    *,
    running: set[UUID],
    position: Mapping[UUID, int],
    now: datetime,
) -> list[dict[str, Any]]:
    """Newest import first; within one, in-flight and queued work first."""
    batches: list[dict[str, Any]] = []
    previous: datetime | None = None
    for job in sorted(jobs, key=lambda j: j["created_at"]):
        if previous is None or job["created_at"] - previous > BATCH_GAP:
            batches.append({"jobs": []})
        previous = job["created_at"]
        identifier = job["id"]
        stage = stage_of(
            job,
            running=identifier in running,
            claimable=identifier in position,
            now=now,
        )
        batches[-1]["jobs"].append(
            {
                "id": str(identifier),
                "ref": ref("opportunity", identifier),
                "title": job["title"],
                "created_at": _iso(job["created_at"]),
                "source": job["source"],
                "stage": stage,
                "position": position.get(identifier) if stage == "queued" else None,
                "score": job["score"] if stage == "scored" else None,
                "assessed_at": _iso(job["assessed_at"]),
                "retry_at": _iso(job["retry_after"])
                if stage in ("retrying", "paused", "waiting")
                else None,
                "attempts": job["attempts"],
                "failure_kind": job["failure_kind"]
                if stage in ("retrying", "paused", "failed")
                else None,
            }
        )
    for batch in batches:
        rows = batch["jobs"]
        batch["started_at"] = rows[0]["created_at"]
        batch["sources"] = sorted({r["source"] for r in rows if r["source"]})
        batch["counts"] = {
            name: sum(1 for r in rows if r["stage"] == name) for name in STAGES
        }
        rows.sort(
            key=lambda r: (
                STAGES.index(r["stage"]),
                r["position"] or 0,
                -(r["score"] or 0),
            )
        )
    batches.reverse()
    return batches


def _calls(uow: UnitOfWork, now: datetime) -> list[dict[str, Any]]:
    call, o = s.model_call, p.opportunity
    found = []
    for r in uow.connection.execute(
        select(call, o.c.title)
        .outerjoin(o, o.c.id == call.c.opportunity_id)
        .order_by(call.c.started_at.desc(), call.c.id.desc())
        .limit(MAX_CALLS)
    ).mappings():
        outcome = r["outcome"]
        if outcome == "running" and r["started_at"] < now - STALE:
            outcome = "interrupted"
        finished = r["finished_at"]
        found.append(
            {
                "id": str(r["id"]),
                "purpose": r["purpose"],
                "model_id": r["model_id"],
                "started_at": _iso(r["started_at"]),
                "seconds": (finished - r["started_at"]).total_seconds()
                if finished is not None
                else None,
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "outcome": outcome,
                "failure_kind": r["failure_kind"],
                "ref": ref("opportunity", r["opportunity_id"])
                if r["opportunity_id"] is not None
                else None,
                "title": r["title"],
            }
        )
    return found


MAX_FEED_RUNS: Final = 12
"""The job feed's most recent runs the view shows."""


def _feed_runs(uow: UnitOfWork) -> list[dict[str, Any]]:
    """The collector's latest runs, newest first: outcome, the counts a person
    reads, and why detail reads failed (fixed codes only)."""
    run = jf.jobfeed_run
    return [
        {
            "id": str(r["id"]),
            "finished_at": _iso(r["finished_at"]),
            "outcome": r["outcome"],
            "stop_code": r["stop_code"],
            "fetched": r["fetched"],
            "imported": r["imported"],
            "refreshed": r["refreshed"],
            "detail_reads": r["detail_reads"],
            "errors": r["errors"],
            "read_failures": dict(r["read_failures"] or {}),
        }
        for r in uow.connection.execute(
            select(run)
            .order_by(run.c.finished_at.desc(), run.c.id.desc())
            .limit(MAX_FEED_RUNS)
        ).mappings()
    ]


def _summary(uow: UnitOfWork, now: datetime) -> list[dict[str, Any]]:
    call = s.model_call
    seconds = extract("epoch", call.c.finished_at - call.c.started_at)
    rows = uow.connection.execute(
        select(
            call.c.purpose,
            call.c.model_id,
            func.count().label("calls"),
            func.coalesce(func.sum(call.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(call.c.output_tokens), 0).label("output_tokens"),
            func.percentile_cont(0.5)
            .within_group(seconds)
            .filter(call.c.outcome == "answered")
            .label("median_seconds"),
            func.count().filter(call.c.outcome == "failed").label("failed"),
            func.count().filter(call.c.outcome == "deferred").label("deferred"),
        )
        .where(call.c.started_at >= now - SUMMARY_WINDOW)
        .group_by(call.c.purpose, call.c.model_id)
        .order_by(func.count().desc(), call.c.purpose, call.c.model_id)
    ).mappings()
    return [
        {
            "purpose": r["purpose"],
            "model_id": r["model_id"],
            "calls": r["calls"],
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "median_seconds": float(r["median_seconds"])
            if r["median_seconds"] is not None
            else None,
            "failed": r["failed"],
            "deferred": r["deferred"],
        }
        for r in rows
    ]


def _median_scoring_seconds(uow: UnitOfWork) -> float | None:
    """The median of the newest answered scoring calls; ``None`` under three."""
    call = s.model_call
    rows = uow.connection.execute(
        select(call.c.started_at, call.c.finished_at)
        .where(call.c.purpose == model_calls.SCORING, call.c.outcome == "answered")
        .order_by(call.c.started_at.desc())
        .limit(ETA_SAMPLE)
    ).all()
    spans = [(r.finished_at - r.started_at).total_seconds() for r in rows]
    return median(spans) if len(spans) >= 3 else None


def _status(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    running: Sequence[Mapping[str, Any]],
    titles: Mapping[UUID, str],
    now: datetime,
) -> dict[str, Any]:
    call, state = s.model_call, s.job_score_state
    last: datetime | None = uow.connection.execute(
        select(func.max(call.c.finished_at))
    ).scalar_one()
    status: dict[str, Any] = {"state": "idle", "last_call_at": _iso(last)}
    if not h.jobsearch_enabled(ctx, uow):
        status["state"] = "off"
        return status
    if not scoring.profile_set(uow):
        status["state"] = "no_profile"
        return status
    if running:
        # A scoring call is named before a drafting one when both run.
        current = min(running, key=lambda r: r["purpose"] != model_calls.SCORING)
        identifier = current["opportunity_id"]
        status.update(
            state="running",
            purpose=current["purpose"],
            started_at=_iso(current["started_at"]),
            ref=ref("opportunity", identifier) if identifier is not None else None,
            title=titles.get(identifier) if identifier is not None else None,
        )
        return status
    o = p.opportunity
    open_ids = scoring._open_jobs().with_only_columns(o.c.id)
    paused = uow.connection.execute(
        select(state.c.retry_after, state.c.failure_kind)
        .where(
            state.c.state == "pending",
            state.c.retry_after > now,
            state.c.failure_kind.is_not(None),
            state.c.failure_kind.not_in(sorted(scoring.JOB_FAILURES)),
            state.c.opportunity_id.in_(open_ids),
        )
        .order_by(state.c.retry_after)
        .limit(1)
    ).one_or_none()
    if paused is not None:
        status.update(
            state="paused",
            until=_iso(paused.retry_after),
            failure_kind=paused.failure_kind,
        )
    return status


def view(ctx: WorkspaceContext, uow: UnitOfWork, m: Empty) -> ViewOutput:
    h.require(ctx)
    now = datetime.now(UTC)
    order, claimable = _claim_order(uow, now)
    running = _running(uow, now)
    jobs = _jobs(uow, now)
    titles = {j["id"]: j["title"] for j in jobs}
    missing = [
        r["opportunity_id"]
        for r in running
        if r["opportunity_id"] is not None and r["opportunity_id"] not in titles
    ]
    if missing:
        o = p.opportunity
        titles.update(
            (r.id, r.title)
            for r in uow.connection.execute(
                select(o.c.id, o.c.title).where(o.c.id.in_(missing))
            )
        )
    scoring_now = {
        r["opportunity_id"]
        for r in running
        if r["purpose"] == model_calls.SCORING and r["opportunity_id"] is not None
    }
    position = {
        identifier: index + 1
        for index, identifier in enumerate(i for i in order if i not in scoring_now)
    }
    state, o = s.job_score_state, p.opportunity
    open_ids = scoring._open_jobs().with_only_columns(o.c.id)
    totals: dict[str, int] = dict(
        uow.connection.execute(
            select(state.c.state, func.count())
            .where(state.c.opportunity_id.in_(open_ids))
            .group_by(state.c.state)
        ).all()
    )
    per_call = _median_scoring_seconds(uow)
    # A job being scored is claimed, so it is never counted as claimable.
    queued = claimable
    return ViewOutput(
        data={
            "generated_at": _iso(now),
            "window_hours": int(WINDOW.total_seconds() // 3600),
            "summary_days": SUMMARY_WINDOW.days,
            "status": _status(ctx, uow, running=running, titles=titles, now=now),
            "queued_total": queued,
            "waiting_total": max(totals.get("pending", 0) - claimable, 0),
            "failed_total": totals.get("failed", 0),
            "median_call_seconds": per_call,
            "eta_seconds": round(per_call * queued)
            if per_call is not None and queued
            else None,
            "batches": _batches(jobs, running=scoring_now, position=position, now=now),
            "calls": _calls(uow, now),
            "summary": _summary(uow, now),
            "feed_runs": _feed_runs(uow),
        }
    )


def retry_failed(ctx: WorkspaceContext, uow: UnitOfWork, m: Empty) -> RetryOutput:
    h.require(ctx)
    if not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is off")
    state, o = s.job_score_state, p.opportunity
    open_ids = scoring._open_jobs().with_only_columns(o.c.id)
    now = datetime.now(UTC)
    count = uow.connection.execute(
        update(state)
        .where(state.c.state == "failed", state.c.opportunity_id.in_(open_ids))
        .values(
            state="pending",
            attempts=0,
            retry_after=None,
            failure_kind=None,
            updated_at=now,
        )
    ).rowcount
    if count:
        scoring.queue_sweep(uow, ctx.workspace_id, now=now)
    return RetryOutput(count=count)


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (
        OperationDeclaration(
            name="leads.ui.scoring",
            safety_class=SafetyClass.READ,
            idempotency=Idempotency.NONE,
            roles=frozenset({Role.OWNER, Role.MEMBER}),
            input_model=Empty,
            output=ViewOutput,
        ),
        view,
    ),
    (
        OperationDeclaration(
            name="leads.jobscoring.retry_failed",
            safety_class=SafetyClass.MUTATE,
            # It spends the owner's scoring account's model time.
            roles=frozenset({Role.OWNER}),
            input_model=Empty,
            output=RetryOutput,
            # A second retry finds nothing failed and counts zero.
            idempotency=Idempotency.NATURAL,
            audit=AuditSpec(subject_field=None),
        ),
        retry_failed,
    ),
)
