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


# --- #548 L16: health names each set-aside reminder key -------------------------------

SYNCED = dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)


def test_reminder_errors_keep_fixed_codes_and_cap_the_list() -> None:
    from rheo_finance.jobs.service import ReminderError, reminder_errors

    rows: list[tuple[str, str, dt.datetime | None]] = [
        ("finance:alpha:p1", "not_owned", None),
        ("finance:beta:p1", "synthetic stored text", SYNCED),
    ] + [(f"finance:gamma:p{n:02d}", "refused", SYNCED) for n in range(23)]
    shown, more = reminder_errors(rows)
    assert len(rows) == 25
    assert (len(shown), more) == (20, 5)
    assert shown[0] == ReminderError(
        key="finance:alpha:p1", code="not_owned", last_success_at=None
    )
    # A stored code outside the fixed set is never shown as stored.
    assert shown[1] == ReminderError(
        key="finance:beta:p1", code="other", last_success_at=SYNCED
    )
    assert reminder_errors([]) == ([], 0)


def test_the_health_command_prints_one_line_per_set_aside_key() -> None:
    from zoneinfo import ZoneInfo

    from rheo_finance.jobs.service import HealthOut, ReminderError

    result = HealthOut(
        code="failed_due_job",
        failed=1,
        reminder_errors=[
            ReminderError(key="finance:alpha:p1", code="not_owned"),
            ReminderError(
                key="finance:beta:p1", code="refused", last_success_at=SYNCED
            ),
        ],
        more=3,
    )
    report = jobs_cli.health_report(result, ZoneInfo("UTC"))
    assert report.headline == (
        "finance jobs health: failed_due_job failed=1 pending=0 skipped_off=0",
    )
    assert report.detail == (
        "reminder finance:alpha:p1: not_owned, never synced",
        "reminder finance:beta:p1: refused, last synced 2063-01-18 12:00 UTC",
        "and 3 more",
    )
    plain = jobs_cli.health_report(HealthOut(code="healthy"), ZoneInfo("UTC"))
    assert plain.lines() == [
        "finance jobs health: healthy failed=0 pending=0 skipped_off=0"
    ]


def test_the_health_command_prints_twenty_keys_and_the_rest_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full capped result through ``finance jobs health``: twenty keys and the
    count go to the report's detail, so the headline budget never refuses them."""
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from rheo_finance.jobs import service
    from rheo_finance.jobs.service import REMINDER_ERRORS_SHOWN, reminder_errors

    rows = [(f"finance:gamma:p{n:02d}", "refused", SYNCED) for n in range(25)]
    shown, more = reminder_errors(rows)
    result = service.HealthOut(
        code="failed_due_job", failed=1, reminder_errors=shown, more=more
    )
    stand_in = SimpleNamespace(settings=SimpleNamespace(timezone=ZoneInfo("UTC")))
    monkeypatch.setattr(jobs_cli, "_context", lambda: stand_in)
    monkeypatch.setattr(service, "health", lambda _ctx: result)

    assert main(["jobs", "health"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[:2] == [
        "finance jobs health: failed_due_job failed=1 pending=0 skipped_off=0",
        "",
    ]
    keys = [f"finance:gamma:p{n:02d}" for n in range(REMINDER_ERRORS_SHOWN)]
    assert lines[2:-1] == [
        f"reminder {key}: refused, last synced 2063-01-18 12:00 UTC" for key in keys
    ]
    assert lines[-1] == "and 5 more"
