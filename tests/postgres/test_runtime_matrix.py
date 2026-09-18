"""C5 matrix demonstrators for criteria 15–17 (AC 1–8, 10, 12).

Each case drives the full ``core.runtime.run`` job path with an injectable recording
double. None of these tests invoke a live ``claude`` binary.
"""

from __future__ import annotations

import inspect
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import add_member, register_harness
from harness.runtime_matrix import (
    SECRET_REF,
    SECRET_VALUE,
    NeverAnswerAdapter,
    RecordingAdapter,
    adapter_capabilities,
    config_dir,
    credential_recording_adapter,
    enable_harness,
    failure_adapter,
    frozen_clock,
    job_row,
    jumping_clock,
    kinds,
    leak_in,
    operation_row,
    owner_context,
    payload,
    visit,
    workspace_engine,
)
from rheo_contracts import FailureKind, Isolation, Role, UnmetCapability
from rheo_core.audit import AUDIT_LIST
from rheo_core.operations import dispatch
from rheo_core.operations import records as operation_records
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.runtime import RUNTIME_RUN
from rheo_core.runtime import operations as runtime_ops
from rheo_core.secrets.value import SecretValue
from rheo_core.storage import runtime_tables
from sqlalchemy import select

pytestmark = pytest.mark.postgres

_WALL_SECONDS = 5.0
_DOMAIN_PATHS = (
    "/rheo_core/operations/",
    "/rheo_core/runtime/",
    "/rheo_core/audit/",
    "/rheo_core/identity/",
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_ref", SECRET_REF)
    monkeypatch.setenv("RHEO_ANTHROPIC_API_KEY", SECRET_VALUE)


def _dispatch_and_visit(
    cluster: ClusterSession,
    workspace: UUID,
    account_id: UUID,
    adapter: RecordingAdapter,
    clock,
    *,
    body: dict | None = None,
    role: str = "owner",
):
    ctx = owner_context(cluster, workspace, account_id, role=role)
    outcome = dispatch(ctx, RUNTIME_RUN, payload() if body is None else body)
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    started = time.perf_counter()
    visit(cluster, workspace, kinds(adapter, clock))
    elapsed = time.perf_counter() - started
    engine = workspace_engine(cluster, workspace)
    return ctx, outcome, engine, elapsed


def _assert_named_handshake(
    engine,
    operation_id: UUID,
    adapter: RecordingAdapter,
    *,
    error_code: str,
    start_count: int,
    elapsed: float,
) -> None:
    operation = operation_row(engine, operation_id)
    assert operation is not None
    assert operation.state == "failed"
    assert operation.error_code == error_code
    assert operation.error_code != "job_failed"
    assert operation.error_code != "runtime_error"
    assert operation.error_code != "unmet_capability"
    assert operation.terminal_check_kind != operation_records.HANDLER_RETURNED
    assert len(adapter.start_calls) == start_count
    job = job_row(engine, operation_id)
    assert job["state"] == "succeeded"
    assert elapsed < _WALL_SECONDS


def _table_blobs(engine) -> list[object]:
    blobs: list[object] = []
    with engine.connect() as connection:
        for table in (
            runtime_tables.runtime_request,
            runtime_tables.runtime_request_context,
            runtime_tables.runtime_transcript,
            runtime_tables.runtime_session,
        ):
            blobs.extend(connection.execute(select(table)).mappings().all())
    return blobs


def _assert_surfaces_clean(
    *,
    adapter: RecordingAdapter,
    engine,
    ctx,
    operation_id: UUID,
    expose_frames: list[str],
) -> None:
    operation = operation_row(engine, operation_id)
    job = job_row(engine, operation_id)
    listed = dispatch(ctx, AUDIT_LIST, {"limit": 500})
    assert listed.ok, listed
    records = listed.result.records  # type: ignore[union-attr]
    surfaces: list[object] = [operation, job["input"], records, *_table_blobs(engine)]
    if adapter.start_calls:
        request, spawn = adapter.start_calls[0]
        surfaces.extend(
            [request.model_dump(mode="json"), spawn.model_dump(mode="json")]
        )
        surfaces.extend(request.context_items)
    leaked = leak_in(surfaces)
    assert leaked is None, leaked
    for frame in expose_frames:
        assert not any(path in frame for path in _DOMAIN_PATHS), frame


def _install_expose_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    frames: list[str] = []
    original = SecretValue.expose

    def probed(self: SecretValue) -> bytes:
        frames.extend(frame.filename for frame in inspect.stack() if frame.filename)
        return original(self)

    monkeypatch.setattr(SecretValue, "expose", probed)
    return frames


@pytest.mark.parametrize(
    ("requirement", "overrides", "error_code"),
    [
        ("structured_output", {"structured_output": False}, "structured_output"),
        ("streaming", {"streaming": False}, "streaming"),
        ("continuation", {"continuation": False}, "continuation"),
        (
            "isolation=enforced",
            {"isolation": Isolation.ADVISORY},
            "isolation=enforced",
        ),
    ],
    ids=[
        "structured_output",
        "streaming",
        "continuation",
        "isolation_enforced",
    ],
)
def test_capability_gate_refuses_named_unmet_capability_before_start(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
    overrides: dict[str, object],
    error_code: str,
) -> None:
    _api_key(monkeypatch)
    adapter = RecordingAdapter(capabilities=adapter_capabilities(**overrides))
    now = datetime.now(UTC)
    body = payload(requirements=[requirement])
    _ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster, workspace, owner_account_id, adapter, frozen_clock(now), body=body
    )
    _assert_named_handshake(
        engine,
        outcome.operation_id,
        adapter,
        error_code=error_code,
        start_count=0,
        elapsed=elapsed,
    )
    operation = operation_row(engine, outcome.operation_id)
    assert operation is not None
    assert operation.error_code == UnmetCapability(error_code).name


