"""The production model client: ``GuardedModelClient`` over core's one-shot runner.

The guard and the reply's validation stay in ``GuardedModelClient.call``, so a refused
prompt never reaches the runner, and building the runtime request stays in core. Each
stage's model is the core setting ``finance.model.<stage>_model_id`` (empty: the CLI's
default); the run's account is ``finance.model.account_id``.

A runner failure becomes ``ModelUnavailable`` with a fixed word and nothing chained,
and the ledger keeps working on what is already classified.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol
from uuid import UUID

from rheo_contracts import ContextPurpose, WorkspaceContext
from rheo_core.runtime.oneshot import (
    OneShotCredentialNotOwned,
    OneShotDeferred,
    OneShotFailed,
    oneshot_runner,
    workspace_settings,
)

from rheo_finance.model.client import (
    GuardedModelClient,
    JsonSchema,
    ModelUnavailable,
    Stage,
)
from rheo_finance.model.guard import PromptGuard

ACCOUNT_KEY: Final = "finance.model.account_id"
SCRATCH_SUBDIR: Final = "finance"


def model_id_key(stage: Stage) -> str:
    return f"finance.model.{stage}_model_id"


class Runner(Protocol):
    """What the client calls: core's ``OneShotRunner.run``."""

    def run(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        purpose: ContextPurpose,
        task: str,
        json_schema: JsonSchema,
        model_id_key: str,
        scratch_subdir: str,
        heartbeat: Callable[[], None] | None = None,
    ) -> object: ...


class RuntimeModelClient(GuardedModelClient):
    def __init__(
        self,
        *,
        runner: Runner,
        workspace_id: UUID,
        account_id: UUID,
        heartbeat: Callable[[], None],
        guard: PromptGuard | None = None,
    ) -> None:
        super().__init__(guard if guard is not None else PromptGuard())
        self._runner = runner
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._heartbeat = heartbeat

    def _send(
        self, stage: Stage, system: str, text: str, json_schema: JsonSchema
    ) -> object:
        try:
            return self._runner.run(
                workspace_id=self._workspace_id,
                account_id=self._account_id,
                purpose=ContextPurpose.INTERNAL_ANALYSIS,
                task=system + "\n\n" + text,
                json_schema=json_schema,
                model_id_key=model_id_key(stage),
                scratch_subdir=SCRATCH_SUBDIR,
                heartbeat=self._heartbeat,
            )
        except OneShotDeferred:
            raise ModelUnavailable("model_deferred") from None
        except OneShotFailed:
            raise ModelUnavailable("model_failed") from None
        except OneShotCredentialNotOwned:
            raise ModelUnavailable("model_account_refused") from None


def _account_setting(workspace_id: UUID) -> str:
    return workspace_settings(workspace_id).get_str(ACCOUNT_KEY)


def runtime_model_client(
    core_ctx: WorkspaceContext,
    *,
    heartbeat: Callable[[], None],
    runner_source: Callable[[], Runner | None] = oneshot_runner,
    account_for: Callable[[UUID], str] = _account_setting,
) -> RuntimeModelClient | None:
    """A client when this process registered runtime adapters (the worker does; a
    ``finance`` CLI process does not) and the workspace names a model account; else
    ``None``, and model steps skip."""
    runner = runner_source()
    if runner is None:
        return None
    try:
        account_id = UUID(account_for(core_ctx.workspace_id).strip())
    except ValueError:
        return None
    return RuntimeModelClient(
        runner=runner,
        workspace_id=core_ctx.workspace_id,
        account_id=account_id,
        heartbeat=heartbeat,
    )
