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


def test_the_run_hands_the_checkpoint_to_the_context_and_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # L1 (#546): the context's progress beat is the token's checkpoint, the same
    # bound method the tick and the model client get.
    built: list[dict[str, object]] = []
    ticked: list[dict[str, object]] = []
    heartbeats: list[object] = []

    def context(*_args: object, **kwargs: object) -> object:
        built.append(kwargs)
        return object()

    def tick(_ctx: object, **kwargs: object) -> None:
        ticked.append(kwargs)

    def model_client(_core: object, *, heartbeat: object) -> None:
        heartbeats.append(heartbeat)

    _patch_core(monkeypatch, tick)
    monkeypatch.setattr(wiring_core, "context_over_core", context)
    monkeypatch.setattr(wiring_model, "runtime_model_client", model_client)
    token = Token()
    tick_job._run(uuid.UUID(int=3), token, REGISTRY)  # type: ignore[arg-type]
    assert len(built) == 1
    assert len(ticked) == 1
    progress = built[0]["progress"]
    assert progress == token.checkpoint
    assert ticked[0]["checkpoint"] is progress
    assert heartbeats == [progress]
    assert heartbeats[0] is progress


def test_a_context_built_without_progress_beats_a_no_op() -> None:
    import datetime as dt

    from rheo_finance.clock import FakeClock
    from rheo_finance.context import FinanceContext, FinancePaths, no_progress
    from rheo_finance.contracts import Role
    from rheo_finance.settings import parse_settings

    ctx = FinanceContext(
        settings=parse_settings(""),
        clock=FakeClock(dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)),
        paths=FinancePaths(data=Path("/synthetic/data")),
        role=Role.SERVICE,
        snapshot_provider=lambda _uow, _ref: None,
    )
    assert ctx.progress is no_progress
    ctx.progress()  # beats nothing, raises nothing


def test_context_over_core_keeps_the_progress_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_finance import operations
    from rheo_finance.context import no_progress
    from rheo_finance.contracts import Role
    from rheo_finance.settings import parse_settings

    monkeypatch.setattr(db, "data_dir", lambda: Path("/synthetic/data"))
    monkeypatch.setattr(wiring_core, "load_settings", lambda _path: parse_settings(""))
    monkeypatch.setattr(
        operations, "with_core_sessions", lambda ctx, *, open_session: ctx
    )
    beats: list[None] = []

    def beat() -> None:
        beats.append(None)

    given = wiring_core.context_over_core(
        CORE_CONTEXT,  # type: ignore[arg-type]
        Role.SERVICE,
        model_client=None,
        progress=beat,
    )
    assert given.progress is beat
    plain = wiring_core.context_over_core(
        CORE_CONTEXT,  # type: ignore[arg-type]
        Role.SERVICE,
        model_client=None,
    )
    assert plain.progress is no_progress
    assert beats == []


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
