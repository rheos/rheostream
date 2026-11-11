"""Schedule health through the CLI and persisted synthetic job history.

The foreground scheduler (``serve``, ``run_once``, ``jobs tick``, ``db locked``) was
not imported into rheoStream, so its tests were not either; where a health test
needs a tick, it runs ``jobs.tick.tick`` directly, the function core's scheduled job
will run.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select

from rheo_finance.cli import jobs as jobs_cli
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.jobs.tick import tick
from rheo_finance.storage.schema import job_run
from tests.jobs.test_tick import configured


def test_health_distinguishes_first_tick_and_off_sinks(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    assert main(["jobs", "health"]) == 1
    assert "no_first_tick" in capsys.readouterr().out
    tick(ctx)
    assert main(["jobs", "health"]) == 0
    out = capsys.readouterr().out
    assert "healthy" in out and "skipped_off=2" in out
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    clock.advance(dt.timedelta(minutes=60, seconds=1))
    assert main(["jobs", "health"]) == 1
    assert "stale_tick" in capsys.readouterr().out


def test_health_connection_failure_is_fixed_code(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
) -> None:
    from sqlalchemy.engine import make_url

    from rheo_finance.jobs.service import health
    from rheo_finance.operations import with_databases

    ctx = rolled_back_context(Role.SERVICE)
    unavailable = make_url(
        "postgresql+psycopg://synthetic:synthetic-secret@127.0.0.1:1/unusable_test?connect_timeout=1"
    )
    ctx = with_databases(ctx, owner_url=unavailable, reader_url=unavailable)
    result = health(ctx)
    assert result.code == "database_unreachable"
    assert "synthetic-secret" not in result.model_dump_json()


def test_health_pending_is_distinct_from_skipped_off_and_missing_history(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
) -> None:
    from rheo_finance.jobs.ops import RecordIn
    from rheo_finance.jobs.service import health
    from rheo_finance.operations import dispatch

    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    assert health(ctx).code == "no_first_tick"
    tick(ctx)
    now = ctx.clock.now_utc()
    dispatch(
        "finance.job.record",
        RecordIn(
            job="finance.reminders_sync",
            started_at=now,
            finished_at=now,
            ok=None,
            detail_code="pending: current not configured",
        ),
        Role.SERVICE,
        ctx=ctx,
    )
    out = health(ctx)
    assert out.code == "healthy" and out.failed == 0
    assert out.pending == 1 and out.skipped_off == 1


def test_dispatch_and_owner_transaction_refuse_at_wait_zero_under_another_writer(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import AbstractContextManager

    from sqlalchemy import text

    from rheo_finance import db, operations
    from rheo_finance.jobs.ops import RecordIn
    from rheo_finance.jobs.service import health

    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)

    def bounded(connection: Connection) -> AbstractContextManager[None]:
        return db.writer_lock(connection, wait_seconds=0)

    monkeypatch.setattr(operations, "writer_lock", bounded)
    now = ctx.clock.now_utc()
    # Another process's writer: a session lock on the same key, on its own connection.
    with owner_engine.connect() as other:
        other.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": db.WRITER_LOCK_KEY}
        )
        with pytest.raises(db.WriterBusy):
            operations.dispatch(
                "finance.job.record",
                RecordIn(
                    job="finance.tick",
                    started_at=now,
                    finished_at=now,
                    ok=True,
                    detail_code="completed",
                ),
                Role.SERVICE,
                ctx=ctx,
            )
        with pytest.raises(db.WriterBusy), operations.owner_transaction(ctx):
            pytest.fail("owner transaction entered while another writer held the lock")
        assert health(ctx).code == "no_first_tick"
        assert owner_conn.execute(select(job_run)).all() == []
