"""Claude CLI adapter: spawn the configured executable, never a database.

Credential resolution happens in :meth:`ClaudeCliRuntime.start` (criterion 17).
Core fills :class:`~rheo_contracts.AdapterSpawn`; this module uses those fields as
handed and does not recompute config paths except the login seed copy target.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import (  # type: ignore[import-untyped]
    SchemaError,
    ValidationError,
)
from pydantic import JsonValue
from rheo_contracts import (
    AdapterSpawn,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    Isolation,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeRequest,
    StructuredOutput,
    UsageEvent,
    UsageKind,
    UsageReporting,
)
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore
from rheo_core.settings import resolve
from rheo_core.storage.data_root import resolve_data_root

CLAUDE_CLI_SCOPE_COMPONENT: Final = "runtime.claude_cli"
# The references ``credential_ref`` may name, per credential kind. Each kind gets the
# adapter's file prefix and its own one environment variable, and nothing else, so an
# ``api_key`` deployment cannot read the OAuth token's variable or the reverse.
CLAUDE_CLI_SCOPE_PREFIXES: Final = (
    "secret://file/runtime/claude-cli/",
    "secret://env/RHEO_ANTHROPIC_API_KEY",
)
CLAUDE_CLI_OAUTH_SCOPE_PREFIXES: Final = (
    "secret://file/runtime/claude-cli/",
    "secret://env/RHEO_CLAUDE_OAUTH_TOKEN",
)
# credential_kind -> (the admitted references, the one child variable it is passed as).
# ``login`` is absent: it passes no credential variable and copies the login seed.
_SECRET_CREDENTIALS: Final[Mapping[str, tuple[tuple[str, ...], str]]] = {
    "api_key": (CLAUDE_CLI_SCOPE_PREFIXES, "ANTHROPIC_API_KEY"),
    "oauth_token": (CLAUDE_CLI_OAUTH_SCOPE_PREFIXES, "CLAUDE_CODE_OAUTH_TOKEN"),
}
DEFAULT_LOGIN_SEED: Final = Path("config") / "claude-cli-login"
MCP_CONFIG_NAME: Final = "mcp.json"
POLL_WAIT_SECONDS: Final = 0.05
POLL_WAIT_CAP_SECONDS: Final = 0.25
CONFIG_DIR_MODE: Final = 0o700
MCP_CONFIG_MODE: Final = 0o600

_LOCALE_KEYS: Final = frozenset({"LANG", "LANGUAGE", "LC_ALL"})
# Set on every child whatever the host has, since the allowlist drops the image's own
# ENV. Both names are from code.claude.com/docs: DISABLE_AUTOUPDATER stops the
# background update check (en/setup, "Disable auto-updates"), and
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC also stops telemetry, error reporting and
# feature-flag fetching (en/env-vars).
_FIXED_CHILD_ENV: Final[Mapping[str, str]] = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
# Where a copied login seed keeps its credential: ``.credentials.json`` under
# CLAUDE_CONFIG_DIR (code.claude.com/docs/en/authentication), and under
# ``$HOME/.claude`` too, since HOME is the same directory. A secret-backed kind
# removes both at spawn, so a workspace that ran under ``login`` cannot fall back
# to the old seed.
_LOGIN_CREDENTIAL_FILES: Final = (
    Path(".credentials.json"),
    Path(".claude") / ".credentials.json",
)


class ClaudeCliHandle:
    """Live or already-failed handle. Named failures are events, not exceptions."""

    def __init__(
        self,
        *,
        process: subprocess.Popen[str] | None = None,
        output_kind: str,
        json_schema: Mapping[str, object] | None,
        queued: list[RuntimeEvent] | None = None,
        wait: float = POLL_WAIT_SECONDS,
    ) -> None:
        self.native_handle: str | None = None
        self._process = process
        self._output_kind = output_kind
        self._json_schema = json_schema
        self._pending: list[RuntimeEvent] = list(queued or ())
        self._wait = wait
        self._backoff = wait
        self._cancelled = False
        self._finished = False
        self._saw_result = False
        self._stdout_closed = process is None
        self._lines: queue.Queue[str | None] = queue.Queue()
        if process is not None and process.stdout is not None:
            thread = threading.Thread(target=self._read_stdout, daemon=True)
            thread.start()
        elif process is None and not self._pending:
            self._finished = True

    def _read_stdout(self) -> None:
        stdout = self._process.stdout if self._process is not None else None
        if stdout is None:
            self._lines.put(None)
            return
        try:
            for line in stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def poll(self) -> RuntimeEvent | None:
        if self._finished and not self._pending:
            return None
        self._drain_lines(block=False)
        event = self._pop_pending()
        if event is not None:
            return event
        exited = self._terminal_if_exited()
        if exited is not None or self._finished:
            return exited
        if not self._stdout_closed:
            self._drain_lines(block=True)
            event = self._pop_pending()
            if event is not None:
                return event
        return self._terminal_if_exited()

    def finished(self) -> bool:
        return self._finished and not self._pending

    def cancel(self) -> None:
        self._cancelled = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()

    def _drain_lines(self, *, block: bool) -> None:
        timeout = self._backoff if block and not self._stdout_closed else None
        while True:
            try:
                if block and timeout is not None:
                    line = self._lines.get(timeout=timeout)
                    timeout = None
                    block = False
                else:
                    line = self._lines.get_nowait()
            except queue.Empty:
                if block:
                    self._backoff = min(self._backoff * 2, POLL_WAIT_CAP_SECONDS)
                return
            if line is None:
                self._stdout_closed = True
                return
            self._backoff = self._wait
            self._parse_line(line)

    def _pop_pending(self) -> RuntimeEvent | None:
        if not self._pending:
            return None
        event = self._pending.pop(0)
        if event.type in {"failure", "final_output", "cancelled"}:
            self._finished = True
        return event

    def _terminal_if_exited(self) -> RuntimeEvent | None:
        process = self._process
        exited = process is None or process.poll() is not None
        if not (exited or self._stdout_closed):
            return None
        if self._cancelled:
            self._finished = True
            return FailureEvent(
                kind=FailureKind.DEADLINE_EXCEEDED,
                detail="the process group was killed at the run deadline",
            )
        if self._saw_result:
            self._finished = True
            return None
        if not self._stdout_closed:
            return None
        self._finished = True
        return FailureEvent(
            kind=FailureKind.STREAM_TRUNCATED,
            detail="the process exited without a stream-json result line",
        )

    def _parse_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return
        self._saw_result = True
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.native_handle = session_id
        usage = _usage_event(payload)
        if usage is not None:
            # Before the terminal event, so the core records it before the loop ends.
            self._pending.append(usage)
        if payload.get("is_error"):
            self._pending.append(_error_result(payload.get("result")))
            return
        self._pending.extend(self._success_events(payload.get("result")))

    def _success_events(self, result: object) -> list[RuntimeEvent]:
        if self._output_kind == "structured":
            parsed = _parse_structured(result)
            if parsed is None or self._json_schema is None:
                return [
                    FailureEvent(
                        kind=FailureKind.OUTPUT_INVALID,
                        detail="the result was not JSON matching the request schema",
                    )
                ]
            try:
                Draft202012Validator(self._json_schema).validate(parsed)
            except (ValidationError, SchemaError, TypeError):
                return [
                    FailureEvent(
                        kind=FailureKind.OUTPUT_INVALID,
                        detail="the result was not JSON matching the request schema",
                    )
                ]
            return [FinalOutputEvent(structured=parsed)]
        if isinstance(result, str):
            body = result
        elif result is None:
            body = ""
        else:
            body = json.dumps(result)
        return [FinalOutputEvent(text=body)]


_INPUT_USAGE_KEYS: Final = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_event(payload: Mapping[str, object]) -> UsageEvent | None:
    """The result line's own usage, as the ``exact`` usage this adapter declares.

    ``input_tokens`` is every input token the model processed, cached or not: the
    CLI reports fresh, cache-write and cache-read input separately. ``cost`` is the
    CLI's ``total_cost_usd``. A result line without a well-formed ``usage`` object
    reports nothing, rather than a guess; a missing cost reads as zero.
    """
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    output_tokens = _count(usage.get("output_tokens"))
    input_parts = [_count(usage.get(key)) for key in _INPUT_USAGE_KEYS]
    if output_tokens is None or input_parts[0] is None:
        return None
    cost = payload.get("total_cost_usd")
    return UsageEvent(
        input_tokens=sum(part for part in input_parts if part is not None),
        output_tokens=output_tokens,
        cost=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else 0.0,
        kind=UsageKind.EXACT,
    )


def _error_result(result: object) -> FailureEvent:
    if result == "authentication_failed":
        return FailureEvent(
            kind=FailureKind.CREDENTIAL_INVALID,
            detail="the executable reported an authentication failure",
        )
    if result == "tool_denied":
        return FailureEvent(
            kind=FailureKind.TOOL_DENIED,
            detail="the executable reported a denied tool",
        )
    return FailureEvent(
        kind=FailureKind.RUNTIME_ERROR,
        detail="the executable reported an error result",
    )


def _parse_structured(result: object) -> JsonValue | None:
    if isinstance(result, str):
        try:
            return cast(JsonValue, json.loads(result))
        except json.JSONDecodeError:
            return None
    if isinstance(result, dict | list | str | bool | int | float) or result is None:
        return cast(JsonValue, result)
    return None


def _failed_handle(
    kind: FailureKind, detail: str, *, output_kind: str = "text"
) -> ClaudeCliHandle:
    return ClaudeCliHandle(
        output_kind=output_kind,
        json_schema=None,
        queued=[FailureEvent(kind=kind, detail=detail)],
    )


def stdin_document(request: RuntimeRequest) -> str:
    """Task, then each context item's text, each preceded by a blank line."""

    parts = [request.task, *(item.text for item in request.context_items)]
    return "\n\n".join(parts)


