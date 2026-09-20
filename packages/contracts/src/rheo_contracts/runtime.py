"""The version-one runtime adapter contract (FR 19, criterion 15).

Source of truth: ``docs/architecture/runtime-and-mcp.md`` § Runtime contract, version 1,
and the Data Models section of the 0c4 spec. Domain code imports these names from
``rheo_contracts``, never from ``rheo_core``.

**Declared only, deliberately: this module defines the types and the capability gate
and performs no I/O.** Constructing a ``RuntimeRequest``, spawning an adapter, and
recording transcripts are ``rheo_core.runtime``.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rheo_contracts.context import Actor, Audience
from rheo_contracts.purposes import ContextPurpose

_BOOL_CAPABILITY_NAMES = frozenset(
    {
        "tool_calling",
        "structured_output",
        "streaming",
        "continuation",
        "cancellation",
    }
)
ISOLATION_ENFORCED = "isolation=enforced"
"""Requirements-list token meaning the adapter must report ``isolation = enforced``."""


class ContextTier(StrEnum):
    """Redaction tier already applied to a context item's text."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class ContextItem(BaseModel):
    """One permission-checked, redacted item the adapter may read."""

    model_config = ConfigDict(frozen=True)

    ref: str
    tier: ContextTier
    text: str


class RuntimeLimits(BaseModel):
    """Deadline, iteration, and output caps for one run."""

    model_config = ConfigDict(frozen=True)

    DEFAULT_MAX_ITERATIONS: ClassVar[int] = 40
    DEFAULT_MAX_OUTPUT_BYTES: ClassVar[int] = 1_000_000

    deadline_seconds: int
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


class TextOutput(BaseModel):
    """The run should return plain text."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["text"] = "text"


class StructuredOutput(BaseModel):
    """The run should return JSON matching ``json_schema`` (Draft 2020-12)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["structured"] = "structured"
    json_schema: Mapping[str, JsonValue]


class StreamOutput(BaseModel):
    """The run should stream tokens; still ends on one terminal event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["stream"] = "stream"


RuntimeOutput = Annotated[
    TextOutput | StructuredOutput | StreamOutput,
    Field(discriminator="kind"),
]


class RuntimeRequest(BaseModel):
    """What the core hands an adapter for one run.

    Built only by ``rheo_core.runtime``. No secret value, secret reference, run-scoped
    token, or ``native_handle`` belongs here: those travel on :class:`AdapterSpawn`.
    ``continuation`` is a ``runtime_session.id`` or ``None``, never a CLI session id.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: UUID
    actor: Actor
    workspace_id: UUID
    audience: Audience
    purpose: ContextPurpose
    task: str
    context_items: list[ContextItem]
    permitted_tools: list[str]
    output: RuntimeOutput
    requirements: list[str]
    limits: RuntimeLimits
    continuation: UUID | None
    credential_slot: str
    runtime_id: str
    model_id: str


class UsageReporting(StrEnum):
    """How an adapter reports token usage."""

    EXACT = "exact"
    ESTIMATE = "estimate"
    NONE = "none"


class Isolation(StrEnum):
    """Whether the adapter can bound filesystem and network access."""

    ENFORCED = "enforced"
    ADVISORY = "advisory"


class RuntimeCapabilities(BaseModel):
    """What one adapter can do; compared to a request's ``requirements`` list."""

    model_config = ConfigDict(frozen=True)

    tool_calling: bool
    structured_output: bool
    streaming: bool
    continuation: bool
    cancellation: bool
    usage_reporting: UsageReporting
    isolation: Isolation


class UnmetCapability(Exception):
    """The adapter cannot satisfy this requirement; ``name`` is the first unmet one."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class FailureKind(StrEnum):
    """Closed set of named runtime failures (criterion 16 plus contract remainder)."""

    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    CREDENTIAL_INVALID = "credential_invalid"
    CREDENTIAL_NOT_OWNED = "credential_not_owned"
    TOOL_DENIED = "tool_denied"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STREAM_TRUNCATED = "stream_truncated"
    OUTPUT_INVALID = "output_invalid"
    ITERATION_LIMIT = "iteration_limit"
    RUNTIME_ERROR = "runtime_error"


class UsageKind(StrEnum):
    """Whether a usage event's counts are measured or estimated."""

    EXACT = "exact"
    ESTIMATE = "estimate"


