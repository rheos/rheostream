"""The tick's step handling and the schema remedy, with no database.

The tick's database calls are replaced by fakes. ``Stop`` stands in for core's
cancellation and lease-loss errors, which this package never imports: core's job hands
the tick its own classes (``wiring.tick_job``). Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.db import code_revisions, head_revision
from rheo_finance.jobs import tick as tick_module
from rheo_finance.jobs.registry import JobResult, JobSpec
from rheo_finance.reports import ops as report_ops
from rheo_finance.reports import views
from rheo_finance.settings import parse_settings

T0 = dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)
FIRST, SECOND = "finance.synthetic_first", "finance.synthetic_second"
REMEDY = "upgrade the finance module with rheo module upgrade"
BEHIND = "0001_finance_schema"


class Stop(Exception):
    """A stand-in for a cancellation or a lost lease raised inside a job."""


def context(tmp_path: Path) -> FinanceContext:
    return FinanceContext(
        settings=parse_settings(
            f'[finance.paths]\narchive = "{tmp_path / "archive"}"\n'
            f'downloads = "{tmp_path / "downloads"}"\n'
        ),
        clock=FakeClock(T0),
        paths=FinancePaths(data=tmp_path / "data"),
        role=Role.SERVICE,
        snapshot_provider=lambda _uow, _ref: None,
    )


class Repository:
    def __init__(self, _uow: object) -> None:
        pass

    def all(self) -> tuple[()]:
        return ()

    def remove(self, ids: list[object]) -> None:
        assert ids == []


class Ledger:
    """The tick's database, faked: a recorded revision, no job rows, and every
    ``finance.job.record`` dispatch kept as (job, ok, detail)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
        self.recorded: list[tuple[str, bool | None, str]] = []
        self.ran: list[str] = []
        uow = SimpleNamespace(scalar=lambda _statement: version)

        @contextmanager
        def owner_transaction(_ctx: FinanceContext) -> Iterator[Any]:
            yield uow

        def dispatch(name: str, data: Any, _role: Role, *, ctx: object) -> None:
            assert name == "finance.job.record"
            self.recorded.append((data.job, data.ok, data.detail_code))

        monkeypatch.setattr(tick_module, "owner_transaction", owner_transaction)
        monkeypatch.setattr(tick_module, "PostgresJobRepository", Repository)
        monkeypatch.setattr(tick_module, "dispatch", dispatch)
        monkeypatch.setattr(tick_module, "catalogue", SimpleNamespace(load_all=dict))

    def jobs(self, monkeypatch: pytest.MonkeyPatch, first: BaseException) -> None:
        def failing(_ctx: FinanceContext) -> JobResult:
            self.ran.append(FIRST)
            raise first

        def fine(_ctx: FinanceContext) -> JobResult:
            self.ran.append(SECOND)
            return JobResult(ok=True)

        monkeypatch.setattr(
            tick_module,
            "JOBS",
            {FIRST: JobSpec(FIRST, failing), SECOND: JobSpec(SECOND, fine)},
        )


def test_a_pass_through_error_from_a_job_stops_the_tick_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(monkeypatch, head_revision())
    raised = Stop()
    ledger.jobs(monkeypatch, raised)
    with pytest.raises(Stop) as caught:
        tick_module.tick(context(tmp_path), pass_through=(Stop,))
    assert caught.value is raised
    assert ledger.ran == [FIRST]
    assert ledger.recorded == []


def test_any_other_error_is_recorded_failed_and_the_tick_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(monkeypatch, head_revision())
    ledger.jobs(monkeypatch, ValueError("synthetic"))
    result = tick_module.tick(context(tmp_path), pass_through=(Stop,))
    assert ledger.ran == [FIRST, SECOND]
    assert ledger.recorded == [
        (FIRST, False, "failed"),
        (SECOND, True, "completed"),
        ("finance.tick", True, "completed"),
    ]
    assert [run.job for run in result.runs] == [FIRST, SECOND]


def test_with_nothing_passed_through_every_job_error_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(monkeypatch, head_revision())
    ledger.jobs(monkeypatch, Stop())
    tick_module.tick(context(tmp_path))
    assert ledger.recorded[0] == (FIRST, False, "failed")
    assert ledger.ran == [FIRST, SECOND]


def test_a_database_behind_the_code_names_cores_module_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert BEHIND in code_revisions() and BEHIND != head_revision(), "control"
    ledger = Ledger(monkeypatch, BEHIND)
    ledger.jobs(monkeypatch, Stop())
    with pytest.raises(tick_module.SchemaBehind) as caught:
        tick_module.tick(context(tmp_path))
    assert str(caught.value) == f"schema_behind: {REMEDY}"
    assert ledger.ran == []


def test_the_status_line_names_cores_module_upgrade(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    facts = {views.SCHEMA: [report_ops.Fact(BEHIND, 1, T0)]}
    assert report_ops._database(ctx, facts).endswith(f"schema behind code: {REMEDY}")
    assert report_ops._no_report_views(ctx).endswith(f"no report views yet: {REMEDY}")
