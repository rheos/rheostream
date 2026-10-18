"""One tool-less ``claude_cli`` run that answers with a structured result.

The machinery the extraction provider was built on, shared so a module job can ask a
model one question without a module constructing a ``RuntimeRequest`` itself (the
runtime contract keeps that to this package). :class:`OneShotRunner` takes the task
text and the response schema, and returns the run's structured mapping unvalidated:
the caller owns the schema and validates what comes back.

**The tool guard is the extraction provider's, unchanged.** No built-in tools
(``disable_builtin_tools``), an empty-snapshot run token, nothing pre-approved, one
turn, and a throwaway directory removed in a ``finally`` whatever happens. The CLI
writes its session transcript (the task text) under its configuration directory, so
that directory never outlives the call.

**The adapter comes only from the injected registry.** The worker registers its
``ADAPTERS`` here with :func:`register_oneshot_adapters`; a process that registers
none gets ``None`` from :func:`oneshot_runner`, and a registry without ``claude_cli``
fails ``adapter_unavailable`` before anything is minted or written.

**Every failure raises, carrying a fixed word.** :class:`OneShotDeferred` when the
account's usage limit refused the run (nothing about the task is wrong, so a caller
waits without counting an attempt), :class:`OneShotFailed` for every other outcome,
and :class:`OneShotCredentialNotOwned` when the login belongs to another account. No
exception carries task text, model output or an adapter's detail string.
"""

import os
import shutil
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from rheo_contracts import (
    Actor,
    ActorKind,
    AdapterSpawn,
    ApprovalRequiredEvent,
    Audience,
    AudienceKind,
    CancelledEvent,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    RuntimeHandle,
    RuntimeLimits,
    RuntimeRequest,
    StructuredOutput,
    UsageEvent,
)
from rheo_contracts.purposes import ContextPurpose

from rheo_core.refs import uuid7
from rheo_core.routing.config import MCP, RoutingConfig
from rheo_core.routing.url_for import url_for
from rheo_core.runtime.operations import (
    ACCOUNT_BOUND_CREDENTIAL_KINDS,
    CREDENTIAL_SLOT,
    HARD_DEADLINE_SECONDS,
    build_runtime_request,
)
from rheo_core.runtime.registry import AdapterRegistry
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.control_plane import delete_access_token
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.issue import issue_runtime_token

RUNTIME_ID: Final = "claude_cli"
THROWAWAY_DIR_MODE: Final = 0o700

ADAPTER_UNAVAILABLE: Final = "adapter_unavailable"
DEADLINE_EXCEEDED: Final = "deadline_exceeded"
STREAM_TRUNCATED: Final = "stream_truncated"
NO_STRUCTURED_RESULT: Final = "no_structured_result"
RUN_CANCELLED: Final = "cancelled"
APPROVAL_REQUESTED: Final = "approval_required"
TOKEN_REVOKE_FAILED: Final = "token_revoke_failed"


class OneShotFailed(Exception):
    """The run produced no usable structured result; ``kind`` says how, in a word."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class OneShotDeferred(Exception):
    """The account cannot run now (its usage limit); ``kind`` is the failure kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class OneShotCredentialNotOwned(Exception):
    """The login credential is bound to another account than the run's."""


def workspace_settings(workspace_id: UUID) -> ResolvedSettings:
    """The workspace's resolved settings, read the way the runtime job reads them."""
    return resolve(workspace_id=workspace_id, source=PostgresOverrideSource())


