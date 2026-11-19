"""One dated next action per opportunity; never an outbound notification."""

from datetime import datetime
from typing import Any, Literal

from rheo_contracts import WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import insert, select, update

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.storage import pipeline as p


def close_pending(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    record: dict[str, Any],
    state: Literal["cancelled", "superseded"],
    at: datetime | None = None,
) -> None:
    """Called while holding the opportunity's workspace lifecycle lock. ``at`` is
    the resolution time; a closing transition passes its own, so the follow-up it
    cancels can be told apart from older ones (#464)."""
    actor = h.actor(ctx, "resolved")
    if at is not None:
        actor["resolved_at"] = at
    uow.connection.execute(
        update(p.followup_reminder)
        .where(
            p.followup_reminder.c.opportunity_id == record["id"],
            p.followup_reminder.c.state == "pending",
        )
        .values(state=state, **actor)
    )


def restore_cancelled(
    ctx: WorkspaceContext, uow: UnitOfWork, record: dict[str, Any], closed_at: datetime
) -> None:
    """On a reopen, bring back the follow-up the closing transition cancelled
    (#464): the one cancelled at or after ``closed_at``, the moment the job closed.
    A closed job can be neither scheduled nor moved to another closed stage, so
    nothing else is cancelled in between and an older cancelled follow-up stays
    as it was. The cancelled row is kept as history; a new pending one takes its
    action and due date, which may now be past (and so shows as overdue)."""
    cancelled = (
        uow.connection.execute(
            select(p.followup_reminder.c.action, p.followup_reminder.c.due_on)
            .where(
                p.followup_reminder.c.opportunity_id == record["id"],
                p.followup_reminder.c.state == "cancelled",
                p.followup_reminder.c.resolved_at >= closed_at,
            )
            .order_by(p.followup_reminder.c.resolved_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    if cancelled is None:
        return
    uow.connection.execute(
        insert(p.followup_reminder).values(
            id=uuid7(),
            opportunity_id=record["id"],
            action=cancelled["action"],
            due_on=cancelled["due_on"],
            state="pending",
            **h.actor(ctx),
        )
    )


def schedule(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.FollowupSchedule
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    if record["disposition_outcome"] is not None:
        raise OperationRefused(
            "opportunity_closed", "closed opportunity cannot be scheduled"
        )
    close_pending(ctx, uow, record, "superseded")
    uow.connection.execute(
        insert(p.followup_reminder).values(
            id=uuid7(),
            opportunity_id=record["id"],
            action=m.action,
            due_on=m.due_on,
            state="pending",
            **h.actor(ctx),
        )
    )
    h.bump(uow, record)
    return h.output(record)


def resolve(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.FollowupResolve
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    reminder = uow.connection.execute(
        select(p.followup_reminder.c.id).where(
            p.followup_reminder.c.id == m.reminder_id,
            p.followup_reminder.c.opportunity_id == record["id"],
            p.followup_reminder.c.state == "pending",
        )
    ).scalar_one_or_none()
    if reminder is None:
        raise OperationRefused("followup_not_pending", "follow-up is no longer pending")
    uow.connection.execute(
        update(p.followup_reminder)
        .where(p.followup_reminder.c.id == reminder)
        .values(state=m.outcome, **h.actor(ctx, "resolved"))
    )
    h.bump(uow, record)
    return h.output(record)
