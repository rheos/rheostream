"""The ``claude_cli`` extraction provider's test doubles, and the no-process guard.

Shared by ``tests/test_extraction_provider.py`` and
``tests/postgres/test_automatic_memory_claude_cli.py``. Nothing here runs a model or a
process: :class:`ScriptedAdapter` replays events a test wrote, and
:func:`forbid_real_processes` makes any attempt to start a process fail loudly.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
import rheo_runtimes.claude_cli as claude_cli_module
from rheo_contracts import (
    AdapterSpawn,
    Isolation,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeRequest,
    UsageReporting,
)

NO_REAL_PROCESS: Final = "no real process in tests"
EXECUTABLE_ENV: Final = "RHEO__runtime__claude_cli__executable"


@dataclass
class PopenGuard:
    """Every attempted ``Popen`` call, each of which raised."""

    calls: list[object] = field(default_factory=list)


def forbid_real_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> PopenGuard:
    """Make ``subprocess.Popen`` raise, and point the CLI executable at nothing.

    ``rheo_runtimes.claude_cli`` reaches ``Popen`` through its ``subprocess`` module
    attribute, which is this same module object, so one patch covers both; the
    assertion after it says so rather than assuming it.
    """
    guard = PopenGuard()

    def _refuse(*args: object, **kwargs: object) -> object:
        guard.calls.append(args)
        raise AssertionError(NO_REAL_PROCESS)

    monkeypatch.setattr(subprocess, "Popen", _refuse)
    assert claude_cli_module.subprocess.Popen is _refuse
    monkeypatch.setenv(EXECUTABLE_ENV, str(tmp_path / "no-such-claude"))
    return guard


@dataclass
class StartRecord:
    """What one ``start`` call was handed, and the directory state at that moment."""

    request: RuntimeRequest
    spawn: AdapterSpawn
    work_dir_existed: bool
    config_dir_existed: bool


class ScriptedHandle:
    """Replays ``events`` in order, then reads as finished (``ends``) or runs on."""

    def __init__(self, events: Sequence[RuntimeEvent], *, ends: bool) -> None:
        self._events = list(events)
        self._ends = ends
        self.native_handle: str | None = None
        self.cancelled = False

    def poll(self) -> RuntimeEvent | None:
        if self._events:
            return self._events.pop(0)
        return None

    def finished(self) -> bool:
        return self.cancelled or (self._ends and not self._events)

    def cancel(self) -> None:
        self.cancelled = True


class ScriptedAdapter:
    """A ``RuntimeAdapter`` that starts nothing: each ``start`` records what it was
    handed and returns a handle replaying the scripted events."""

    def __init__(
        self, events: Sequence[RuntimeEvent] = (), *, ends: bool = True
    ) -> None:
        self.events = list(events)
        self.ends = ends
        self.starts: list[StartRecord] = []
        self.handles: list[ScriptedHandle] = []

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            tool_calling=True,
            structured_output=True,
            streaming=True,
            continuation=False,
            cancellation=True,
            usage_reporting=UsageReporting.NONE,
            isolation=Isolation.ADVISORY,
        )

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> ScriptedHandle:
        self.starts.append(
            StartRecord(
                request=request,
                spawn=spawn,
                work_dir_existed=Path(spawn.work_dir).is_dir(),
                config_dir_existed=Path(spawn.config_dir).exists(),
            )
        )
        handle = ScriptedHandle(self.events, ends=self.ends)
        self.handles.append(handle)
        return handle