def revoke_run_token(token_id: UUID) -> None:
    with get_backend().control_engine.begin() as connection:
        delete_access_token(connection, token_id)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _remove_tree(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


class OneShotRunner:
    """One tool-less run of the registry's ``claude_cli`` adapter in a throwaway
    directory, its structured result returned unvalidated.

    ``settings_for``, ``issue_token``, ``revoke_token`` and ``clock`` default to the
    production readers; a test replaces them to run without a cluster or to move the
    clock past the deadline.
    """

    def __init__(
        self,
        adapters: AdapterRegistry,
        *,
        settings_for: Callable[[UUID], ResolvedSettings] = workspace_settings,
        issue_token: Callable[..., tuple[UUID, str]] = issue_runtime_token,
        revoke_token: Callable[[UUID], None] = revoke_run_token,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._adapters = adapters
        self._settings_for = settings_for
        self._issue_token = issue_token
        self._revoke_token = revoke_token
        self._clock = clock

    def run(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        purpose: ContextPurpose,
        task: str,
        json_schema: dict[str, Any],
        model_id_key: str,
        scratch_subdir: str,
        heartbeat: Callable[[], None] | None = None,
        on_usage: Callable[[int, int], None] | None = None,
    ) -> Mapping[str, object]:
        """Run ``task`` once as ``account_id`` and return the structured result.

        ``model_id_key`` names the setting that picks the model (empty leaves the
        CLI's default). ``scratch_subdir`` names the throwaway directory's parent
        under the workspace's scratch area. ``heartbeat``, when given, is called
        while the run is awaited, so a job handler can keep its lease.
        ``on_usage``, when given, receives the run's input and output token counts
        if the adapter reports them; a run that fails can still report them.
        """
        # Only ever the registered adapter, and first: a process whose registry
        # holds none reads no settings, mints no token and makes no directory.
        adapter = self._adapters.lookup(RUNTIME_ID)
        if adapter is None:
            raise OneShotFailed(ADAPTER_UNAVAILABLE)
        settings = self._settings_for(workspace_id)
        # The runtime's own single-account-login rule: a login credential, and an
        # ``oauth_token`` minted from that subscription, serve only their account.
        if (
            settings.get_str("runtime.claude_cli.credential_kind")
            in ACCOUNT_BOUND_CREDENTIAL_KINDS
        ):
            wanted = settings.get_str("runtime.claude_cli.credential_account_id")
            if str(account_id) != wanted:
                raise OneShotCredentialNotOwned(
                    "the run's account does not own the login"
                )

        deadline = min(
            settings.get_int("runtime.max_deadline_seconds"), HARD_DEADLINE_SECONDS
        )
        model_override = settings.get_str(model_id_key).strip() or None
        request = self._request(
            workspace_id=workspace_id,
            purpose=purpose,
            task=task,
            json_schema=json_schema,
            deadline=deadline,
            model_id=model_override,
        )

        started_at = self._clock()
        deadline_at = started_at + timedelta(seconds=deadline)
        # Per call, never the workspace's shared ``claude-cli`` directory: the CLI
        # writes its session transcript (the task text) under its configuration
        # directory, and the shared one is kept for the transcript retention window.
        root = (
            workspace_dir_for(workspace_id, Purpose.SCRATCH)
            / scratch_subdir
            / str(uuid7())
        )
        token_id: UUID | None = None
        handle: RuntimeHandle | None = None
        try:
            token_id, run_token = self._issue_token(
                account_id=account_id,
                workspace_id=workspace_id,
                purpose=purpose.value,
                expires_at=deadline_at,
                operations=[],
            )
            root.mkdir(mode=THROWAWAY_DIR_MODE, parents=True)
            os.chmod(root, THROWAWAY_DIR_MODE)
            work_dir = root / "work"
            work_dir.mkdir(mode=THROWAWAY_DIR_MODE)
            # Not created: the adapter's ``start`` prepares it, copying the login seed
            # only into a configuration directory that does not exist yet.
            config_dir = root / "config"
            spawn = AdapterSpawn(
                mcp_url=url_for(RoutingConfig.from_settings(resolve()), MCP, "/"),
                run_token=run_token,
                work_dir=str(work_dir),
                config_dir=str(config_dir),
                native_handle=None,
                model_override=model_override,
                disable_builtin_tools=True,
            )
            handle = adapter.start(request, spawn=spawn)
            return self._await_result(
                handle, deadline_at=deadline_at, heartbeat=heartbeat, on_usage=on_usage
            )
        finally:
            revoke_failed = False
            try:
                if handle is not None and not handle.finished():
                    handle.cancel()
                if token_id is not None:
                    try:
                        self._revoke_token(token_id)
                    except Exception:
                        revoke_failed = True
            finally:
                # Its own ``finally``: whatever the cancel or the revoke does, the
                # throwaway directory goes. The CLI's transcript under ``config/`` is
                # the task text, and nothing else ever sweeps this directory.
                _remove_tree(root)
            if revoke_failed:
                # Raised outside the ``except`` block, so the revoke error (whose text
                # may quote the database's own detail) is not on ``__context__``.
                raise OneShotFailed(TOKEN_REVOKE_FAILED)

    def _request(
        self,
        *,
        workspace_id: UUID,
        purpose: ContextPurpose,
        task: str,
        json_schema: dict[str, Any],
        deadline: int,
        model_id: str | None,
    ) -> RuntimeRequest:
        return build_runtime_request(
            operation_id=uuid7(),
            # A job, not a person, starts this run; the account is on the run token,
            # and the output is read only by the caller's validator.
            actor=Actor(kind=ActorKind.SYSTEM, id=None),
            workspace_id=workspace_id,
            audience=Audience(kind=AudienceKind.JOB, id=None),
            purpose=purpose,
            task=task,
            context_items=[],
            permitted_tools=[],
            output=StructuredOutput(json_schema=json_schema),
            requirements=[],
            limits=RuntimeLimits(
                deadline_seconds=deadline,
                max_iterations=1,
                max_output_bytes=RuntimeLimits.DEFAULT_MAX_OUTPUT_BYTES,
            ),
            continuation=None,
            credential_slot=CREDENTIAL_SLOT,
            runtime_id=RUNTIME_ID,
            model_id=model_id or "",
        )

    def _await_result(
        self,
        handle: RuntimeHandle,
        *,
        deadline_at: datetime,
        heartbeat: Callable[[], None] | None,
        on_usage: Callable[[int, int], None] | None = None,
    ) -> Mapping[str, object]:
        """Poll to a terminal event; the structured mapping, or a raise."""
        while True:
            if heartbeat is not None:
                heartbeat()
            if self._clock() >= deadline_at and not handle.finished():
                handle.cancel()
                raise OneShotFailed(DEADLINE_EXCEEDED)
            event = handle.poll()
            if event is None:
                if handle.finished():
                    raise OneShotFailed(STREAM_TRUNCATED)
                continue
            if isinstance(event, FinalOutputEvent):
                structured = event.structured
                if not isinstance(structured, dict):
                    raise OneShotFailed(NO_STRUCTURED_RESULT)
                return structured
            if isinstance(event, FailureEvent):
                if event.kind is FailureKind.USAGE_LIMITED:
                    # Not the task's fault: the caller waits without counting it.
                    raise OneShotDeferred(event.kind.value)
                raise OneShotFailed(event.kind.value)
            if isinstance(event, CancelledEvent):
                raise OneShotFailed(RUN_CANCELLED)
            if isinstance(event, ApprovalRequiredEvent):
                # Nothing is permitted, so no approval request can be legitimate.
                raise OneShotFailed(APPROVAL_REQUESTED)
            if isinstance(event, UsageEvent):
                # Counts only: the cost figure is not passed on.
                if on_usage is not None:
                    on_usage(event.input_tokens, event.output_tokens)
                continue
            # Progress, tool-call and tool-result events carry no result.


_registered: OneShotRunner | None = None
_lock: Final = threading.Lock()


def register_oneshot_adapters(adapters: AdapterRegistry) -> None:
    """Make :func:`oneshot_runner` answer a runner over ``adapters`` in this process."""
    global _registered
    with _lock:
        _registered = OneShotRunner(adapters)


def oneshot_runner() -> OneShotRunner | None:
    """The process's runner, or ``None`` when this process registered no adapters."""
    with _lock:
        return _registered


def set_oneshot_runner(runner: OneShotRunner | None) -> None:
    """Install ``runner`` (or clear it). For tests and composition roots only."""
    global _registered
    with _lock:
        _registered = runner
