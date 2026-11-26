"""The runtime model client (L10): the guard before the runner, the stage's model key,
runner failures as ``ModelUnavailable``, an invalid reply discarded, the heartbeat
forwarded, and no client without a runner or an account. No database, no runner: a
fake records each call.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel
from rheo_contracts import ContextPurpose
from rheo_core.runtime.oneshot import (
    OneShotCredentialNotOwned,
    OneShotDeferred,
    OneShotFailed,
)

from rheo_finance.model.client import ModelReplyInvalid, ModelUnavailable
from rheo_finance.model.guard import PromptRefused
from rheo_finance.wiring.model import RuntimeModelClient, runtime_model_client

WORKSPACE = uuid.UUID(int=11)
ACCOUNT = uuid.UUID(int=12)


class Label(BaseModel):
    label: str


class FakeRunner:
    def __init__(self, reply: object = None, error: BaseException | None = None):
        self.calls: list[dict[str, Any]] = []
        self._reply = {"label": "groceries"} if reply is None else reply
        self._error = error

    def run(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        heartbeat = kwargs.get("heartbeat")
        if heartbeat is not None:
            heartbeat()
        if self._error is not None:
            raise self._error
        return self._reply


def client(runner: FakeRunner, beats: list[int]) -> RuntimeModelClient:
    return RuntimeModelClient(
        runner=runner,
        workspace_id=WORKSPACE,
        account_id=ACCOUNT,
        heartbeat=lambda: beats.append(1),
    )


@pytest.mark.parametrize("stage", ["classify", "suggest", "extract"])
def test_a_call_reaches_the_runner_with_its_stage_key(stage: str) -> None:
    runner, beats = FakeRunner(), []
    out = client(runner, beats).call(stage, "Classify.", "sample grocer", Label)  # type: ignore[arg-type]
    assert out == Label(label="groceries")
    (call,) = runner.calls
    assert call["model_id_key"] == f"finance.model.{stage}_model_id"
    assert call["workspace_id"] == WORKSPACE and call["account_id"] == ACCOUNT
    assert call["purpose"] is ContextPurpose.INTERNAL_ANALYSIS
    assert call["task"] == "Classify.\n\nsample grocer"
    assert call["json_schema"] == Label.model_json_schema()
    assert call["scratch_subdir"] == "finance"
    assert beats == [1]


def test_a_refused_prompt_never_reaches_the_runner() -> None:
    runner = FakeRunner()
    with pytest.raises(PromptRefused):
        client(runner, []).call("classify", "Classify.", "receipt GIF89a", Label)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("error", "word"),
    [
        (OneShotDeferred("usage_limited"), "model_deferred"),
        (OneShotFailed("adapter_unavailable"), "model_failed"),
        (OneShotCredentialNotOwned("synthetic"), "model_account_refused"),
    ],
)
def test_runner_failures_are_model_unavailable(error: BaseException, word: str) -> None:
    with pytest.raises(ModelUnavailable) as caught:
        client(FakeRunner(error=error), []).call("extract", "S.", "t", Label)
    assert str(caught.value) == word
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_an_invalid_reply_is_discarded() -> None:
    runner = FakeRunner(reply={"other": "synthetic reply text"})
    with pytest.raises(ModelReplyInvalid) as caught:
        client(runner, []).call("classify", "S.", "t", Label)
    assert "synthetic reply text" not in str(caught.value)


class _Ctx:
    workspace_id = WORKSPACE


def _built(runner: FakeRunner | None, account: str) -> RuntimeModelClient | None:
    source: Callable[[], FakeRunner | None] = lambda: runner  # noqa: E731
    return runtime_model_client(
        _Ctx(),  # type: ignore[arg-type]
        heartbeat=lambda: None,
        runner_source=source,
        account_for=lambda _workspace: account,
    )


def test_no_runner_or_no_account_gives_no_client() -> None:
    assert _built(None, str(ACCOUNT)) is None
    assert _built(FakeRunner(), "") is None
    assert _built(FakeRunner(), "not-a-uuid") is None
    built = _built(FakeRunner(), f" {ACCOUNT} ")
    assert isinstance(built, RuntimeModelClient)
