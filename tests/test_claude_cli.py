"""ClaudeCliRuntime start/poll, stdin document, and secret-resolution seams.

Adapter unit tests point ``runtime.claude_cli.executable`` at stub scripts under
``tmp_path``. They never invoke a host-PATH ``claude``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from rheo_app_worker.main import ADAPTERS, main, refuse_misconfigured_login
from rheo_contracts import (
    Actor,
    ActorKind,
    AdapterSpawn,
    Audience,
    AudienceKind,
    ContextItem,
    ContextPurpose,
    ContextTier,
    Isolation,
    RuntimeLimits,
    RuntimeRequest,
    StructuredOutput,
    TextOutput,
    UsageReporting,
)
from rheo_runtimes.claude_cli import (
    CLAUDE_CLI_SCOPE_COMPONENT,
    CLAUDE_CLI_SCOPE_PREFIXES,
    POLL_WAIT_SECONDS,
    ClaudeCliRuntime,
    stdin_document,
)

_OPERATION_ID = UUID("018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b")
_WORKSPACE_ID = UUID("018f6b2f-0000-7000-8000-000000000001")
_ACTOR_ID = UUID("018f6b2f-0000-7000-8000-000000000002")
_AUDIENCE_ID = UUID("018f6b2f-0000-7000-8000-000000000003")
_TASK = "UNIQUE_TASK_TEXT_FOR_ARGV"
_ITEM_A = "UNIQUE_ITEM_ALPHA"
_ITEM_B = "UNIQUE_ITEM_BRAVO"
_API_SECRET = "sk-ant-test-not-a-real-key"
_RUNTIMES_SRC = (
    Path(__file__).resolve().parents[1] / "runtimes" / "src" / "rheo_runtimes"
)

_CAPTURE_STUB = """#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from pathlib import Path

stdin = sys.stdin.read()
Path("stdin.txt").write_text(stdin)
Path("argv.json").write_text(json.dumps(sys.argv))
Path("env_names.txt").write_text("\\n".join(sorted(os.environ)))
key = os.environ.get("ANTHROPIC_API_KEY", "")
Path("api_key_sha256.txt").write_text(
    hashlib.sha256(key.encode()).hexdigest() if key else ""
)
payload = Path("result.json")
if payload.is_file():
    sys.stdout.write(payload.read_text())
    if not payload.read_text().endswith("\\n"):
        sys.stdout.write("\\n")
else:
    sys.stdout.write(
        '{"type": "result", "session_id": "sess-capture", "result": "ok"}\\n'
    )
