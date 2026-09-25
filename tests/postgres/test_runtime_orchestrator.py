"""C3 orchestrator: ``core.runtime.run`` dispatch, handshakes, and spawn fields."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.redaction import (
    PROBE_EMAIL,
    PROBE_ORGANIZATION,
    PROBE_TITLE,
    note_renderings,
    probe_body,
)
from harness.registry import (
    NOTE_WRITE,
    enable_harness_module,
    register_harness,
)
from rheo_contracts import (
    ActorKind,
    AdapterSpawn,
    AudienceKind,
    ContextItem,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    Isolation,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeRequest,
    UsageReporting,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.events.consumers import ConsumerRegistry
from rheo_core.operations import dispatch
from rheo_core.operations import records as operation_records
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.redaction import registry as redaction_registry
from rheo_core.redaction.masking import EMAIL_MASK, SECRET_MASK
from rheo_core.refs import uuid7
from rheo_core.runtime import (
    RUNTIME_RUN,
    AdapterRegistry,
    RuntimeJobPayload,
    make_run_runtime_job,
)
from rheo_core.storage import runtime_tables, work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_access_token
from rheo_core.storage.data_root import Purpose, run_dir_for, workspace_dir_for
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.tokens.issue import issue_runtime_token
from rheo_core.work.cancellation import CancellationToken, JobCancelled
from rheo_core.work.jobs import has_job_for_operation
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from sqlalchemy import Engine, func, select

pytestmark = pytest.mark.postgres

OWNER = "runtime-orchestrator-test"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _capabilities(**overrides: object) -> RuntimeCapabilities:
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
        self._cancelled = False

    def poll(self) -> RuntimeEvent | None:
        if self._events:
            return self._events.pop(0)
        return None

    def finished(self) -> bool:
        return not self._events

    def cancel(self) -> None:
        self._cancelled = True


class RecordingAdapter:
    def __init__(
        self,
        events: list[RuntimeEvent] | None = None,
        *,
        capabilities: RuntimeCapabilities | None = None,
        native_handle: str | None = "cli-session-1",
    ) -> None:
        self.start_calls: list[tuple[RuntimeRequest, AdapterSpawn]] = []
        self._events = events if events is not None else [FinalOutputEvent(text="ok")]
        self._capabilities = capabilities or _capabilities()
        self._native_handle = native_handle

    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> RuntimeHandle:
        self.start_calls.append((request, spawn))
        return _Handle(list(self._events), self._native_handle)


class NeverAnswerAdapter(RecordingAdapter):
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


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def _owner_context(
    cluster: ClusterSession,
    workspace_id: UUID,
    account_id: UUID,
    *,
    harness: bool = False,
) -> WorkspaceContext:
    if harness:
        row = cluster.registry_row(workspace_id)
        with UnitOfWork(
            cluster.backend.pools.engine_for(row.database_name), row.database_name
        ) as uow:
            enable_harness_module(uow.connection)
            uow.commit()
    ctx = context_for_harness(workspace_id, account_id, "owner")
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")


def _kinds(adapter: RecordingAdapter, clock: Callable[[], datetime]) -> JobKindRegistry:
    registry = AdapterRegistry()
    registry.register("claude_cli", adapter)
    kinds = JobKindRegistry()
    kinds.register(
        RUNTIME_RUN, RuntimeJobPayload, make_run_runtime_job(registry, clock)
    )
    return kinds


def _visit(
    cluster: ClusterSession,
    workspace_id: UUID,
    kinds: JobKindRegistry,
) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )


def _payload() -> dict[str, Any]:
    return {
        "task": "draft a reply",
        "purpose": "respond",
        "output": {"kind": "text"},
        "refs": [],
        "requirements": [],
        "permitted_tools": [],
    }


def _job_row(engine: Engine, operation_id: UUID) -> Any:
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


def _operation(engine: Engine, operation_id: UUID):
    with engine.connect() as connection:
        return operation_records.get(connection, operation_id=operation_id)


def test_runtime_dispatch_refuses_operator_runtime_actor_required(
    cluster: ClusterSession, workspace: UUID
) -> None:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    outcome = dispatch(ctx, RUNTIME_RUN, _payload())
    assert outcome.state == "runtime_actor_required"
    engine = workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        if outcome.operation_id is not None:
            assert not has_job_for_operation(
                connection, operation_id=outcome.operation_id
            )
        count = connection.execute(
            select(func.count()).select_from(work_tables.job)
        ).scalar_one()
    assert count == 0


def test_runtime_dispatch_fills_sole_allowed_runtime_model_and_deadline(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    outcome = dispatch(ctx, RUNTIME_RUN, _payload())
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    engine = workspace_engine(cluster, workspace)
    row = _job_row(engine, outcome.operation_id)
    payload = RuntimeJobPayload.model_validate(row["input"])
    assert payload.runtime_id == "claude_cli"
    assert payload.model_id == "sonnet"
    assert payload.deadline_seconds == 600
    assert payload.operation_id == outcome.operation_id
    assert row["operation_id"] == outcome.operation_id
    assert "text" not in payload.model_dump()["refs"]
    dumped = row["input"]
    assert "context_items" not in dumped
    assert dumped.get("refs") == []


def test_runtime_dispatch_refuses_unknown_runtime_before_enqueue(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = _owner_context(cluster, workspace, owner_account_id)
    payload = _payload()
    payload["runtime_id"] = "not_a_runtime"
    outcome = dispatch(ctx, RUNTIME_RUN, payload)
    assert outcome.state == "runtime_unknown"
    engine = workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        count = connection.execute(
            select(func.count()).select_from(work_tables.job)
        ).scalar_one()
    assert count == 0


def test_runtime_login_binding_handshake_does_not_start_or_write_seed(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_account_id", str(uuid7()))
    config_dir = workspace_dir_for(workspace, Purpose.SCRATCH) / "claude-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    seed = config_dir / "seed-marker"
    seed.write_text("untouched", encoding="utf-8")
    before_mtime = seed.stat().st_mtime
    before_text = seed.read_text(encoding="utf-8")

    adapter = RecordingAdapter()
    clock = datetime.now(UTC)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    outcome = dispatch(ctx, RUNTIME_RUN, _payload())
    assert outcome.state == "pending", outcome
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))

    engine = workspace_engine(cluster, workspace)
    operation = _operation(engine, outcome.operation_id)
    assert operation is not None
    assert operation.state == "failed"
    assert operation.error_code == "credential_not_owned"
    assert adapter.start_calls == []
    assert seed.read_text(encoding="utf-8") == before_text
    assert seed.stat().st_mtime == before_mtime
    job = _job_row(engine, outcome.operation_id)
    assert job["state"] in {"succeeded", "failed"}


def test_runtime_gate_before_adapter_start_uses_unmet_capability_name(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    adapter = RecordingAdapter(capabilities=_capabilities(structured_output=False))
    clock = datetime.now(UTC)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    payload = _payload()
    payload["requirements"] = ["structured_output"]
    outcome = dispatch(ctx, RUNTIME_RUN, payload)
    assert outcome.state == "pending", outcome
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    operation = _operation(workspace_engine(cluster, workspace), outcome.operation_id)
    assert operation is not None
    assert operation.state == "failed"
    assert operation.error_code == "structured_output"
    assert adapter.start_calls == []
    engine = workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        sessions = connection.execute(
            select(func.count()).select_from(runtime_tables.runtime_session)
        ).scalar_one()
    assert sessions == 0


def test_runtime_failure_handshake_uses_executable_unavailable_not_job_failed(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    adapter = RecordingAdapter(
        events=[
            FailureEvent(
                kind=FailureKind.EXECUTABLE_UNAVAILABLE, detail="missing binary"
            )
        ]
    )
    clock = datetime.now(UTC)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    outcome = dispatch(ctx, RUNTIME_RUN, _payload())
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    operation = _operation(workspace_engine(cluster, workspace), outcome.operation_id)
    assert operation is not None
    assert operation.state == "failed"
    assert operation.error_code == "executable_unavailable"
    assert operation.error_code != "job_failed"
    assert operation.terminal_check_kind is None
    job = _job_row(workspace_engine(cluster, workspace), outcome.operation_id)
    assert job["state"] == "succeeded"
    with workspace_engine(cluster, workspace).connect() as connection:
        sessions = connection.execute(
            select(func.count()).select_from(runtime_tables.runtime_session)
        ).scalar_one()
    assert sessions == 0


def test_runtime_job_operation_id_equals_minted_id_and_spawn_dirs(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    adapter = RecordingAdapter()
    clock = datetime.now(UTC)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    outcome = dispatch(ctx, RUNTIME_RUN, _payload())
    assert outcome.operation_id is not None
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    engine = workspace_engine(cluster, workspace)
    job = _job_row(engine, outcome.operation_id)
    assert job["operation_id"] == outcome.operation_id
    payload = RuntimeJobPayload.model_validate(job["input"])
    assert payload.operation_id == outcome.operation_id
    assert len(adapter.start_calls) == 1
    request, spawn = adapter.start_calls[0]
    assert request.context_items == []
    expected_work = run_dir_for(workspace, outcome.operation_id)
    expected_config = workspace_dir_for(workspace, Purpose.SCRATCH) / "claude-cli"
    assert spawn.work_dir == str(expected_work)
    assert spawn.config_dir == str(expected_config)
    assert spawn.native_handle is None
    assert spawn.run_token.startswith("rheo_runtime_")
    operation = _operation(engine, outcome.operation_id)
    assert operation is not None
    assert operation.state == "succeeded"
    assert operation.terminal_check_kind == operation_records.HANDLER_RETURNED
    with engine.connect() as connection:
        sessions = (
            connection.execute(select(runtime_tables.runtime_session)).mappings().all()
        )
        requests = (
            connection.execute(select(runtime_tables.runtime_request)).mappings().all()
        )
    assert len(sessions) == 1
    assert sessions[0]["native_handle"] == "cli-session-1"
    assert len(requests) == 1
    request_row = requests[0]
    blob = " ".join(str(value) for value in request_row.values())
    assert "secret://" not in blob
    assert "rheo_runtime_" not in blob
    assert not expected_work.exists()


def test_runtime_continuation_puts_cli_native_handle_on_spawn_not_uuid(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    session_id = uuid7()
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            runtime_tables.runtime_session.insert().values(
                id=session_id,
                runtime_id="claude_cli",
                credential_scope="api_key",
                actor_kind=ActorKind.ACCOUNT.value,
                actor_id=owner_account_id,
                audience_kind=AudienceKind.SESSION.value,
                audience_id=owner_account_id,
                native_handle="native-cli-handle",
                created_at=now,
                last_used_at=now,
                expires_at=now + timedelta(hours=72),
            )
        )
    adapter = RecordingAdapter(native_handle="rotated-handle")
    clock = now
    payload = _payload()
    payload["continuation"] = str(session_id)
    dispatched = dispatch(ctx, RUNTIME_RUN, payload)
    assert dispatched.state == "pending", dispatched
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    assert len(adapter.start_calls) == 1
    spawn = adapter.start_calls[0][1]
    assert spawn.native_handle == "native-cli-handle"
    assert spawn.native_handle != str(session_id)


def test_runtime_expired_continuation_does_not_resume(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    session_id = uuid7()
    engine = workspace_engine(cluster, workspace)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            runtime_tables.runtime_session.insert().values(
                id=session_id,
                runtime_id="claude_cli",
                credential_scope="api_key",
                actor_kind=ActorKind.ACCOUNT.value,
                actor_id=owner_account_id,
                audience_kind=AudienceKind.SESSION.value,
                audience_id=owner_account_id,
                native_handle="stale-handle",
                created_at=now - timedelta(hours=80),
                last_used_at=now - timedelta(hours=80),
                expires_at=now - timedelta(seconds=1),
            )
        )
    adapter = RecordingAdapter()
    payload = _payload()
    payload["continuation"] = str(session_id)
    dispatched = dispatch(ctx, RUNTIME_RUN, payload)
    assert dispatched.state == "pending", dispatched
    _visit(cluster, workspace, _kinds(adapter, lambda: now))
    spawn = adapter.start_calls[0][1]
    assert spawn.native_handle is None


def test_runtime_build_context_resolves_refs_in_the_job(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    ctx = _owner_context(cluster, workspace, owner_account_id, harness=True)
    written = dispatch(ctx, NOTE_WRITE, {"body": "note body for the model"})
    assert written.ok, written
    ref = written.result.ref  # type: ignore[union-attr]
    adapter = RecordingAdapter()
    clock = datetime.now(UTC)
    payload = _payload()
    payload["refs"] = [ref]
    outcome = dispatch(ctx, RUNTIME_RUN, payload)
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    request = adapter.start_calls[0][0]
    assert request.context_items
    assert all(isinstance(item, ContextItem) for item in request.context_items)
    assert any("note body for the model" in item.text for item in request.context_items)
    job = _job_row(workspace_engine(cluster, workspace), outcome.operation_id)
    dumped = job["input"]
    assert dumped["refs"] == [ref]
    assert "note body for the model" not in str(dumped)


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("respond", f"title: {PROBE_TITLE}\norganization: {PROBE_ORGANIZATION}"),
        ("share_with_referral", f"title: {PROBE_TITLE}"),
    ],
)
def test_runtime_build_context_redacts_a_tiered_record_in_the_job(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    expected: str,
) -> None:
    """Issue #130 through the real job: the run's purpose is the one its token was
    minted with, rebuilt onto the job's context, and the tiered note reaches the
    adapter as its allowed fields and nothing of its restricted email.

    The rendering is patched over the process-global table, which is the one the job
    handler's ``build_context`` reads, and put back when the test ends.
    """
    _api_key(monkeypatch)
    monkeypatch.setattr(redaction_registry, "RENDERINGS", note_renderings())
    ctx = _owner_context(cluster, workspace, owner_account_id, harness=True)
    written = dispatch(ctx, NOTE_WRITE, {"body": probe_body()})
    assert written.ok, written
    ref = written.result.ref  # type: ignore[union-attr]
    adapter = RecordingAdapter()
    clock = datetime.now(UTC)
    payload = _payload()
    payload["refs"] = [ref]
    payload["purpose"] = purpose
    outcome = dispatch(ctx, RUNTIME_RUN, payload)
    assert outcome.operation_id is not None, outcome
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    request = adapter.start_calls[0][0]
    assert [item.text for item in request.context_items] == [expected]
    assert PROBE_EMAIL not in request.model_dump_json()
    assert PROBE_EMAIL.split("@", 1)[0] not in request.model_dump_json()


def test_runtime_task_text_is_masked_before_the_adapter_sees_it(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task goes to the provider like a context item, so the same policy masks
    it: a secret reference always, a contact value while the allowance is off."""
    _api_key(monkeypatch)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    adapter = RecordingAdapter()
    clock = datetime.now(UTC)
    payload = _payload()
    payload["task"] = f"use secret://env/RHEO_KEY and write to {PROBE_EMAIL}"
    dispatch(ctx, RUNTIME_RUN, payload)
    _visit(cluster, workspace, _kinds(adapter, lambda: clock))
    request = adapter.start_calls[0][0]
    assert request.task == f"use {SECRET_MASK} and write to {EMAIL_MASK}"


