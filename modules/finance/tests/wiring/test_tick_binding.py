"""The tick binds reminders to Current in process, and releases them (L8, RS-6c).

``_run`` binds an in-process Current client over the job's core context and the
worker's consumer registry before the tick runs, and nothing is bound afterwards,
also when the tick raises; ``finance_tick_job`` hands ``_run`` the registry the worker
put on its unit of work. No database: core's factories are patched.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rheo_finance import db
from rheo_finance.jobs import tick as tick_module
from rheo_finance.sinks import current
from rheo_finance.wiring import core as wiring_core
from rheo_finance.wiring import model as wiring_model
from rheo_finance.wiring import tick_job
from rheo_finance.wiring.current import CoreCurrent

CORE_CONTEXT = SimpleNamespace(workspace_id=uuid.UUID(int=3))
REGISTRY = object()


class Token:
    def checkpoint(self) -> None:
        pass


def _patch_core(monkeypatch: pytest.MonkeyPatch, tick: object) -> None:
    layout = db.layout_under(Path("/synthetic/uploads"))
    monkeypatch.setattr(tick_job, "context_for_scheduled_job", lambda _id: CORE_CONTEXT)
    monkeypatch.setattr(wiring_core, "layout_for", lambda _core: layout)
    monkeypatch.setattr(db, "check_layout", lambda given: given)
    monkeypatch.setattr(wiring_core, "context_over_core", lambda *_a, **_k: object())
    monkeypatch.setattr(wiring_model, "runtime_model_client", lambda *_a, **_k: None)
    monkeypatch.setattr(tick_module, "tick", tick)


def test_the_tick_runs_with_reminders_bound_and_nothing_bound_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[current.InProcessCurrent | None] = []

    def tick(_ctx: object, **_kwargs: object) -> None:
        seen.append(current.in_process())

    _patch_core(monkeypatch, tick)
    assert current.in_process() is None
    tick_job._run(uuid.UUID(int=3), Token(), REGISTRY)  # type: ignore[arg-type]
    assert len(seen) == 1
    binding = seen[0]
    assert isinstance(binding, CoreCurrent)
    # The job's own core context and the worker's registry.
    assert id(binding._core_ctx) == id(CORE_CONTEXT)
    assert binding._consumers is REGISTRY
    assert current.in_process() is None
    assert db.bound_layout() is None


def test_nothing_stays_bound_when_the_tick_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tick(_ctx: object, **_kwargs: object) -> None:
        assert current.in_process() is not None
        raise ValueError("synthetic failure")

    _patch_core(monkeypatch, tick)
    with pytest.raises(ValueError):
        tick_job._run(uuid.UUID(int=3), Token(), REGISTRY)  # type: ignore[arg-type]
    assert current.in_process() is None
    assert db.bound_layout() is None


def test_the_job_hands_the_worker_registry_to_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handed: list[object] = []
    monkeypatch.setattr(
        tick_job, "_run", lambda _workspace, _token, consumers: handed.append(consumers)
    )
    view = SimpleNamespace(consumers=REGISTRY)
    payload = tick_job.TickPayload(workspace_id=uuid.UUID(int=3))
    tick_job.finance_tick_job(view, payload, Token())  # type: ignore[arg-type]
    assert handed == [REGISTRY]
