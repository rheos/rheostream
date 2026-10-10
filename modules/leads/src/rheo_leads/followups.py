"""One dated next action per opportunity; never an outbound notification."""

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
) -> None:
    """Called while holding the opportunity's workspace lifecycle lock."""
    uow.connection.execute(
        update(p.followup_reminder)
        .where(
            p.followup_reminder.c.opportunity_id == record["id"],
            p.followup_reminder.c.state == "pending",
        )
        .values(state=state, **h.actor(ctx, "resolved"))
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