def _seed_source() -> Path:
    configured = resolve().get_str("runtime.claude_cli.login_seed_dir")
    if configured:
        return Path(configured)
    return resolve_data_root().path / DEFAULT_LOGIN_SEED


def _seed_is_empty(path: Path) -> bool:
    if not path.is_dir():
        return True
    return not any(candidate.is_file() for candidate in path.rglob("*"))


def _copy_login_seed(config_dir: Path) -> FailureEvent | None:
    if config_dir.exists():
        return None
    source = _seed_source()
    if _seed_is_empty(source):
        return FailureEvent(
            kind=FailureKind.CREDENTIAL_INVALID,
            detail="runtime.claude_cli.login_seed_dir is missing or empty",
        )
    shutil.copytree(source, config_dir)
    os.chmod(config_dir, CONFIG_DIR_MODE)
    return None


def _prepare_secret_config_dir(config_dir: Path) -> FailureEvent | None:
    """The configuration directory for a secret credential: no login seed in it.

    Created empty the first time. The directory is per workspace, so it may already
    hold a seed copied under ``login``: the known credential files are removed and
    everything else (``projects/``, the native sessions continuation resumes) stays.
    A credential file that cannot be removed refuses the spawn.
    """
    config_dir.mkdir(mode=CONFIG_DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, CONFIG_DIR_MODE)
    except OSError:
        pass
    for relative in _LOGIN_CREDENTIAL_FILES:
        try:
            (config_dir / relative).unlink(missing_ok=True)
        except OSError:
            return FailureEvent(
                kind=FailureKind.CREDENTIAL_INVALID,
                detail="a stale login credential could not be removed",
            )
    return None


