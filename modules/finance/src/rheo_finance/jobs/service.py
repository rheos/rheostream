"""Schedule health over the tick history: ``finance.job.health`` and ``health``.

Health also names each reminder key whose last sync set it aside (ADR-0027), with its
fixed code, read from ``sink_ticket`` on the owner connection.

The foreground scheduler that once lived here (``serve``, ``run_once`` and the
signal handling around them) was not imported into rheoStream: core's worker runs
the tick as a scheduled job.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from rheo_finance import db
from rheo_finance.clock import as_utc
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.jobs.registry import JOBS, TICK_JOB
from rheo_finance.jobs.store import PostgresJobRepository
from rheo_finance.operations import OperationSpec, dispatch, register
from rheo_finance.sinks.current import CURRENT_CODES
from rheo_finance.storage.schema import sink_ticket
from rheo_finance.tier import Tier

HealthCode = Literal[
    "healthy",
    "database_unreachable",
    "database_error",
    "schema_behind",
    "schema_unknown",
    "no_first_tick",
    "stale_tick",
    "failed_tick",
    "failed_due_job",
]


class HealthIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


REMINDER_ERRORS_SHOWN: Final = 20


class ReminderError(BaseModel):
    """A reminder key whose last sync refused it (ADR-0027): its key, its fixed code
    (``other`` for a stored code outside ``CURRENT_CODES``) and when it last synced."""

    model_config = ConfigDict(frozen=True)
    key: Annotated[str, Tiered(Tier.PUBLIC)]
    code: Annotated[str, Tiered(Tier.PUBLIC)]
    last_success_at: Annotated[dt.datetime | None, Tiered(Tier.PUBLIC)] = None


class HealthOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: Annotated[HealthCode, Tiered(Tier.PUBLIC)]
    failed: Annotated[int, Tiered(Tier.PUBLIC)] = 0
    pending: Annotated[int, Tiered(Tier.PUBLIC)] = 0
    skipped_off: Annotated[int, Tiered(Tier.PUBLIC)] = 0
    # The set-aside reminder keys, by key, at most ``REMINDER_ERRORS_SHOWN``; ``more``
    # counts the rest.
    reminder_errors: Annotated[list[ReminderError], Tiered(Tier.PUBLIC)] = []
    more: Annotated[int, Tiered(Tier.PUBLIC)] = 0


def reminder_errors(
    rows: Iterable[tuple[str, str, dt.datetime | None]],
) -> tuple[list[ReminderError], int]:
    """The first ``REMINDER_ERRORS_SHOWN`` of ``rows`` (key, stored code, last
    success), each code normalised to a fixed one, and how many more there are."""
    found = [
        ReminderError(
            key=key,
            code=code if code in CURRENT_CODES else "other",
            last_success_at=last_success_at,
        )
        for key, code, last_success_at in rows
    ]
    return found[:REMINDER_ERRORS_SHOWN], max(0, len(found) - REMINDER_ERRORS_SHOWN)


def _health(ctx: FinanceContext, uow: UnitOfWork, _data: HealthIn) -> HealthOut:
    version_table = uow.scalar(
        text("SELECT to_regclass('finance.alembic_version_finance')")
    )
    version = (
        None
        if version_table is None
        else uow.scalar(text("SELECT version_num FROM finance.alembic_version_finance"))
    )
    if version != db.head_revision():
        code: HealthCode = (
            "schema_unknown"
            if version is not None and version not in db.code_revisions()
            else "schema_behind"
        )
        return HealthOut(code=code)
    latest = {row.job: row for row in PostgresJobRepository(uow).all()}
    jobs = [latest[name] for name in JOBS if name in latest]
    counts = {
        "failed": sum(row.ok is False for row in jobs),
        "pending": sum(
            row.ok is None and row.detail_code != "skipped: sink off" for row in jobs
        ),
        "skipped_off": sum(
            row.ok is None and row.detail_code == "skipped: sink off" for row in jobs
        ),
    }
    last_tick = latest.get(TICK_JOB)
    if last_tick is None:
        code = "no_first_tick"
    elif last_tick.ok is not True:
        code = "failed_tick"
    elif (as_utc(ctx.clock.now_utc()) - last_tick.finished_at).total_seconds() > (
        ctx.settings.get_int("finance.jobs.tick_stale_minutes") * 60
    ):
        code = "stale_tick"
    elif counts["failed"]:
        code = "failed_due_job"
    else:
        code = "healthy"
    errors, more = reminder_errors(
        (row.source_ref, row.last_error_code, row.last_success_at)
        for row in uow.execute(
            select(
                sink_ticket.c.source_ref,
                sink_ticket.c.last_error_code,
                sink_ticket.c.last_success_at,
            )
            .where(sink_ticket.c.last_error_code.is_not(None))
            .order_by(sink_ticket.c.source_ref)
        )
    )
    return HealthOut(code=code, **counts, reminder_errors=errors, more=more)


register(
    OperationSpec(
        name="finance.job.health",
        input_model=HealthIn,
        output_model=HealthOut,
        handler=_health,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description="Read fixed schedule health codes and counts.",
    )
)


def health(ctx: FinanceContext) -> HealthOut:
    try:
        out = dispatch("finance.job.health", {}, ctx.role, ctx=ctx)
        assert isinstance(out, HealthOut)
        return out
    except Exception as exc:
        return HealthOut(
            code="database_unreachable" if db.unreachable(exc) else "database_error"
        )
