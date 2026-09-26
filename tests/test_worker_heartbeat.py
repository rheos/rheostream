"""The worker's liveness signal (#156): ``worker_loop`` beats once per iteration,
failed passes included, and ``python -m rheo_app_worker.heartbeat`` reads the beat.

No Postgres: ``run_one_pass`` is replaced, because what is under test is the loop's
own control flow around it, not a pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from rheo_app_worker import heartbeat
from rheo_core.events import ConsumerRegistry
from rheo_core.work import loop
from rheo_core.work.kinds import JobKindRegistry


def _run_loop(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[str], beat: object
) -> list[str]:
    """Drive ``worker_loop`` through ``outcomes`` ("idle", "work" or "fail"),
    recording each beat and each pass into one ordered trace."""
    trace: list[str] = []
    pending = iter(outcomes)
    monkeypatch.setattr(loop, "IDLE_INTERVAL_SECONDS", 0.0)

    def fake_pass(**_: object) -> loop.PassResult:
        outcome = next(pending)
        trace.append(f"pass:{outcome}")
        if outcome == "fail":
            raise RuntimeError("the database is not there yet")
        found = 1 if outcome == "work" else 0
        return loop.PassResult(
            workspaces_visited=found, jobs_acquired=found, deliveries_acquired=0
        )

    monkeypatch.setattr(loop, "run_one_pass", fake_pass)

    def record_beat() -> None:
        trace.append("beat")
        if callable(beat):
            beat()

    loop.worker_loop(
        kinds=JobKindRegistry(),
        consumers=ConsumerRegistry(),
        backend=object(),  # type: ignore[arg-type]
        stop=threading.Event(),
        max_passes=len(outcomes),
        heartbeat=record_beat,
    )
    return trace


def test_the_loop_beats_before_every_pass_including_failed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = _run_loop(monkeypatch, ["fail", "fail", "idle", "work"], None)
    assert trace == [
        "beat",
        "pass:fail",
        "beat",
        "pass:fail",
        "beat",
        "pass:idle",
        "beat",
        "pass:work",
    ]


def test_the_loop_runs_without_a_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "IDLE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        loop,
        "run_one_pass",
        lambda **_: loop.PassResult(
            workspaces_visited=0, jobs_acquired=0, deliveries_acquired=0
        ),
    )
    loop.worker_loop(
        kinds=JobKindRegistry(),
        consumers=ConsumerRegistry(),
        backend=object(),  # type: ignore[arg-type]
        stop=threading.Event(),
        max_passes=2,
    )


def test_the_env_heartbeat_makes_the_probe_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "beat"
    env = {heartbeat.HEARTBEAT_FILE_VARIABLE: str(path)}
    beat = heartbeat.heartbeat_from_env(env)
    assert beat is not None
    assert heartbeat.main(env) == 1  # no beat yet
    _run_loop(monkeypatch, ["idle"], beat)
    assert path.is_file()
    assert heartbeat.main(env) == 0


def test_the_heartbeat_is_off_unless_configured() -> None:
    assert heartbeat.heartbeat_from_env({}) is None
    blank = {heartbeat.HEARTBEAT_FILE_VARIABLE: " "}
    assert heartbeat.heartbeat_from_env(blank) is None
    assert heartbeat.main({}) == 1


def test_a_stale_heartbeat_fails_the_probe(tmp_path: Path) -> None:
    path = tmp_path / "beat"
    heartbeat.touch(path)
    now = time.time()
    assert heartbeat.is_fresh(path, now=now)
    stale = now - heartbeat.MAX_AGE_SECONDS - 1
    os.utime(path, (stale, stale))
    assert not heartbeat.is_fresh(path, now=now)
    assert heartbeat.main({heartbeat.HEARTBEAT_FILE_VARIABLE: str(path)}) == 1


def test_touch_never_raises(tmp_path: Path) -> None:
    heartbeat.touch(tmp_path / "no-such-dir" / "beat")


def test_the_probe_runs_as_the_health_check_runs_it(tmp_path: Path) -> None:
    """``python -m rheo_app_worker.heartbeat``, the compose health check's command,
    in a fresh interpreter: exit 0 on a fresh beat, 1 on a missing one."""
    path = tmp_path / "beat"

    def probe() -> int:
        return subprocess.run(
            [sys.executable, "-m", "rheo_app_worker.heartbeat"],
            env={**os.environ, heartbeat.HEARTBEAT_FILE_VARIABLE: str(path)},
            capture_output=True,
            timeout=30,
            check=False,
        ).returncode

    assert probe() == 1
    heartbeat.touch(path)
    assert probe() == 0