def _resolve_secret_credential(prefixes: tuple[str, ...]) -> str | FailureEvent:
    """``credential_ref`` through the adapter's scope for one credential kind.

    Every failure is a fixed-text ``credential_invalid``: neither the reference's
    value nor the refusal's own message is carried into the event.
    """
    ref_text = resolve().get_str("runtime.claude_cli.credential_ref")
    if not ref_text.strip():
        return FailureEvent(
            kind=FailureKind.CREDENTIAL_INVALID,
            detail="runtime.claude_cli.credential_ref is empty",
        )
    store = SecretStore(resolve_data_root().path)
    scope = SecretStore.scope_for(CLAUDE_CLI_SCOPE_COMPONENT, *prefixes)
    try:
        secret = store.resolve(SecretRef.parse(ref_text), scope)
        try:
            value = secret.expose().decode("utf-8")
        finally:
            del secret
    except (SecretRefusal, TypeError, ValueError, UnicodeDecodeError):
        return FailureEvent(
            kind=FailureKind.CREDENTIAL_INVALID,
            detail="the model credential could not be resolved",
        )
    if not value:
        return FailureEvent(
            kind=FailureKind.CREDENTIAL_INVALID,
            detail="the model credential resolved empty",
        )
    return value


def _child_env(
    *, config_dir: str, credential: tuple[str, str] | None
) -> dict[str, str]:
    """The allowlisted child environment.

    ``credential`` is ``(variable, value)`` for a secret credential kind, and the
    only credential variable the child ever sees; ``None`` under ``login``.
    """
    env: dict[str, str] = {}
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    for key, value in os.environ.items():
        if key in _LOCALE_KEYS or key.startswith("LC_"):
            env[key] = value
    env.update(_FIXED_CHILD_ENV)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["HOME"] = config_dir
    if credential is not None:
        variable, value = credential
        env[variable] = value
    return env


