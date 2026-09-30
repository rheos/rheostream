"""The ``claude_cli`` extraction provider, through a scripted adapter only (spec
§ Architecture, System Components item 7; FR 13, FR 17; AC 12; R2).

Seams under test: :meth:`ClaudeCliExtractionProvider.extract` — the login binding,
the request and spawn it hands the adapter, the throwaway directory's lifecycle, and
how each way a run can end becomes a result or a raise — and the import boundary that
keeps ``rheo_runtimes`` out of ``packages/core``.

No test here starts a process. The autouse guard makes ``subprocess.Popen`` raise and
points the CLI executable at a path that does not exist; every test that expects a
start asserts the scripted adapter served it and that ``Popen`` was never reached.
The token reader, the revoker and the settings reader are replaced by in-memory ones,
so nothing here needs a cluster either.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from harness.extraction import PopenGuard, ScriptedAdapter, forbid_real_processes
from rheo_contracts import (
    ApprovalRequiredEvent,
    CancelledEvent,
    ContextPurpose,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    ProgressEvent,
    RuntimeEvent,
    StructuredOutput,
    ToolCallEvent,
    UsageEvent,
    UsageKind,
)
from rheo_core.evidence import providers
from rheo_core.evidence.extract import (
    DigestBatch,
    ExtractionScope,
    digest,
    extraction_response_schema,
)
from rheo_core.runtime import AdapterRegistry
from rheo_core.runtime.extraction import (
    ADAPTER_UNAVAILABLE,
    DEADLINE_EXCEEDED,
    EXTRACTION_INSTRUCTION,
    NO_STRUCTURED_RESULT,
    PROVIDER_NAME,
    STREAM_TRUNCATED,
    TOKEN_REVOKE_FAILED,
    ClaudeCliExtractionProvider,
    ExtractionCredentialNotOwned,
    ExtractionRunFailed,
    register_claude_cli_extraction,
)
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.storage.data_root import Purpose, workspace_dir_for

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_CORE_SRC: Final = _REPO_ROOT / "packages/core/src"

_WORKSPACE: Final = UUID("018f7a10-0000-7000-8000-00000000a001")
_ACCOUNT: Final = UUID("018f7a10-0000-7000-8000-00000000a002")
_OTHER_ACCOUNT: Final = UUID("018f7a10-0000-7000-8000-00000000a003")
_PURPOSE: Final = ContextPurpose.INTERNAL_ANALYSIS
_TOKEN_ID: Final = UUID("018f7a10-0000-7000-8000-00000000a004")
_RUN_TOKEN: Final = "rheo_rt_synthetic_not_a_token"
_MODEL_ENV: Final = "RHEO__automatic_memory__extraction__model_id"
_KIND_ENV: Final = "RHEO__runtime__claude_cli__credential_kind"
_ACCOUNT_ENV: Final = "RHEO__runtime__claude_cli__credential_account_id"
_TEXT: Final = "I have decided to keep the allotment another year."
_ANSWER: Final[dict[str, object]] = {
    "items": [
        {
            "item": "u1",
            "memory": {"kind": "decision", "title": "Allotment", "body": _TEXT},
        }
    ]
}


@pytest.fixture(autouse=True)
def popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PopenGuard:
    """The no-process guard, over an isolated environment whose login credential is
    bound to this file's speaker."""
    isolate_rheo_environment(monkeypatch, tmp_path / "data")
    monkeypatch.setenv(_KIND_ENV, "login")
    monkeypatch.setenv(_ACCOUNT_ENV, str(_ACCOUNT))
    return forbid_real_processes(monkeypatch, tmp_path)


class _Tokens:
    """The token reader and revoker, in memory."""

    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []
        self.revoked: list[UUID] = []

    def issue(self, **kwargs: Any) -> tuple[UUID, str]:
        self.issued.append(kwargs)
        return _TOKEN_ID, _RUN_TOKEN

    def revoke(self, token_id: UUID) -> None:
        self.revoked.append(token_id)


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


def _settings(workspace_id: UUID) -> ResolvedSettings:
    assert workspace_id == _WORKSPACE
    return resolve()


def _provider(
    adapter: ScriptedAdapter | None,
    tokens: _Tokens,
    *,
    clock: Any = None,
) -> ClaudeCliExtractionProvider:
    registry = AdapterRegistry()
    if adapter is not None:
        registry.register(PROVIDER_NAME, adapter)
    extra: dict[str, Any] = {} if clock is None else {"clock": clock}
    return ClaudeCliExtractionProvider(
        registry,
        settings_for=_settings,
        issue_token=tokens.issue,
        revoke_token=tokens.revoke,
        **extra,
    )


