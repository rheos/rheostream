"""Milestone generation inside the drafting sweep (``leads.jobsearch_propose``).

After its drafts, a sweep takes each queued pricing request the same way: claim it
(count the attempt, mark it ``running``, hold it for the claim window), ask the model
once for a schedule, and record the rows, the scope-fit warning (decided here, never
by the model) or the too-vague reason. The failure rules are the drafts': an account
or deployment failure gives the attempt back and every queued request waits; any other
failure counts, and the third marks the request failed; a claim whose worker died
lapses and is taken again until its attempts are spent. A result for a request that
was replaced meanwhile is dropped.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from rheo_core.runtime.oneshot import ADAPTER_UNAVAILABLE, OneShotRunner
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.backoff import backoff_for
from rheo_core.work.cancellation import CancellationToken
from sqlalchemy import and_, func, or_, select, update

from rheo_leads import model_calls
from rheo_leads.proposals import job as drafting
from rheo_leads.proposals import milestones, pricing
from rheo_leads.proposals.pipeline import AnswerUnusable
from rheo_leads.proposals.pricing_store import write_schedule
from rheo_leads.proposals.profile import StoredProposalProfile
from rheo_leads.scoring.tier1 import EXTENSION
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import proposals as s

PRICING_NOT_SET = "pricing_not_set"
INVALID_SCHEDULE = "output_invalid"


@dataclass(frozen=True)
class PriceClaim:
    opportunity_id: UUID
    attempts: int
    requested_at: datetime
    title: str
    fields: dict[str, str]
    mode: str | None


def _claimable(now: datetime) -> Any:
    t = s.job_pricing
    due = or_(t.c.retry_after.is_(None), t.c.retry_after <= now)
    return or_(
        and_(t.c.state == "queued", due),
        and_(t.c.state == "running", t.c.retry_after <= now),
    )


def claim(uow: Any, *, now: datetime, window: timedelta) -> PriceClaim | None:
    t, o = s.job_pricing, p.opportunity
    uow.connection.execute(
        update(t)
        .where(
            t.c.state == "running",
            t.c.retry_after <= now,
            t.c.attempts >= drafting.DRAFT_ATTEMPTS,
        )
        .values(
            state="failed",
            retry_after=None,
            failure_kind=drafting.CLAIM_LAPSED,
            updated_at=now,
        )
    )
    row = (
        uow.connection.execute(
            select(
                t.c.opportunity_id, t.c.attempts, t.c.requested_at, t.c.mode, o.c.title
            )
            .join(o, o.c.id == t.c.opportunity_id)
            .where(_claimable(now))
            .order_by(t.c.requested_at, t.c.opportunity_id)
            .limit(1)
            .with_for_update(of=t, skip_locked=True)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    attempts = row["attempts"] + 1
    uow.connection.execute(
        update(t)
        .where(t.c.opportunity_id == row["opportunity_id"])
        .values(
            state="running",
            attempts=attempts,
            failure_kind=None,
            retry_after=now + window,
            updated_at=now,
        )
    )
    return PriceClaim(
        opportunity_id=row["opportunity_id"],
        attempts=attempts,
        requested_at=row["requested_at"],
        title=row["title"],
        fields=drafting._fields(uow, row["opportunity_id"]),
        mode=row["mode"],
    )


def _mine(claimed: PriceClaim) -> Any:
    t = s.job_pricing
    return and_(
        t.c.opportunity_id == claimed.opportunity_id,
        t.c.state == "running",
        t.c.requested_at == claimed.requested_at,
        t.c.attempts == claimed.attempts,
    )


def _current(uow: Any, claimed: PriceClaim) -> bool:
    t = s.job_pricing
    return (
        uow.connection.execute(
            select(t.c.opportunity_id).where(_mine(claimed)).with_for_update()
        ).one_or_none()
        is not None
    )


def _budget(value: str | None) -> Decimal | None:
    try:
        return Decimal(value) if value else None
    except ArithmeticError:
        return None


def record_schedule(
    uow: Any, claimed: PriceClaim, schedule: milestones.Schedule, *, now: datetime
) -> bool:
    if not _current(uow, claimed):
        return False
    fields = claimed.fields
    warning = pricing.scope_fit_warning(
        [stage.amount for stage in schedule.stages],
        _budget(fields.get(EXTENSION + "budget_min")),
        _budget(fields.get(EXTENSION + "budget_max")),
        fields.get(EXTENSION + "job_type"),
    )
    write_schedule(uow, claimed.opportunity_id, schedule, warning=warning, now=now)
    return True


def record_wait(uow: Any, claimed: PriceClaim, *, kind: str, now: datetime) -> bool:
    if not _current(uow, claimed):
        return False
    t = s.job_pricing
    uow.connection.execute(
        update(t)
        .where(t.c.opportunity_id == claimed.opportunity_id)
        .values(state="queued", attempts=max(claimed.attempts - 1, 0))
    )
    uow.connection.execute(
        update(t)
        .where(
            t.c.state == "queued",
            or_(
                t.c.opportunity_id == claimed.opportunity_id,
                t.c.retry_after.is_(None),
                t.c.retry_after <= now + drafting.USAGE_DEFER,
            ),
        )
        .values(
            retry_after=now + drafting.USAGE_DEFER, failure_kind=kind, updated_at=now
        )
    )
    return True


def record_failure(uow: Any, claimed: PriceClaim, *, kind: str, now: datetime) -> bool:
    if not _current(uow, claimed):
        return False
    final = claimed.attempts >= drafting.DRAFT_ATTEMPTS
    uow.connection.execute(
        update(s.job_pricing)
        .where(s.job_pricing.c.opportunity_id == claimed.opportunity_id)
        .values(
            state="failed" if final else "queued",
            failure_kind=kind,
            retry_after=None
            if final
            else now + backoff_for(claimed.attempts, jitter=None),
            updated_at=now,
        )
    )
    return True


def follow_up_at(uow: Any, *, now: datetime) -> datetime | None:
    t = s.job_pricing
    if uow.connection.execute(
        select(func.count()).select_from(t).where(_claimable(now))
    ).scalar_one():
        return now
    earliest: datetime | None = uow.connection.execute(
        select(func.min(t.c.retry_after)).where(t.c.state.in_(("queued", "running")))
    ).scalar_one()
    return earliest


def price_one(
    workspace_id: UUID,
    claimed: PriceClaim,
    profile: StoredProposalProfile | None,
    *,
    runner: OneShotRunner | None,
    token: CancellationToken,
    window: timedelta,
    ctx: Any,
    today: Callable[[], Any],
) -> milestones.Schedule | Exception | str:
    """The schedule, a wait for the whole account, or a counted failure's word."""
    if profile is None:
        return drafting.PROFILE_MISSING
    policy = profile.policy.pricing
    if policy is None:
        return PRICING_NOT_SET
    description = claimed.fields.get("message", "")
    if not description.strip():
        return drafting.NO_DESCRIPTION
    if runner is None:
        return drafting._Deferred(ADAPTER_UNAVAILABLE)
    if not drafting._account_ready(workspace_id, profile.drafting_account_id):
        return drafting.ACCOUNT_UNAVAILABLE

    def hold() -> None:
        token.checkpoint(force=True)
        with open_unit_of_work(ctx) as short:
            short.connection.execute(
                update(s.job_pricing)
                .where(_mine(claimed))
                .values(retry_after=drafting.HOOKS.clock() + window)
            )
            short.commit()

    ask = drafting._asker(
        runner,
        workspace_id=workspace_id,
        account_id=profile.drafting_account_id,
        heartbeat=token.checkpoint,
        before=hold,
        track=model_calls.Tracker(ctx, claimed.opportunity_id, drafting.HOOKS.clock),
    )
    fields = claimed.fields
    try:
        answer = ask(
            "milestones",
            milestones.task(
                policy,
                title=claimed.title,
                description=description,
                budget_min=fields.get(EXTENSION + "budget_min"),
                budget_max=fields.get(EXTENSION + "budget_max"),
            ),
            milestones.SCHEMA,
        )
        return milestones.read(answer, today=today())
    except drafting._Deferred as exc:
        return exc
    except AnswerUnusable as exc:
        return exc.kind
    except ValueError:
        return INVALID_SCHEDULE


