"""The ticker's declared interval and opt-in gate, and ``ensure_module_schedule``.

Seams under test: ``run_due_schedules`` over a real workspace database and the real job
queue (``core.job``), with a fixture module loaded through the harness's one loading
recipe; ``ensure_module_schedule`` on a workspace that has no row. Assertions read the
rows the ticker wrote, never a private helper.

- A gated two-hour schedule with the gate closed enqueues nothing across consecutive
  due ticks and still advances ``next_run_at`` by the interval. Closed means the
  workspace has not itself set the key to true: no row (the key's package default is
  ``True``, which must not count) and a row set to ``false`` are both closed.
- With the workspace row set to ``true``, exactly one job per due interval.
- A row with no declaration, a declaration with neither field, and the core's own daily
  ``core.retention_sweep`` keep the one-day, ungated behaviour.

**The probe's key is added to the settings registry the resolver reads, for the test
only.** ``loaded_probe_modules`` deliberately keeps a fixture's settings keys out of the
process-wide registry; the ticker resolves through it, so each test rebinds the
resolver's registry to a copy of the real one plus the probe key, the way the harness
rebinds the loader's.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import PROBE_ENTRY_POINTS, loaded_probe_modules, manifest
from rheo_core.modules import ENTRY_POINT_GROUP, Schedule
from rheo_core.settings import resolver as resolver_module
from rheo_core.settings.schema import (
    REGISTRY,
    KeySpec,
    Scope,
    SettingsRegistry,
    ValueType,
    encode_text,
)
from rheo_core.storage import work_tables
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    ScheduleUndeclared,
    ensure_module_schedule,
    run_due_schedules,
)
from sqlalchemy import Connection, Engine, Row, func, insert, select, update

pytestmark = pytest.mark.postgres

GATE_PROBE_ID: Final = "gate_probe"
GATE_KEY: Final = f"{GATE_PROBE_ID}.capabilities.collect"
GATED: Final = "collect"
GATED_KIND: Final = f"{GATE_PROBE_ID}.collect"
DAILY: Final = "digest"
DAILY_KIND: Final = f"{GATE_PROBE_ID}.digest"
INTERVAL: Final = timedelta(hours=2)
TICKS: Final = 4

GATE_SPEC: Final = KeySpec(
    key=GATE_KEY,
    type=ValueType.BOOL,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=False,
    default=True,
)
"""Default ``True``, like the job-search capability: no default may open the gate."""

GATE_MANIFEST: Final = manifest(
    GATE_PROBE_ID,
    configuration_schema=(GATE_SPEC,),
    schedules=(
        Schedule(
            name=GATED,
            job_kind=GATED_KIND,
            cron="0 */2 * * *",
            enabled_by_default=False,
            interval_seconds=int(INTERVAL.total_seconds()),
            gate_setting=GATE_KEY,
        ),
        Schedule(
            name=DAILY,
            job_kind=DAILY_KIND,
            cron="0 3 * * *",
            enabled_by_default=False,
        ),
    ),
)


@pytest.fixture
def gate_probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fixture module loaded, and its key resolvable by the ticker."""
    monkeypatch.setitem(
        PROBE_ENTRY_POINTS,
        GATE_PROBE_ID,
        EntryPoint(
            name=GATE_PROBE_ID,
            value=f"{__name__}:GATE_MANIFEST",
            group=ENTRY_POINT_GROUP,
        ),
    )
    registry = SettingsRegistry()
    for spec in REGISTRY.specs():
        registry.register(spec, origin=REGISTRY.origin_of(spec.key))
    registry.register(GATE_SPEC, origin=GATE_PROBE_ID)
    monkeypatch.setattr(resolver_module, "REGISTRY", registry)
    with loaded_probe_modules(monkeypatch, GATE_PROBE_ID):
        yield


def _engine(cluster: ClusterSession, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def _start() -> datetime:
    """Well clear of the migration-seeded retention row's next run (a day out)."""
    return datetime.now(UTC).replace(microsecond=0)


def _set_gate(connection: Connection, value: bool) -> None:
    upsert_workspace_setting(
        connection,
        key=GATE_KEY,
        value=encode_text(GATE_SPEC, value),
        value_type=ValueType.BOOL,
        updated_by=None,
    )


def _jobs(connection: Connection, kind: str) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(work_tables.job)
            .where(work_tables.job.c.kind == kind)
        ).scalar_one()
    )


def _row(connection: Connection, module_id: str, name: str) -> Row[Any]:
    return connection.execute(
        select(work_tables.schedule).where(
            work_tables.schedule.c.module_id == module_id,
            work_tables.schedule.c.name == name,
        )
    ).one()


def _rows(connection: Connection, module_id: str, name: str) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(work_tables.schedule)
            .where(
                work_tables.schedule.c.module_id == module_id,
                work_tables.schedule.c.name == name,
            )
        ).scalar_one()
    )


# --- the gate -------------------------------------------------------------------------


@pytest.mark.parametrize("workspace_value", [None, False], ids=["no-row", "row-false"])
@pytest.mark.usefixtures("gate_probe")
def test_a_closed_gate_enqueues_nothing_and_still_advances_by_the_interval(
    cluster: ClusterSession, workspace: UUID, workspace_value: bool | None
) -> None:
    start = _start()
    with _engine(cluster, workspace).begin() as connection:
        if workspace_value is not None:
            _set_gate(connection, workspace_value)
        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=start
        )
        for tick in range(TICKS):
            now = start + tick * INTERVAL
            run_due_schedules(connection, workspace_id=workspace, now=now)
            row = _row(connection, GATE_PROBE_ID, GATED)
            assert row.next_run_at == now + INTERVAL
            assert row.last_run_at is None
        assert _jobs(connection, GATED_KIND) == 0