def _batch(
    *texts: str,
    account_id: UUID = _ACCOUNT,
    mention_kinds: tuple[str, ...] = (),
) -> DigestBatch:
    return digest(
        list(texts or (_TEXT,)),
        mention_kinds=mention_kinds,
        scope=ExtractionScope(
            workspace_id=_WORKSPACE, account_id=account_id, purpose=_PURPOSE
        ),
    )


def _answered(structured: Any = None) -> list[RuntimeEvent]:
    return [
        ProgressEvent(text="working", fraction=0.5),
        UsageEvent(input_tokens=1, output_tokens=1, cost=0.0, kind=UsageKind.EXACT),
        FinalOutputEvent(structured=_ANSWER if structured is None else structured),
    ]


def _throwaway_root(adapter: ScriptedAdapter) -> Path:
    [start] = adapter.starts
    return Path(start.spawn.work_dir).parent


def _served(adapter: ScriptedAdapter, popen: PopenGuard) -> None:
    """The scripted adapter, not a process, served the call."""
    assert len(adapter.starts) == 1
    assert popen.calls == []


# --- the request and the spawn --------------------------------------------------------


def test_a_structured_result_is_returned_as_the_raw_mapping(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())

    result = _provider(adapter, tokens).extract(_batch())

    assert result == _ANSWER
    _served(adapter, popen)