PRICED_LOG = "job_pricing_finished"
PRICING_DEFERRED_LOG = "job_pricing_deferred"
PRICING_FAILED_LOG = "job_pricing_failed"


def price_all(
    ctx: Any,
    profile: StoredProposalProfile | None,
    *,
    runner: OneShotRunner | None,
    token: CancellationToken,
    window: timedelta,
    started: datetime,
) -> None:
    """Every claimable pricing request, within what is left of the run budget."""
    from rheo_core.log_config import content_free_extra

    hooks = drafting.HOOKS
    count = 0
    while True:
        token.checkpoint()
        now = hooks.clock()
        if now - started >= drafting.RUN_BUDGET:
            break
        with open_unit_of_work(ctx) as short:
            claimed = claim(short, now=now, window=window)
            short.commit()
        if claimed is None:
            break
        outcome = price_one(
            ctx.workspace_id,
            claimed,
            profile,
            runner=runner,
            token=token,
            window=window,
            ctx=ctx,
            today=lambda: hooks.clock().date(),
        )
        now = hooks.clock()
        count += 1
        with open_unit_of_work(ctx) as short:
            if isinstance(outcome, milestones.Schedule):
                record_schedule(short, claimed, outcome, now=now)
            elif isinstance(outcome, drafting._Deferred):
                record_wait(short, claimed, kind=outcome.kind, now=now)
            else:
                record_failure(short, claimed, kind=str(outcome), now=now)
            short.commit()
        if isinstance(outcome, drafting._Deferred):
            drafting._log.warning(
                PRICING_DEFERRED_LOG,
                extra=content_free_extra({"failure_kind": outcome.kind}),
            )
            break
        if isinstance(outcome, str):
            drafting._log.warning(
                PRICING_FAILED_LOG,
                extra=content_free_extra(
                    {"failure_kind": outcome, "attempts": claimed.attempts}
                ),
            )
    drafting._log.info(PRICED_LOG, extra=content_free_extra({"unit_count": count}))