def test_runtime_token_kind_and_issuer_constraint(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    token_id, value = issue_runtime_token(
        account_id=owner_account_id,
        workspace_id=workspace,
        purpose="respond",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        operations=[],
    )
    assert value.startswith("rheo_runtime_")
    with get_backend().control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    assert row is not None
    assert row.kind == "runtime"
    assert row.issued_from == "runtime"


def test_runtime_clock_driven_deadline_does_not_sleep(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    origin = datetime.now(UTC)
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        if calls["n"] > 8:
            return origin + timedelta(seconds=1200)
        return origin

    adapter = NeverAnswerAdapter()
    ctx = _owner_context(cluster, workspace, owner_account_id)
    payload = _payload()
    payload["deadline_seconds"] = 60
    outcome = dispatch(ctx, RUNTIME_RUN, payload)
    _visit(cluster, workspace, _kinds(adapter, clock))
    operation = _operation(workspace_engine(cluster, workspace), outcome.operation_id)
    assert operation is not None
    assert operation.state == "failed"
    assert operation.error_code == "deadline_exceeded"
    assert adapter.start_calls  # start happened, then the clock jumped


class _CancelRecordingHandle:
    """A run still going: never answers, never finishes, and records ``cancel``."""

    native_handle = None

    def __init__(self) -> None:
        self.polls = 0
        self.cancelled = False

    def poll(self) -> RuntimeEvent | None:
        self.polls += 1
        return None

    def finished(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True


class _CancelRecordingAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__(events=[], native_handle=None)
        self.handle = _CancelRecordingHandle()

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> RuntimeHandle:
        self.start_calls.append((request, spawn))
        return self.handle


def test_a_cancelled_runtime_job_kills_the_running_process(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#128. A job cancelled while its run is live leaves the poll loop by
    ``token.checkpoint()`` raising ``JobCancelled``. The handle must be cancelled on
    the way out, which kills the CLI's process group; before the fix only the token
    and working directory were cleaned up and the process ran on.

    The checkpoint is made to raise once the run has polled, which is the moment an
    operator's cancel would be observed; how ``cancel_requested`` reaches the token is
    the worker's own tested path and not this test's subject.
    """
    _api_key(monkeypatch)
    adapter = _CancelRecordingAdapter()
    original = CancellationToken.checkpoint

    def checkpoint(self: CancellationToken, now: datetime | None = None) -> None:
        if adapter.handle.polls:
            raise JobCancelled("cancelled mid-run")
        original(self, now)

    monkeypatch.setattr(CancellationToken, "checkpoint", checkpoint)
    ctx = _owner_context(cluster, workspace, owner_account_id)
    dispatch(ctx, RUNTIME_RUN, _payload())

    _visit(cluster, workspace, _kinds(adapter, lambda: datetime.now(UTC)))

    assert adapter.start_calls
    assert adapter.handle.polls >= 1
    assert adapter.handle.cancelled