def _feed_stdin(process: subprocess.Popen[str], document: str) -> None:
    """Write the stdin document after the handle's stdout reader is already draining.

    A daemon thread keeps ``start`` from blocking on a child that ignores stdin
    (pipe-buffer fill) or prints stream-json before finishing the read.
    """

    stdin = process.stdin
    if stdin is None:
        return

    def _write() -> None:
        try:
            stdin.write(document)
            stdin.close()
        except BrokenPipeError:
            pass

    threading.Thread(target=_write, daemon=True).start()


def _write_mcp_config(work_dir: Path, spawn: AdapterSpawn) -> Path:
    path = work_dir / MCP_CONFIG_NAME
    document = {
        "mcpServers": {
            "rheo": {
                "type": "http",
                "url": spawn.mcp_url,
                "headers": {"Authorization": f"Bearer {spawn.run_token}"},
            }
        }
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, MCP_CONFIG_MODE)
    return path


def _argv(
    executable: str,
    request: RuntimeRequest,
    spawn: AdapterSpawn,
    mcp_config: Path,
) -> list[str]:
    argv = [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(request.limits.max_iterations),
        "--mcp-config",
        str(mcp_config),
        "--allowedTools",
        ",".join(request.permitted_tools),
    ]
    if spawn.model_override:
        argv.extend(["--model", spawn.model_override])
    if spawn.disable_builtin_tools:
        # Doc-sourced: an empty --tools value disables every built-in tool.
        # --allowedTools "" only pre-approves nothing. Unverified against the
        # installed CLI until CP-B checks that a tool call is refused.
        argv.extend(["--tools", ""])
    if spawn.native_handle:
        argv.extend(["--resume", spawn.native_handle])
    return argv


def _executable_unavailable(executable: str) -> bool:
    if not executable:
        return True
    path = Path(executable)
    return not path.is_file() or not os.access(path, os.X_OK)


class ClaudeCliRuntime:
    """``runtime_id = claude_cli``. Isolation is advisory in release one."""

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            tool_calling=True,
            structured_output=True,
            streaming=True,
            continuation=True,
            cancellation=True,
            usage_reporting=UsageReporting.EXACT,
            isolation=Isolation.ADVISORY,
        )

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> ClaudeCliHandle:
        output_kind = request.output.kind
        json_schema: Mapping[str, object] | None = None
        if isinstance(request.output, StructuredOutput):
            json_schema = dict(request.output.json_schema)
        settings = resolve()
        executable = settings.get_str("runtime.claude_cli.executable")
        if _executable_unavailable(executable):
            return _failed_handle(
                FailureKind.EXECUTABLE_UNAVAILABLE,
                "runtime.claude_cli.executable is missing or not executable",
                output_kind=output_kind,
            )
        kind = settings.get_str("runtime.claude_cli.credential_kind")
        config_dir = Path(spawn.config_dir)
        credential: tuple[str, str] | None = None
        if kind == "login":
            failure = _copy_login_seed(config_dir)
            if failure is not None:
                return ClaudeCliHandle(
                    output_kind=output_kind,
                    json_schema=json_schema,
                    queued=[failure],
                )
        elif kind in _SECRET_CREDENTIALS:
            prefixes, variable = _SECRET_CREDENTIALS[kind]
            resolved = _resolve_secret_credential(prefixes)
            if isinstance(resolved, FailureEvent):
                return ClaudeCliHandle(
                    output_kind=output_kind,
                    json_schema=json_schema,
                    queued=[resolved],
                )
            failure = _prepare_secret_config_dir(config_dir)
            if failure is not None:
                return ClaudeCliHandle(
                    output_kind=output_kind,
                    json_schema=json_schema,
                    queued=[failure],
                )
            credential = (variable, resolved)
        else:
            return ClaudeCliHandle(
                output_kind=output_kind,
                json_schema=json_schema,
                queued=[
                    FailureEvent(
                        kind=FailureKind.CREDENTIAL_INVALID,
                        detail="runtime.claude_cli.credential_kind is not supported",
                    )
                ],
            )

        work_dir = Path(spawn.work_dir)
        try:
            mcp_config = _write_mcp_config(work_dir, spawn)
            argv = _argv(executable, request, spawn, mcp_config)
            document = stdin_document(request)
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(work_dir),
                env=_child_env(config_dir=str(config_dir), credential=credential),
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError:
            return _failed_handle(
                FailureKind.EXECUTABLE_UNAVAILABLE,
                "the executable could not be spawned",
                output_kind=output_kind,
            )
        handle = ClaudeCliHandle(
            process=process,
            output_kind=output_kind,
            json_schema=json_schema,
        )
        _feed_stdin(process, document)
        return handle