@pytest.mark.usefixtures("gate_probe")
def test_an_open_gate_enqueues_exactly_one_job_per_due_interval(
    cluster: ClusterSession, workspace: UUID
) -> None:
    start = _start()
    with _engine(cluster, workspace).begin() as connection:
        _set_gate(connection, True)
        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=start
        )
        for tick in range(TICKS):
            now = start + tick * INTERVAL
            run_due_schedules(connection, workspace_id=workspace, now=now)
            assert _jobs(connection, GATED_KIND) == tick + 1
            # Half an interval later the row is not due: nothing more is enqueued.
            run_due_schedules(
                connection, workspace_id=workspace, now=now + INTERVAL / 2
            )
            assert _jobs(connection, GATED_KIND) == tick + 1
            row = _row(connection, GATE_PROBE_ID, GATED)
            assert row.last_run_at == now
            assert row.next_run_at == now + INTERVAL
        job = connection.execute(
            select(work_tables.job).where(work_tables.job.c.kind == GATED_KIND).limit(1)
        ).one()
        assert job.input == {"workspace_id": str(workspace)}


@pytest.mark.usefixtures("gate_probe")
def test_turning_the_gate_on_resumes_on_the_next_due_tick(
    cluster: ClusterSession, workspace: UUID
) -> None:
    start = _start()
    with _engine(cluster, workspace).begin() as connection:
        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=start
        )
        run_due_schedules(connection, workspace_id=workspace, now=start)
        assert _jobs(connection, GATED_KIND) == 0
        _set_gate(connection, True)
        run_due_schedules(connection, workspace_id=workspace, now=start + INTERVAL)
        assert _jobs(connection, GATED_KIND) == 1


# --- existing behaviour is unchanged --------------------------------------------------


@pytest.mark.usefixtures("gate_probe")
def test_undeclared_and_fieldless_schedules_keep_one_day_ungated(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """The core's own retention row, a row no loaded module declares, and a declared
    schedule with neither field, all due on a tick where the probe's gate is closed."""
    now = _start()
    with _engine(cluster, workspace).begin() as connection:
        connection.execute(
            update(work_tables.schedule)
            .where(work_tables.schedule.c.job_kind == RETENTION_SWEEP)
            .values(next_run_at=now)
        )
        connection.execute(
            insert(work_tables.schedule).values(
                id=UUID("01a10000-0000-7000-8000-000000000001"),
                module_id="absent_probe",
                name="nightly",
                job_kind="absent_probe.nightly",
                cron="0 3 * * *",
                enabled=True,
                next_run_at=now,
                last_run_at=None,
            )
        )
        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=DAILY, enabled=True, due_at=now
        )
        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=now
        )
        run_due_schedules(connection, workspace_id=workspace, now=now)
        for kind in (RETENTION_SWEEP, "absent_probe.nightly", DAILY_KIND):
            assert _jobs(connection, kind) == 1, kind
            row = connection.execute(
                select(work_tables.schedule).where(
                    work_tables.schedule.c.job_kind == kind
                )
            ).one()
            assert row.last_run_at == now, kind
            assert row.next_run_at == now + timedelta(days=1), kind
        assert _jobs(connection, GATED_KIND) == 0


# --- ensure_module_schedule -----------------------------------------------------------


@pytest.mark.usefixtures("gate_probe")
def test_ensure_creates_the_declared_row_and_flips_it_without_touching_others(
    cluster: ClusterSession, workspace: UUID
) -> None:
    first = _start()
    second = first + timedelta(hours=5)
    third = first + timedelta(hours=9)
    with _engine(cluster, workspace).begin() as connection:
        assert _rows(connection, GATE_PROBE_ID, GATED) == 0
        others_before = connection.execute(
            select(work_tables.schedule).order_by(work_tables.schedule.c.id)
        ).all()

        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=first
        )
        row = _row(connection, GATE_PROBE_ID, GATED)
        assert row.enabled is True
        assert row.next_run_at == first
        assert row.job_kind == GATED_KIND
        assert row.cron == "0 */2 * * *"

        ensure_module_schedule(
            connection,
            module_id=GATE_PROBE_ID,
            name=GATED,
            enabled=False,
            due_at=second,
        )
        row = _row(connection, GATE_PROBE_ID, GATED)
        assert row.enabled is False
        assert row.next_run_at == second

        ensure_module_schedule(
            connection, module_id=GATE_PROBE_ID, name=GATED, enabled=True, due_at=third
        )
        row = _row(connection, GATE_PROBE_ID, GATED)
        assert row.enabled is True
        assert row.next_run_at == third
        assert _rows(connection, GATE_PROBE_ID, GATED) == 1

        others_after = connection.execute(
            select(work_tables.schedule)
            .where(work_tables.schedule.c.module_id != GATE_PROBE_ID)
            .order_by(work_tables.schedule.c.id)
        ).all()
        assert others_after == others_before


@pytest.mark.usefixtures("gate_probe")
def test_ensure_refuses_an_undeclared_schedule_and_writes_nothing(
    cluster: ClusterSession, workspace: UUID
) -> None:
    with _engine(cluster, workspace).begin() as connection:
        before = connection.execute(
            select(func.count()).select_from(work_tables.schedule)
        ).scalar_one()
        with pytest.raises(ScheduleUndeclared):
            ensure_module_schedule(
                connection,
                module_id=GATE_PROBE_ID,
                name="not_declared",
                enabled=True,
                due_at=_start(),
            )
        with pytest.raises(ScheduleUndeclared):
            ensure_module_schedule(
                connection,
                module_id="absent_probe",
                name=GATED,
                enabled=True,
                due_at=_start(),
            )
        after = connection.execute(
            select(func.count()).select_from(work_tables.schedule)
        ).scalar_one()
        assert after == before