def _named_failure_case(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    kind: FailureKind,
) -> None:
    _api_key(monkeypatch)
    adapter = failure_adapter(kind, f"{kind.value} via double")
    now = datetime.now(UTC)
    _ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster, workspace, owner_account_id, adapter, frozen_clock(now)
    )
    _assert_named_handshake(
        engine,
        outcome.operation_id,
        adapter,
        error_code=kind.value,
        start_count=1,
        elapsed=elapsed,
    )


def test_executable_unavailable_fails_named_kind_within_deadline(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _named_failure_case(
        cluster,
        workspace,
        owner_account_id,
        monkeypatch,
        FailureKind.EXECUTABLE_UNAVAILABLE,
    )


def test_credential_invalid_fails_named_kind_within_deadline(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _named_failure_case(
        cluster,
        workspace,
        owner_account_id,
        monkeypatch,
        FailureKind.CREDENTIAL_INVALID,
    )


def test_tool_denied_fails_named_kind_within_deadline(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _named_failure_case(
        cluster, workspace, owner_account_id, monkeypatch, FailureKind.TOOL_DENIED
    )


def test_stream_truncated_fails_named_kind_within_deadline(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    adapter = RecordingAdapter(events=[], native_handle=None)
    now = datetime.now(UTC)
    _ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster, workspace, owner_account_id, adapter, frozen_clock(now)
    )
    _assert_named_handshake(
        engine,
        outcome.operation_id,
        adapter,
        error_code="stream_truncated",
        start_count=1,
        elapsed=elapsed,
    )


def test_never_answer_fails_deadline_exceeded_without_hang(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    origin = datetime.now(UTC)
    adapter = NeverAnswerAdapter()
    body = payload(deadline_seconds=60)
    _ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster,
        workspace,
        owner_account_id,
        adapter,
        jumping_clock(origin),
        body=body,
    )
    _assert_named_handshake(
        engine,
        outcome.operation_id,
        adapter,
        error_code="deadline_exceeded",
        start_count=1,
        elapsed=elapsed,
    )


def test_login_from_other_member_is_credential_not_owned_without_spawn(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    monkeypatch.setenv(
        "RHEO__runtime__claude_cli__credential_account_id", str(owner_account_id)
    )
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="other-runtime-member"
    )
    path = config_dir(workspace)
    path.mkdir(parents=True, exist_ok=True)
    seed = path / "seed-marker"
    seed.write_text("untouched-login-seed", encoding="utf-8")
    before_mtime = seed.stat().st_mtime
    before_text = seed.read_text(encoding="utf-8")

    adapter = RecordingAdapter()
    now = datetime.now(UTC)
    _ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster,
        workspace,
        member_id,
        adapter,
        frozen_clock(now),
        role="member",
    )
    _assert_named_handshake(
        engine,
        outcome.operation_id,
        adapter,
        error_code="credential_not_owned",
        start_count=0,
        elapsed=elapsed,
    )
    assert seed.read_text(encoding="utf-8") == before_text
    assert seed.stat().st_mtime == before_mtime


def test_model_credential_run_does_not_leak_secret_or_ref(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api_key(monkeypatch)
    enable_harness(cluster, workspace)
    frames = _install_expose_probe(monkeypatch)
    adapter = credential_recording_adapter()
    now = datetime.now(UTC)
    ctx, outcome, engine, elapsed = _dispatch_and_visit(
        cluster, workspace, owner_account_id, adapter, frozen_clock(now)
    )
    assert elapsed < _WALL_SECONDS
    assert len(adapter.start_calls) == 1
    request, _spawn = adapter.start_calls[0]
    assert leak_in(request.model_dump(mode="json")) is None
    operation = operation_row(engine, outcome.operation_id)
    assert operation is not None
    assert operation.state == "succeeded"
    _assert_surfaces_clean(
        adapter=adapter,
        engine=engine,
        ctx=ctx,
        operation_id=outcome.operation_id,
        expose_frames=frames,
    )


def test_ac8_leak_into_recorded_request_reddens_the_boundary(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2-w4: leaking a resolvable ``secret://`` ref into the recorded request
    must fail the criterion-17 surface assertions."""
    _api_key(monkeypatch)
    enable_harness(cluster, workspace)
    original = runtime_ops.build_runtime_request

    def leaking(**kwargs):  # type: ignore[no-untyped-def]
        request = original(**kwargs)
        return request.model_copy(update={"task": f"{request.task} {SECRET_REF}"})

    monkeypatch.setattr(runtime_ops, "build_runtime_request", leaking)
    adapter = credential_recording_adapter()
    now = datetime.now(UTC)
    ctx, outcome, engine, _elapsed = _dispatch_and_visit(
        cluster, workspace, owner_account_id, adapter, frozen_clock(now)
    )
    with pytest.raises(AssertionError, match="secret reference"):
        _assert_surfaces_clean(
            adapter=adapter,
            engine=engine,
            ctx=ctx,
            operation_id=outcome.operation_id,
            expose_frames=[],
        )
