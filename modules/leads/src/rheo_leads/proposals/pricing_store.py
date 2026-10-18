"""Reading and writing a job's pricing: its mode and its milestone rows.

Shared by the pricing sweep and the drafting sweep (whose Pricing stage writes the
same rows), so neither imports the other.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_leads.proposals.milestones import Schedule
from rheo_leads.storage import proposals as s


@dataclass(frozen=True)
class Priced:
    """What a draft needs to know about a job's pricing."""

    mode: str | None = None
    rows: tuple[tuple[str, Decimal], ...] = ()


def read_priced(uow: UnitOfWork, identifier: UUID) -> Priced:
    mode = uow.connection.execute(
        select(s.job_pricing.c.mode).where(s.job_pricing.c.opportunity_id == identifier)
    ).scalar_one_or_none()
    m = s.proposal_milestone
    rows = tuple(
        (r.title, Decimal(r.amount))
        for r in uow.connection.execute(
            select(m.c.title, m.c.amount)
            .where(m.c.opportunity_id == identifier)
            .order_by(m.c.position)
        )
    )
    return Priced(mode=mode, rows=rows)


def milestone_rows(uow: UnitOfWork, identifier: UUID) -> list[dict[str, Any]]:
    m = s.proposal_milestone
    return [
        dict(r)
        for r in uow.connection.execute(
            select(m).where(m.c.opportunity_id == identifier).order_by(m.c.position)
        ).mappings()
    ]


def write_schedule(
    uow: UnitOfWork,
    identifier: UUID,
    schedule: Schedule,
    *,
    warning: str | None,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> None:
    """Replace the job's milestones with ``schedule`` and mark the pricing ready.

    A schedule with stages chooses milestone pricing. A too-vague answer clears the
    rows (stale ones would come back on a reload) and keeps the mode as it was."""
    values: dict[str, Any] = {
        "state": "ready",
        "warning": warning,
        "too_vague": schedule.too_vague,
        "generated_at": now,
        "retry_after": None,
        "failure_kind": None,
        "updated_at": now,
        **(extra or {}),
    }
    if schedule.stages:
        values["mode"] = "milestones"
    table = s.job_pricing
    uow.connection.execute(
        pg_insert(table)
        .values(opportunity_id=identifier, requested_at=now, attempts=0, **values)
        .on_conflict_do_update(index_elements=[table.c.opportunity_id], set_=values)
    )
    m = s.proposal_milestone
    uow.connection.execute(delete(m).where(m.c.opportunity_id == identifier))
    if schedule.stages:
        uow.connection.execute(
            insert(m),
            [
                {
                    "id": uuid7(),
                    "opportunity_id": identifier,
                    "position": index,
                    "title": stage.title,
                    "description": stage.description or None,
                    "amount": stage.amount,
                    "due_on": stage.due_on,
                    "updated_at": now,
                }
                for index, stage in enumerate(schedule.stages)
            ],
        )


def amounts(rows: Sequence[dict[str, Any]]) -> list[Decimal]:
    return [Decimal(r["amount"]) for r in rows]
