"""Issue #12's LRU half: a worker pass must not evict the engine it is about to use.

Seam: ``work.loop.run_one_pass`` over a real ``EnginePool``. No Postgres: the due-work
index, the visit and ``record_visit`` are replaced with an in-memory index that keeps
the real one's ordering (oldest ``due_at`` first, capped at the pass limit) and a visit
that only asks the pool for the workspace's engine. SQLAlchemy's ``create_engine`` is
lazy, so the pool builds engines and never connects.

The workload is the one the issue names: more continuously busy workspaces than
``storage.pool_cache_size`` (6 since #170). Each visit stops at the job cap with work
left, so it reports itself due again straight away and goes to the back of the index.
Under that rotation the pass that follows opens with the workspaces the previous pass
could not reach, and a pass that visits them in index order evicts, at every miss, the
engine the same pass needs next: every visit misses. The fix visits cached engines
first, so a pass pays one engine per workspace that genuinely was not cached, which
for ``cache_size < N <= 2 * cache_size`` is ``N - cache_size`` per pass.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from rheo_core.events import ConsumerRegistry
from rheo_core.storage.pools import EnginePool
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.storage.provisioning import database_name_for
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work import loop
from rheo_core.work.kinds import JobKindRegistry
from sqlalchemy.engine import make_url

CACHE_SIZE = 6
"""``storage.pool_cache_size``'s shipped default since #170."""

PASSES = 12
WARM_UP = 2
"""Passes before the cache holds a steady working set; the first one misses on every
workspace because nothing is cached yet."""


class _Clock:
    """Strictly increasing instants anchored to the real clock, one second apart."""

    def __init__(self) -> None:
        self._at = datetime.now(UTC)

    def __call__(self) -> datetime:
        self._at += timedelta(seconds=1)
        return self._at


class _Index:
    """The due-work index's ordering and nothing else: oldest ``due_at`` first."""

    def __init__(self, workspaces: list[UUID], clock: _Clock) -> None:
        self.due_at = {workspace: clock() for workspace in workspaces}

    def read(
        self, _conn: object, *, now: datetime, limit: int
    ) -> tuple[DueWorkspace, ...]:
        due = sorted(
            (at, workspace) for workspace, at in self.due_at.items() if at <= now
        )
        return tuple(
            DueWorkspace(workspace_id=workspace, observed_due_at=at)
            for at, workspace in due[:limit]
        )

    def record(
        self,
        _conn: object,
        workspace_id: UUID,
        *,
        observed_due_at: datetime | None,
        next_due_at: datetime | None,
        reconcile_seconds: int,
    ) -> bool:
        assert next_due_at is not None
        self.due_at[workspace_id] = next_due_at
        return True


class _Control:
    @contextmanager
    def begin(self) -> Iterator[None]:
        yield None


class _Backend:
    def __init__(self) -> None:
        self.pools = EnginePool(
            make_url("postgresql+psycopg://rheo:secret@localhost:5432/postgres"),
            cache_size=CACHE_SIZE,
            pool_size=5,
            idle_close_seconds=3600.0,
            reserved_connections=0,
        )
        self.control_engine = _Control()


def _misses_per_pass(monkeypatch: pytest.MonkeyPatch, workspaces: int) -> list[int]:
    clock = _Clock()
    index = _Index([uuid4() for _ in range(workspaces)], clock)
    backend = _Backend()
    misses: list[int] = []

    def visit(workspace: DueWorkspace, **_: object) -> loop.VisitResult:
        name = database_name_for(workspace.workspace_id)
        if name not in backend.pools.cached():
            misses[-1] += 1
        backend.pools.engine_for(name)
        # Stopped at the job cap with work left: due again now, to the back.
        return loop.VisitResult(
            next_due_at=clock(), jobs_acquired=1, deliveries_acquired=0
        )

    monkeypatch.setattr(loop, "workspaces_with_due_work", index.read)
    monkeypatch.setattr(loop, "record_visit", index.record)
    monkeypatch.setattr(loop, "visit_workspace", visit)
    try:
        for _ in range(PASSES):
            misses.append(0)
            result = loop.run_one_pass(
                kinds=JobKindRegistry(),
                consumers=ConsumerRegistry(),
                backend=cast(PostgresBackend, backend),
                owner="worker-cache-order-test",
                now=clock(),
                clock=clock,
                jitter=random.Random(0),
                reconcile_seconds=900,
            )
            assert result.workspaces_visited == min(workspaces, CACHE_SIZE)
    finally:
        backend.pools.dispose_all()
    return misses


@pytest.mark.parametrize("workspaces", [CACHE_SIZE + 1, CACHE_SIZE + 2, CACHE_SIZE * 2])
def test_a_pass_over_more_busy_workspaces_than_the_cache_never_evicts_its_next_visit(
    monkeypatch: pytest.MonkeyPatch, workspaces: int
) -> None:
    """Seven busy workspaces against a cache of six cost one new engine a pass, not six.

    Before #12's fix every steady-state pass missed on all ``CACHE_SIZE`` visits. The
    floor is ``workspaces - CACHE_SIZE``: a pass of ``CACHE_SIZE`` visits needs that
    many workspaces the previous pass did not reach, and no order can make those hits.
    """
    misses = _misses_per_pass(monkeypatch, workspaces)

    assert misses[0] == CACHE_SIZE, "a cold cache misses on every visit"
    assert misses[WARM_UP:] == [workspaces - CACHE_SIZE] * (PASSES - WARM_UP), misses


def test_a_working_set_that_fits_the_cache_misses_only_while_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: at or under the cap there is nothing to evict, in any order."""
    misses = _misses_per_pass(monkeypatch, CACHE_SIZE)

    assert misses == [CACHE_SIZE] + [0] * (PASSES - 1), misses


def test_cache_hits_first_keeps_the_index_order_within_hits_and_within_misses() -> None:
    """The reorder moves no workspace in or out of the pass and is stable in each half,
    so the index's oldest-first order still decides who goes first among equals."""
    ids = [uuid4() for _ in range(5)]
    due = tuple(DueWorkspace(workspace_id=i, observed_due_at=None) for i in ids)
    cached = (database_name_for(ids[3]), database_name_for(ids[1]), "ws_unrelated")

    ordered = loop.cache_hits_first(due, cached=cached)

    assert [w.workspace_id for w in ordered] == [ids[1], ids[3], ids[0], ids[2], ids[4]]
