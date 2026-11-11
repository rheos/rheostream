"""Schedule health over the tick history: ``finance.job.health`` and ``health``.

The foreground scheduler that once lived here (``serve``, ``run_once`` and the
signal handling around them) was not imported into rheoStream: core's worker runs
the tick as a scheduled job.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from rheo_finance import db
from rheo_finance.clock import as_utc
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.jobs.registry import JOBS, TICK_JOB
from rheo_finance.jobs.store import PostgresJobRepository
from rheo_finance.operations import OperationSpec, dispatch, register
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


class HealthOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: Annotated[HealthCode, Tiered(Tier.PUBLIC)]
    failed: Annotated[int, Tiered(Tier.PUBLIC)] = 0
    pending: Annotated[int, Tiered(Tier.PUBLIC)] = 0
    skipped_off: Annotated[int, Tiered(Tier.PUBLIC)] = 0


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
    return HealthOut(code=code, **counts)


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