sys.stdout.flush()
"""

_HANG_STUB = """#!/usr/bin/env python3
import time
time.sleep(3600)
"""

_TRUNCATED_STUB = """#!/usr/bin/env python3
print('{"type": "assistant", "message": "partial"}')
print("not-json")
"""

_AUTH_STUB = """#!/usr/bin/env python3
print(
    '{"type": "result", "is_error": true, "result": "authentication_failed"}'
)
"""

_DENIED_STUB = """#!/usr/bin/env python3
print('{"type": "result", "is_error": true, "result": "tool_denied"}')
"""


def _write_stub(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _request(
    *,
    context: bool = False,
    output: TextOutput | StructuredOutput | None = None,
    tools: list[str] | None = None,
) -> RuntimeRequest:
    items = []
    if context:
        items = [
            ContextItem(ref="note:a", tier=ContextTier.INTERNAL, text=_ITEM_A),
            ContextItem(ref="note:b", tier=ContextTier.INTERNAL, text=_ITEM_B),
        ]
    return RuntimeRequest(
        operation_id=_OPERATION_ID,
        actor=Actor(kind=ActorKind.ACCOUNT, id=_ACTOR_ID),
        workspace_id=_WORKSPACE_ID,
        audience=Audience(kind=AudienceKind.JOB, id=_AUDIENCE_ID),
        purpose=ContextPurpose.RESPOND,
        task=_TASK,
        context_items=items,
        permitted_tools=tools or ["core.note.get"],
        output=output or TextOutput(),
        requirements=[],
        limits=RuntimeLimits(deadline_seconds=60, max_iterations=7),
        continuation=None,
        credential_slot="model",
        runtime_id="claude_cli",
        model_id="sonnet",
    )


def _spawn(tmp_path: Path, *, native_handle: str | None = None) -> AdapterSpawn:
    work_dir = tmp_path / "work"
    config_dir = tmp_path / "config-dir"
    work_dir.mkdir()
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    return AdapterSpawn(
        mcp_url="http://127.0.0.1:8080/mcp",
        run_token="rheo_runtime_test_token",
        work_dir=str(work_dir),
        config_dir=str(config_dir),
        native_handle=native_handle,
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    executable: Path,
    kind: str = "login",
    seed: Path | None = None,
    credential_ref: str = "",
    account_id: str = "018f6b2f-0000-7000-8000-000000000099",
) -> None:
    isolate_rheo_environment(monkeypatch, tmp_path / "data-root")
    monkeypatch.setenv("RHEO__runtime__claude_cli__executable", str(executable))
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", kind)
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_account_id", account_id)
    if seed is not None:
        monkeypatch.setenv("RHEO__runtime__claude_cli__login_seed_dir", str(seed))
    if credential_ref:
        monkeypatch.setenv("RHEO__runtime__claude_cli__credential_ref", credential_ref)


def _fill_seed(path: Path, *, marker: str = "seed-login") -> Path:
    path.mkdir(parents=True)
    (path / "credentials").write_text(marker)
    return path


def drain_until_terminal(handle: object, *, timeout: float = 2.0) -> object:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        event = handle.poll()  # type: ignore[attr-defined]
        if event is not None:
            last = event
            if event.type in {"failure", "final_output", "cancelled"}:
                return event
        elif handle.finished():  # type: ignore[attr-defined]
            if last is not None:
                return last
            raise AssertionError("handle finished without a terminal event")
    raise AssertionError(f"no terminal event before timeout; last={last!r}")


def test_capabilities_match_the_advisory_vector() -> None:
    caps = ClaudeCliRuntime().capabilities()
    assert caps.tool_calling is True
    assert caps.structured_output is True
    assert caps.streaming is True
    assert caps.continuation is True
    assert caps.cancellation is True
    assert caps.usage_reporting is UsageReporting.EXACT
    assert caps.isolation is Isolation.ADVISORY


def test_stdin_document_is_task_then_items_with_blank_lines() -> None:
    request = _request(context=True)
    document = stdin_document(request)
    assert document == f"{_TASK}\n\n{_ITEM_A}\n\n{_ITEM_B}"
    empty = stdin_document(_request(context=False))
    assert empty == _TASK


def test_capture_stub_writes_stdin_and_keeps_task_off_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    spawn = _spawn(tmp_path)
    handle = ClaudeCliRuntime().start(_request(context=True), spawn=spawn)
    event = drain_until_terminal(handle)
    assert event.type == "final_output"
    assert event.text == "ok"
    assert handle.native_handle == "sess-capture"
    work = Path(spawn.work_dir)
    stdin = (work / "stdin.txt").read_text()
    assert _TASK in stdin
    assert _ITEM_A in stdin
    assert _ITEM_B in stdin
    argv = json.loads((work / "argv.json").read_text())
    joined = " ".join(argv)
    assert _TASK not in joined
    assert _ITEM_A not in joined
    assert _ITEM_B not in joined
    assert "--model" not in argv
    assert argv[1:6] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
    ]
    assert "7" in argv
    assert "--mcp-config" in argv
    assert "--resume" not in argv
    mcp = (work / "mcp.json").read_text()
    assert spawn.mcp_url in mcp
    assert spawn.run_token in mcp
    env_names = (work / "env_names.txt").read_text().splitlines()
    assert "ANTHROPIC_API_KEY" not in env_names
    assert "CLAUDE_CONFIG_DIR" in env_names
    assert "HOME" in env_names


def test_resume_uses_spawn_native_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    spawn = _spawn(tmp_path, native_handle="cli-session-99")
    handle = ClaudeCliRuntime().start(_request(), spawn=spawn)
    drain_until_terminal(handle)
    argv = json.loads((Path(spawn.work_dir) / "argv.json").read_text())
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "cli-session-99"


def test_empty_login_seed_is_credential_invalid_and_does_not_write_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    empty_seed = tmp_path / "empty-seed"
    empty_seed.mkdir()
    _configure(monkeypatch, tmp_path, executable=stub, seed=empty_seed)
    spawn = _spawn(tmp_path)
    handle = ClaudeCliRuntime().start(_request(), spawn=spawn)
    event = drain_until_terminal(handle)
    assert event.type == "failure"
    assert event.kind.value == "credential_invalid"
    assert not Path(spawn.config_dir).exists()
    assert not (Path(spawn.work_dir) / "argv.json").exists()


def test_missing_login_seed_is_credential_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    _configure(monkeypatch, tmp_path, executable=stub, seed=tmp_path / "no-such-seed")
    spawn = _spawn(tmp_path)
    event = drain_until_terminal(ClaudeCliRuntime().start(_request(), spawn=spawn))
    assert event.type == "failure"
    assert event.kind.value == "credential_invalid"


def test_second_login_start_does_not_recopy_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    seed = _fill_seed(tmp_path / "seed", marker="original")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    spawn = _spawn(tmp_path)
    runtime = ClaudeCliRuntime()
    drain_until_terminal(runtime.start(_request(), spawn=spawn))
    copied = Path(spawn.config_dir) / "credentials"
    assert copied.read_text() == "original"
    (Path(spawn.config_dir) / "projects").mkdir()
    (seed / "credentials").write_text("updated-seed")
    (seed / "extra").write_text("new-file")
    drain_until_terminal(runtime.start(_request(), spawn=spawn))
    assert copied.read_text() == "original"
    assert not (Path(spawn.config_dir) / "extra").exists()


def test_api_key_resolves_into_child_env_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    _configure(
        monkeypatch,
        tmp_path,
        executable=stub,
        kind="api_key",
        credential_ref="secret://env/RHEO_ANTHROPIC_API_KEY",
    )
    monkeypatch.setenv("RHEO_ANTHROPIC_API_KEY", _API_SECRET)
    spawn = _spawn(tmp_path)
    event = drain_until_terminal(ClaudeCliRuntime().start(_request(), spawn=spawn))
    assert event.type == "final_output"
    work = Path(spawn.work_dir)
    digest = (work / "api_key_sha256.txt").read_text()
    assert digest == hashlib.sha256(_API_SECRET.encode()).hexdigest()
    argv = json.loads((work / "argv.json").read_text())
    assert _API_SECRET not in json.dumps(argv)
    assert _API_SECRET not in (work / "stdin.txt").read_text()
    assert _API_SECRET not in (work / "mcp.json").read_text()
    assert Path(spawn.config_dir).is_dir()
    assert list(Path(spawn.config_dir).iterdir()) == []
    assert CLAUDE_CLI_SCOPE_COMPONENT == "runtime.claude_cli"
    assert "secret://env/RHEO_ANTHROPIC_API_KEY" in CLAUDE_CLI_SCOPE_PREFIXES


def test_empty_api_key_ref_is_credential_invalid_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    _configure(
        monkeypatch, tmp_path, executable=stub, kind="api_key", credential_ref=""
    )
    spawn = _spawn(tmp_path)
    event = drain_until_terminal(ClaudeCliRuntime().start(_request(), spawn=spawn))
    assert event.type == "failure"
    assert event.kind.value == "credential_invalid"
    assert not (Path(spawn.work_dir) / "argv.json").exists()


def test_auth_error_maps_to_credential_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _AUTH_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    event = drain_until_terminal(
        ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    )
    assert event.type == "failure"
    assert event.kind.value == "credential_invalid"


def test_tool_denied_maps_to_tool_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _DENIED_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    event = drain_until_terminal(
        ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    )
    assert event.type == "failure"
    assert event.kind.value == "tool_denied"


def test_exit_without_result_is_stream_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _TRUNCATED_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    event = drain_until_terminal(
        ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    )
    assert event.type == "failure"
    assert event.kind.value == "stream_truncated"


def test_missing_binary_is_executable_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-claude"
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=missing, seed=seed)
    spawn = _spawn(tmp_path)
    event = drain_until_terminal(ClaudeCliRuntime().start(_request(), spawn=spawn))
    assert event.type == "failure"
    assert event.kind.value == "executable_unavailable"
    assert not (Path(spawn.work_dir) / "argv.json").exists()


def test_non_executable_file_is_executable_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blob = tmp_path / "not-exec"
    blob.write_text("#!/usr/bin/env python3\n")
    blob.chmod(0o644)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=blob, seed=seed)
    event = drain_until_terminal(
        ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    )
    assert event.type == "failure"
    assert event.kind.value == "executable_unavailable"


def test_poll_waits_then_cancel_is_deadline_exceeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _HANG_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    handle = ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    try:
        started = time.monotonic()
        assert handle.poll() is None
        assert handle.finished() is False
        assert time.monotonic() - started >= POLL_WAIT_SECONDS
        handle.cancel()
        event = drain_until_terminal(handle)
        assert event.type == "failure"
        assert event.kind.value == "deadline_exceeded"
    finally:
        handle.cancel()


def test_structured_output_match_emits_final_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    spawn = _spawn(tmp_path)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    (Path(spawn.work_dir) / "result.json").write_text(
        json.dumps(
            {"type": "result", "session_id": "sess-struct", "result": '{"ok": true}'}
        )
        + "\n"
    )
    event = drain_until_terminal(
        ClaudeCliRuntime().start(
            _request(output=StructuredOutput(json_schema=schema)),
            spawn=spawn,
        )
    )
    assert event.type == "final_output"
    assert event.structured == {"ok": True}


def test_structured_output_mismatch_is_output_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _CAPTURE_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    spawn = _spawn(tmp_path)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    (Path(spawn.work_dir) / "result.json").write_text(
        json.dumps(
            {"type": "result", "session_id": "sess-bad", "result": '{"ok": "nope"}'}
        )
        + "\n"
    )
    event = drain_until_terminal(
        ClaudeCliRuntime().start(
            _request(output=StructuredOutput(json_schema=schema)),
            spawn=spawn,
        )
    )
    assert event.type == "failure"
    assert event.kind.value == "output_invalid"


def test_start_and_poll_do_not_raise_named_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _write_stub(tmp_path / "claude-stub", _AUTH_STUB)
    seed = _fill_seed(tmp_path / "seed")
    _configure(monkeypatch, tmp_path, executable=stub, seed=seed)
    handle = ClaudeCliRuntime().start(_request(), spawn=_spawn(tmp_path))
    event = drain_until_terminal(handle)
    assert event.type == "failure"


def test_production_registry_holds_claude_cli() -> None:
    assert ADAPTERS.lookup("claude_cli") is not None
    assert isinstance(ADAPTERS.lookup("claude_cli"), ClaudeCliRuntime)


def test_startup_guard_refuses_login_without_account_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    isolate_rheo_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "RHEO__runtime__claude_cli__executable", str(tmp_path / "claude")
    )
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    with pytest.raises(SystemExit, match="credential_account_id"):
        main()


def test_startup_guard_allows_unconfigured_skeleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    isolate_rheo_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    refuse_misconfigured_login()


def test_runtimes_ast_scan_keeps_storage_operations_and_routing_out() -> None:
    sources = sorted(_RUNTIMES_SRC.rglob("*.py"))
    assert sources
    violations: dict[str, list[str]] = {}
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders: list[str] = []
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                if name == "rheo_core.storage" or (
                    name.startswith("rheo_core.storage.")
                    and not (
                        name == "rheo_core.storage.data_root"
                        or name.startswith("rheo_core.storage.data_root.")
                    )
                ):
                    offenders.append(name)
                if name == "rheo_core.operations" or name.startswith(
                    "rheo_core.operations."
                ):
                    offenders.append(name)
                if name == "rheo_core.routing" or name.startswith("rheo_core.routing."):
                    offenders.append(name)
        if offenders:
            violations[str(path)] = offenders
    assert not violations, violations