def test_the_request_permits_nothing_and_asks_for_the_response_schema(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    request = adapter.starts[0].request
    assert request.permitted_tools == []
    assert request.limits.max_iterations == 1
    assert request.output == StructuredOutput(json_schema=extraction_response_schema())
    assert request.purpose is _PURPOSE
    assert request.workspace_id == _WORKSPACE
    assert request.runtime_id == PROVIDER_NAME
    assert request.context_items == []
    assert request.continuation is None
    assert request.task.startswith(EXTRACTION_INSTRUCTION)
    assert _TEXT in request.task


def test_the_spawn_disables_built_in_tools_and_carries_the_run_token(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    spawn = adapter.starts[0].spawn
    assert spawn.disable_builtin_tools is True
    assert spawn.run_token == _RUN_TOKEN
    assert spawn.native_handle is None


def test_the_minted_token_has_an_empty_snapshot_and_is_revoked(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    [issued] = tokens.issued
    assert issued["operations"] == []
    assert issued["account_id"] == _ACCOUNT
    assert issued["workspace_id"] == _WORKSPACE
    assert issued["purpose"] == _PURPOSE.value
    assert tokens.revoked == [_TOKEN_ID]


def test_the_batch_scope_never_reaches_the_prompt(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    """Carried item (b): the workspace, the speaker and the purpose are for the
    provider's own checks; none of them is in the text the model reads, in any
    spelling, for any purpose value."""
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch(mention_kinds=("person", "place")))

    _served(adapter, popen)
    request = adapter.starts[0].request
    model_text = "\n".join([request.task, *(i.text for i in request.context_items)])
    for identifier in (_WORKSPACE, _ACCOUNT):
        for spelling in (str(identifier), identifier.hex, str(identifier).upper()):
            assert spelling not in model_text
    for purpose in ContextPurpose:
        assert purpose.value not in model_text
        assert purpose.value.replace("_", " ") not in model_text
    # The two things the model is meant to see are there.
    assert _TEXT in model_text
    assert "person, place" in model_text


def test_every_item_is_in_the_task_as_json_data(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch("first passage", "second passage"))

    task = adapter.starts[0].request.task
    assert '"item": "u1", "text": "first passage"' in task
    assert '"item": "u2", "text": "second passage"' in task
    _served(adapter, popen)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("   ", None), (" claude-model-x ", "claude-model-x")],
)
def test_the_model_override_is_the_stripped_key_or_none(
    monkeypatch: pytest.MonkeyPatch,
    tokens: _Tokens,
    popen: PopenGuard,
    value: str | None,
    expected: str | None,
) -> None:
    """Carried item (a): whitespace-only means unset."""
    if value is not None:
        monkeypatch.setenv(_MODEL_ENV, value)
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    assert adapter.starts[0].spawn.model_override == expected


# --- the throwaway directory ----------------------------------------------------------


def test_the_work_dir_exists_at_start_and_the_config_dir_does_not(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    """The adapter, not the provider, prepares the configuration directory."""
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    [start] = adapter.starts
    assert start.work_dir_existed is True
    assert start.config_dir_existed is False
    root = _throwaway_root(adapter)
    assert Path(start.spawn.config_dir).parent == root
    assert root.parent == workspace_dir_for(_WORKSPACE, Purpose.SCRATCH) / "extract"


def test_the_throwaway_root_is_gone_after_a_success(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(_answered())
    _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    assert not _throwaway_root(adapter).exists()


def test_the_throwaway_root_is_gone_after_a_raise(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    adapter = ScriptedAdapter(
        [FailureEvent(kind=FailureKind.RUNTIME_ERROR, detail="x")]
    )
    with pytest.raises(ExtractionRunFailed):
        _provider(adapter, tokens).extract(_batch())

    _served(adapter, popen)
    assert not _throwaway_root(adapter).exists()
    assert tokens.revoked == [_TOKEN_ID]


class _RevokeFails(_Tokens):
    """A revoker whose database error quotes text that must not surface."""

    def revoke(self, token_id: UUID) -> None:
        super().revoke(token_id)
        raise RuntimeError(f"revoke failed near {_TEXT}")


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(_answered(), id="after-a-success"),
        pytest.param(
            [FailureEvent(kind=FailureKind.RUNTIME_ERROR, detail="x")],
            id="after-a-failed-run",
        ),
    ],
)
def test_a_failing_revoke_still_removes_the_throwaway_root(
    popen: PopenGuard, events: list[RuntimeEvent]
) -> None:
    """The directory goes whatever the revoke does, and the revoke's failure
    surfaces content-free, with its own error off the exception chain."""
    tokens = _RevokeFails()
    adapter = ScriptedAdapter(events)
    with pytest.raises(ExtractionRunFailed) as raised:
        _provider(adapter, tokens).extract(_batch())

    assert raised.value.kind == TOKEN_REVOKE_FAILED
    assert tokens.revoked == [_TOKEN_ID]
    assert not _throwaway_root(adapter).exists()
    chained = raised.value.__context__
    assert not isinstance(chained, RuntimeError)
    assert _TEXT not in str(raised.value)
    _served(adapter, popen)


def test_each_call_gets_its_own_throwaway_root(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    first, second = ScriptedAdapter(_answered()), ScriptedAdapter(_answered())
    _provider(first, tokens).extract(_batch())
    _provider(second, tokens).extract(_batch())

    assert _throwaway_root(first) != _throwaway_root(second)
    assert popen.calls == []


# --- how a run ends -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("events", "kind"),
    [
        pytest.param(
            [ToolCallEvent(tool="Bash", arguments_digest="d")],
            STREAM_TRUNCATED,
            id="tool-use-then-run-end",
        ),
        pytest.param(
            [
                ToolCallEvent(tool="Bash", arguments_digest="d"),
                FinalOutputEvent(text="done"),
            ],
            NO_STRUCTURED_RESULT,
            id="tool-use-then-text-only",
        ),
        pytest.param([], STREAM_TRUNCATED, id="missing-result"),
        pytest.param(
            [FinalOutputEvent(structured=["not", "an", "object"])],
            NO_STRUCTURED_RESULT,
            id="structured-not-an-object",
        ),
        pytest.param(
            [FailureEvent(kind=FailureKind.TOOL_DENIED, detail="quoted text")],
            "tool_denied",
            id="failure-event",
        ),
        pytest.param([CancelledEvent()], "cancelled", id="cancelled"),
        pytest.param(
            [ApprovalRequiredEvent(approval_id=_TOKEN_ID)],
            "approval_required",
            id="approval-requested",
        ),
    ],
)
def test_every_run_without_a_structured_result_raises(
    tokens: _Tokens,
    popen: PopenGuard,
    events: list[RuntimeEvent],
    kind: str,
) -> None:
    adapter = ScriptedAdapter(events)
    with pytest.raises(ExtractionRunFailed) as raised:
        _provider(adapter, tokens).extract(_batch())

    assert raised.value.kind == kind
    # Content-free: the adapter's own detail text never rides on the exception.
    assert "quoted text" not in str(raised.value)
    _served(adapter, popen)
    assert not _throwaway_root(adapter).exists()


def test_the_deadline_cancels_the_run_and_raises(
    tokens: _Tokens, popen: PopenGuard
) -> None:
    """An injected clock that jumps past ``runtime.max_deadline_seconds`` after the
    start; the scripted run never finishes on its own."""
    start = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    readings = iter([start, start + timedelta(seconds=1), start + timedelta(days=1)])
    adapter = ScriptedAdapter([ProgressEvent(text="working", fraction=0.1)], ends=False)
    with pytest.raises(ExtractionRunFailed) as raised:
        _provider(adapter, tokens, clock=lambda: next(readings)).extract(_batch())

    assert raised.value.kind == DEADLINE_EXCEEDED
    [handle] = adapter.handles
    assert handle.cancelled is True
    _served(adapter, popen)
    assert not _throwaway_root(adapter).exists()
    assert tokens.issued[0]["expires_at"] == start + timedelta(seconds=600)


# --- the credential binding and the adapter source ------------------------------------


@pytest.mark.parametrize("kind", ["login", "oauth_token"])
def test_a_credential_bound_to_another_account_refuses_before_any_start(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, popen: PopenGuard, kind: str
) -> None:
    monkeypatch.setenv(_KIND_ENV, kind)
    adapter = ScriptedAdapter(_answered())
    with pytest.raises(ExtractionCredentialNotOwned):
        _provider(adapter, tokens).extract(_batch(account_id=_OTHER_ACCOUNT))

    assert adapter.starts == []
    assert tokens.issued == []
    assert popen.calls == []
    assert not (workspace_dir_for(_WORKSPACE, Purpose.SCRATCH) / "extract").exists()


def test_an_oauth_token_without_an_account_id_refuses_every_speaker(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, popen: PopenGuard
) -> None:
    monkeypatch.setenv(_KIND_ENV, "oauth_token")
    monkeypatch.setenv(_ACCOUNT_ENV, "")
    adapter = ScriptedAdapter(_answered())
    with pytest.raises(ExtractionCredentialNotOwned):
        _provider(adapter, tokens).extract(_batch())

    assert adapter.starts == []
    assert tokens.issued == []


def test_an_oauth_token_serves_its_own_account(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, popen: PopenGuard
) -> None:
    monkeypatch.setenv(_KIND_ENV, "oauth_token")
    adapter = ScriptedAdapter(_answered())

    assert _provider(adapter, tokens).extract(_batch()) == _ANSWER
    _served(adapter, popen)


def test_an_api_key_credential_serves_any_account(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, popen: PopenGuard
) -> None:
    monkeypatch.setenv(_KIND_ENV, "api_key")
    adapter = ScriptedAdapter(_answered())

    assert _provider(adapter, tokens).extract(_batch(account_id=_OTHER_ACCOUNT))
    _served(adapter, popen)


def test_a_batch_without_a_scope_is_refused(tokens: _Tokens, popen: PopenGuard) -> None:
    adapter = ScriptedAdapter(_answered())
    with pytest.raises(ExtractionRunFailed):
        _provider(adapter, tokens).extract(digest([_TEXT]))

    assert adapter.starts == []
    assert tokens.issued == []
    assert popen.calls == []


def test_the_provider_never_reaches_a_real_process(
    monkeypatch: pytest.MonkeyPatch, popen: PopenGuard
) -> None:
    """Registered the production way, on a registry holding no adapter, and resolved
    through the evidence registry: the call fails ``adapter_unavailable`` before any
    settings read, token or directory, and nothing reaches ``Popen``."""
    monkeypatch.setenv("RHEO__automatic_memory__extraction__provider", PROVIDER_NAME)
    # Snapshotted, so teardown restores (or removes) whatever was there: the
    # registration below must not leak into the shared provider registry.
    monkeypatch.setitem(
        providers.providers(),
        PROVIDER_NAME,
        ClaudeCliExtractionProvider(AdapterRegistry()),
    )
    register_claude_cli_extraction(AdapterRegistry())
    provider = providers.resolve_provider()
    assert isinstance(provider, ClaudeCliExtractionProvider)

    with pytest.raises(ExtractionRunFailed) as raised:
        provider.extract(_batch())

    assert raised.value.kind == ADAPTER_UNAVAILABLE
    assert popen.calls == []
    assert not (workspace_dir_for(_WORKSPACE, Purpose.SCRATCH) / "extract").exists()


# --- the import boundary --------------------------------------------------------------


def _runtimes_imports(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.append(node.module)
        hits = [
            name
            for name in names
            if name == "rheo_runtimes" or name.startswith("rheo_runtimes.")
        ]
        if hits:
            found[str(path.relative_to(root))] = hits
    return found


def test_no_core_module_imports_rheo_runtimes() -> None:
    """Carried item (c): the provider reaches the CLI adapter only as what a
    composition root registered, never by importing it."""
    assert (_CORE_SRC / "rheo_core/runtime/extraction.py").is_file()
    assert _runtimes_imports(_CORE_SRC) == {}


def test_the_runtimes_import_scan_flags_a_real_import(tmp_path: Path) -> None:
    (tmp_path / "probe.py").write_text(
        "import rheo_runtimes\n"
        "def later():\n"
        "    from rheo_runtimes.claude_cli import ClaudeCliRuntime\n"
        "from rheo_runtimes_other import unrelated\n",
        encoding="utf-8",
    )
    assert _runtimes_imports(tmp_path) == {
        "probe.py": ["rheo_runtimes", "rheo_runtimes.claude_cli"]
    }