class ToolOutcomeOk(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"


class ToolOutcomeRefused(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["refused"] = "refused"


class ToolOutcomeApprovalRequired(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["approval_required"] = "approval_required"
    approval_id: UUID


class ToolOutcomeError(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["error"] = "error"


ToolResultOutcome = Annotated[
    ToolOutcomeOk | ToolOutcomeRefused | ToolOutcomeApprovalRequired | ToolOutcomeError,
    Field(discriminator="status"),
]


class ProgressEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["progress"] = "progress"
    text: str
    fraction: float


class ToolCallEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tool_call"] = "tool_call"
    tool: str
    arguments_digest: str


class ToolResultEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tool_result"] = "tool_result"
    tool: str
    outcome: ToolResultOutcome


class UsageEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    cost: float
    kind: UsageKind


class FinalOutputEvent(BaseModel):
    """Terminal success. Exactly one of ``text`` or ``structured`` is set."""

    model_config = ConfigDict(frozen=True)

    type: Literal["final_output"] = "final_output"
    text: str | None = None
    structured: JsonValue | None = None

    @model_validator(mode="after")
    def _exactly_one_body(self) -> Self:
        has_text = "text" in self.model_fields_set
        has_structured = "structured" in self.model_fields_set
        if has_text == has_structured:
            raise ValueError("final_output requires exactly one of text or structured")
        return self


class FailureEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["failure"] = "failure"
    kind: FailureKind
    detail: str


class CancelledEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["cancelled"] = "cancelled"


class ApprovalRequiredEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["approval_required"] = "approval_required"
    approval_id: UUID


RuntimeEvent = Annotated[
    ProgressEvent
    | ToolCallEvent
    | ToolResultEvent
    | UsageEvent
    | FinalOutputEvent
    | FailureEvent
    | CancelledEvent
    | ApprovalRequiredEvent,
    Field(discriminator="type"),
]


class AdapterSpawn(BaseModel):
    """Per-run values core fills and never persists on :class:`RuntimeRequest`."""

    model_config = ConfigDict(frozen=True)

    mcp_url: str
    run_token: str
    work_dir: str
    config_dir: str
    native_handle: str | None


class RuntimeHandle(Protocol):
    """Live handle for one started run.

    ``native_handle`` is the CLI session id, or none.
    """

    native_handle: str | None

    def poll(self) -> RuntimeEvent | None: ...

    def finished(self) -> bool: ...

    def cancel(self) -> None: ...


class RuntimeAdapter(Protocol):
    """One runtime backend. Domain modules depend on this, not a vendor type."""

    def capabilities(self) -> RuntimeCapabilities: ...

    def start(
        self, request: RuntimeRequest, *, spawn: AdapterSpawn
    ) -> RuntimeHandle: ...


class RuntimeGate:
    """Refuse a run whose requirements the adapter cannot satisfy, before ``start``."""

    @staticmethod
    def check(requirements: Sequence[str], capabilities: RuntimeCapabilities) -> None:
        """Raise :class:`UnmetCapability` for the first unmet requirement.

        Boolean capabilities are required by name. The token that means isolation must
        be enforced is the literal string ``isolation=enforced``.
        """

        for requirement in requirements:
            if requirement == ISOLATION_ENFORCED:
                if capabilities.isolation == Isolation.ADVISORY:
                    raise UnmetCapability(name=ISOLATION_ENFORCED)
                continue
            if requirement not in _BOOL_CAPABILITY_NAMES:
                raise UnmetCapability(name=requirement)
            if not getattr(capabilities, requirement):
                raise UnmetCapability(name=requirement)
