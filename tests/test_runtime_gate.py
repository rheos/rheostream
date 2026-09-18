"""Capability-gate seam: ``RuntimeGate.check`` refuses before ``adapter.start``.

No postgres. The production sequence this pins is check-then-start: a recording
double must not be started when the gate raises. Bool requirements are named as
themselves; isolation uses the literal token ``isolation=enforced``.
"""

from uuid import UUID

import pytest
from rheo_contracts import (
    Actor,
    ActorKind,
    AdapterSpawn,
    Audience,
    AudienceKind,
    ContextPurpose,
    Isolation,
    RuntimeCapabilities,
    RuntimeGate,
    RuntimeLimits,
    RuntimeRequest,
    TextOutput,
    UnmetCapability,
    UsageReporting,
)

_OPERATION_ID = UUID("018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b")
_WORKSPACE_ID = UUID("018f6b2f-0000-7000-8000-000000000001")
_ACTOR_ID = UUID("018f6b2f-0000-7000-8000-000000000002")
_AUDIENCE_ID = UUID("018f6b2f-0000-7000-8000-000000000003")


def _capabilities(**overrides: object) -> RuntimeCapabilities:
    values: dict[str, object] = {
        "tool_calling": True,
        "structured_output": True,
        "streaming": True,
        "continuation": True,
        "cancellation": True,
        "usage_reporting": UsageReporting.EXACT,
        "isolation": Isolation.ENFORCED,
    }
    values.update(overrides)
    return RuntimeCapabilities.model_validate(values)


def _request(requirements: list[str]) -> RuntimeRequest:
    return RuntimeRequest(
        operation_id=_OPERATION_ID,
        actor=Actor(kind=ActorKind.ACCOUNT, id=_ACTOR_ID),
        workspace_id=_WORKSPACE_ID,
        audience=Audience(kind=AudienceKind.JOB, id=_AUDIENCE_ID),
        purpose=ContextPurpose.RESPOND,
        task="draft a reply",
        context_items=[],
        permitted_tools=[],
        output=TextOutput(),
        requirements=requirements,
        limits=RuntimeLimits(deadline_seconds=60),
        continuation=None,
        credential_slot="model",
        runtime_id="claude_cli",
        model_id="sonnet",
    )


_SPAWN = AdapterSpawn(
    mcp_url="http://127.0.0.1:8080/mcp",
    run_token="rheo_runtime_test_token",
    work_dir="/tmp/rheo-run",
    config_dir="/tmp/rheo-config",
    native_handle=None,
)


class _RecordingHandle:
    native_handle: str | None = None

    def poll(self) -> None:
        return None

    def finished(self) -> bool:
        return True

    def cancel(self) -> None:
        return None


class _RecordingAdapter:
    def __init__(self, capabilities: RuntimeCapabilities) -> None:
        self._capabilities = capabilities
        self.start_calls = 0

    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def start(
        self, request: RuntimeRequest, *, spawn: AdapterSpawn
    ) -> _RecordingHandle:
        self.start_calls += 1
        return _RecordingHandle()


def _check_then_start(adapter: _RecordingAdapter, requirements: list[str]) -> None:
    RuntimeGate.check(requirements, adapter.capabilities())
    adapter.start(_request(requirements), spawn=_SPAWN)


@pytest.mark.parametrize(
    "capability", ["structured_output", "streaming", "continuation"]
)
def test_gate_refuses_unmet_bool_capability_without_start(
    capability: str,
) -> None:
    adapter = _RecordingAdapter(_capabilities(**{capability: False}))
    with pytest.raises(UnmetCapability) as caught:
        _check_then_start(adapter, [capability])
    assert caught.value.name == capability
    assert adapter.start_calls == 0


def test_gate_refuses_isolation_enforced_against_advisory_without_start() -> None:
    adapter = _RecordingAdapter(_capabilities(isolation=Isolation.ADVISORY))
    with pytest.raises(UnmetCapability) as caught:
        _check_then_start(adapter, ["isolation=enforced"])
    assert caught.value.name == "isolation=enforced"
    assert adapter.start_calls == 0
