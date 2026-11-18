"""Tool-free typed judgments. No session, tool authority, or credential in a request.

Separate from the conversational runtime protocol: a provider evaluates named
questions over supplied state and returns numbers, never generated prose.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class JudgmentQuestion:
    kind: Literal["noul", "score"]
    instructions: str
    levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgmentRequest:
    model: str
    state: Mapping[str, object]
    questions: Mapping[str, JudgmentQuestion]


@dataclass(frozen=True)
class JudgmentResult:
    values: Mapping[str, float]
    model: str
    input_tokens: int
    output_tokens: int
    confidence: Mapping[str, float]


class JudgmentFailed(Exception):
    """Only a closed failure vocabulary may cross the provider boundary."""

    def __init__(self, kind: str) -> None:
        self.kind = (
            kind
            if kind
            in {
                "disabled",
                "adapter_unavailable",
                "credential_invalid",
                "input_too_large",
                "input_invalid",
                "output_invalid",
                "rate_limited",
                "provider_unavailable",
                "deadline_exceeded",
            }
            else "provider_unavailable"
        )
        super().__init__(self.kind)


class JudgmentProvider(Protocol):
    async def evaluate(self, request: JudgmentRequest) -> JudgmentResult: ...
