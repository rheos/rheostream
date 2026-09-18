"""Injectable runtime doubles and helpers for phase-one matrix rows 15–17.

Matrix demonstrators drive ``core.runtime.run`` through ``visit_workspace`` with a
process-local ``AdapterRegistry`` of recording doubles. They never construct the
worker production registry and never invoke a host-PATH ``claude`` binary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from rheo_contracts import (
    AdapterSpawn,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    Isolation,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    ToolCallEvent,
    UsageReporting,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.operations import records as operation_records
from rheo_core.runtime import (
    RUNTIME_RUN,
    AdapterRegistry,
    RuntimeJobPayload,
    make_run_runtime_job,
)
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import Engine, select

OWNER = "runtime-matrix-test"

SECRET_VALUE = "sk-ant-c5-matrix-not-a-real-key-7f3a"
SECRET_REF = "secret://env/RHEO_ANTHROPIC_API_KEY"
TOOL_CALL_DIGEST = "e3b0c44298fc1c149afbf4c8996fb924"


def adapter_capabilities(**overrides: object) -> RuntimeCapabilities:
    values: dict[str, object] = {
        "tool_calling": True,
        "structured_output": True,
        "streaming": True,
        "continuation": True,
        "cancellation": True,
        "usage_reporting": UsageReporting.EXACT,
        "isolation": Isolation.ADVISORY,
    }
    values.update(overrides)
    return RuntimeCapabilities.model_validate(values)


class _Handle:
    native_handle: str | None

    def __init__(self, events: list[RuntimeEvent], native_handle: str | None) -> None:
        self._events = list(events)
        self.native_handle = native_handle

    def poll(self) -> RuntimeEvent | None:
        if self._events:
            return self._events.pop(0)
        return None

    def finished(self) -> bool:
        return not self._events

    def cancel(self) -> None:
        return None


class RecordingAdapter:
    """Records ``start`` and plays a prepared event stream. No subprocess."""

    def __init__(
        self,
        events: list[RuntimeEvent] | None = None,
        *,
        capabilities: RuntimeCapabilities | None = None,
        native_handle: str | None = "cli-session-matrix",
    ) -> None:
        self.start_calls: list[tuple[RuntimeRequest, AdapterSpawn]] = []
        self._events = events if events is not None else [FinalOutputEvent(text="ok")]
        self._capabilities = capabilities or adapter_capabilities()
        self._native_handle = native_handle

    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> RuntimeHandle:
        self.start_calls.append((request, spawn))
        return _Handle(list(self._events), self._native_handle)


class NeverAnswerAdapter(RecordingAdapter):
    """``finished()`` stays false; ``poll()`` returns ``None``; no sleep."""

    def __init__(self) -> None:
        super().__init__(events=[], native_handle=None)

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> RuntimeHandle:
        self.start_calls.append((request, spawn))
        return _NeverHandle()


class _NeverHandle:
    native_handle = None

    def poll(self) -> RuntimeEvent | None:
        return None

    def finished(self) -> bool:
        return False

    def cancel(self) -> None:
        return None


def failure_adapter(kind: FailureKind, detail: str) -> RecordingAdapter:
    return RecordingAdapter(events=[FailureEvent(kind=kind, detail=detail)])


def credential_recording_adapter() -> RecordingAdapter:
    return RecordingAdapter(
        events=[
            ToolCallEvent(tool="core.note.get", arguments_digest=TOOL_CALL_DIGEST),
            FinalOutputEvent(text="ok"),
        ]
    )


def workspace_engine(cluster: Any, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def owner_context(
    cluster: Any,
    workspace_id: UUID,
    account_id: UUID,
    *,
    role: str = "owner",
) -> WorkspaceContext:
    ctx = context_for_harness(workspace_id, account_id, role)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def enable_harness(cluster: Any, workspace_id: UUID) -> None:
    from harness.registry import enable_harness_module

    row = cluster.registry_row(workspace_id)
    with UnitOfWork(
        cluster.backend.pools.engine_for(row.database_name), row.database_name
    ) as uow:
        enable_harness_module(uow.connection)
        uow.commit()


def kinds(adapter: RecordingAdapter, clock: Callable[[], datetime]) -> JobKindRegistry:
    registry = AdapterRegistry()
    registry.register("claude_cli", adapter)
    registered = JobKindRegistry()
    registered.register(
        RUNTIME_RUN, RuntimeJobPayload, make_run_runtime_job(registry, clock)
    )
    return registered


def visit(
    cluster: Any,
    workspace_id: UUID,
    job_kinds: JobKindRegistry,
) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=job_kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )


def payload(**overrides: object) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task": "draft a reply",
        "purpose": "respond",
        "output": {"kind": "text"},
        "refs": [],
        "requirements": [],
        "permitted_tools": [],
    }
    body.update(overrides)
    return body


def job_row(engine: Engine, operation_id: UUID) -> Any:
    with engine.connect() as connection:
        return (
            connection.execute(
                select(work_tables.job).where(
                    work_tables.job.c.operation_id == operation_id
                )
            )
            .mappings()
            .one()
        )


def operation_row(engine: Engine, operation_id: UUID):
    with engine.connect() as connection:
        return operation_records.get(connection, operation_id=operation_id)


def config_dir(workspace_id: UUID):
    return workspace_dir_for(workspace_id, Purpose.SCRATCH) / "claude-cli"


def frozen_clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def jumping_clock(origin: datetime, *, after: int = 8, jump_seconds: int = 1200):
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        if calls["n"] > after:
            return origin + timedelta(seconds=jump_seconds)
        return origin

    return clock


def stringify(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, Mapping):
        return " ".join(stringify(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(stringify(item) for item in value)
    return str(value)


def leak_in(value: object) -> str | None:
    blob = stringify(value)
    if SECRET_VALUE in blob:
        return "secret value"
    if SECRET_REF in blob:
        return "secret reference"
    return None
