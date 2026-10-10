"""Bounded pipeline commands; presets describe data and never executable policy."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from rheo_contracts import ContextPurpose, RecordRef
from rheo_core.redaction.tiers import SensitivityTier, Tiered

# Explicit aliases keep audit subjects typed through the tool adapter.
OpportunityRef = Annotated[
    RecordRef,
    BeforeValidator(lambda v: _parse(v, "opportunity"), json_schema_input_type=str),
    PlainSerializer(lambda r: r.format(), return_type=str),
]
ObservationRef = Annotated[
    RecordRef,
    BeforeValidator(lambda v: _parse(v, "observation"), json_schema_input_type=str),
    PlainSerializer(lambda r: r.format(), return_type=str),
]
PartyRef = Annotated[
    RecordRef,
    BeforeValidator(
        lambda v: _parse(v, "party", "relationships"), json_schema_input_type=str
    ),
    PlainSerializer(lambda r: r.format(), return_type=str),
]


def _parse(v: object, kind: str, module: str = "leads") -> RecordRef:
    r = RecordRef.parse(v) if isinstance(v, str) else v
    if not isinstance(r, RecordRef) or r.module != module or r.record_type != kind:
        raise ValueError("unexpected reference")
    return r


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Empty(Strict):
    pass


class RefInput(Strict):
    ref: OpportunityRef


class RevisionInput(RefInput):
    revision: int = Field(ge=1)


def _calendar_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("use a calendar date in YYYY-MM-DD format")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("use a calendar date in YYYY-MM-DD format")
    return parsed


CalendarDate = Annotated[date, BeforeValidator(_calendar_date)]


class ListInput(Strict):
    pipeline_id: UUID | None = None
    query: str = Field(default="", max_length=512)
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)
    followup_due_by: CalendarDate | None = None


class ObservationInput(Strict):
    observation_ref: ObservationRef


class CreateInput(ObservationInput):
    pipeline_id: UUID


class AttachInput(RevisionInput, ObservationInput):
    pass


class UpdateInput(RevisionInput):
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    owner_id: UUID | None = None
    value_amount: Decimal | None = Field(default=None, allow_inf_nan=False)
    value_currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    value_basis: (
        Literal["project_fee", "annual_contract", "hourly_rate", "referral_fee"] | None
    ) = None
    fields: dict[str, str | None] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def value_group(self) -> "UpdateInput":
        if any(
            v is not None
            for v in (self.value_amount, self.value_currency, self.value_basis)
        ) and not all(
            v is not None
            for v in (self.value_amount, self.value_currency, self.value_basis)
        ):
            raise ValueError("amount, currency and basis are required together")
        return self


class TransitionInput(RevisionInput):
    to_stage_id: str = Field(pattern="^[a-z][a-z0-9_]{0,63}$")
    note: str | None = Field(default=None, max_length=16000)


class NoteInput(RevisionInput):
    body: str = Field(min_length=1, max_length=16000)


class PartyInput(RevisionInput):
    party_ref: PartyRef
    role: Literal["contact", "organization", "referrer"] = "contact"


class PipelineCreate(Strict):
    name: str = Field(min_length=1, max_length=512)
    preset: Literal["inbound_services", "job_search"] = "inbound_services"


class Stage(Strict):
    stage_id: str = Field(pattern="^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=512)
    kind: Literal["open", "terminal"] = "open"
    outcome: Literal["succeeded", "lost", "disqualified"] | None = None

    @model_validator(mode="after")
    def outcome_required(self) -> "Stage":
        if (self.kind == "terminal") != (self.outcome is not None):
            raise ValueError("terminal stages require an outcome")
        return self


class ExtensionField(Strict):
    target: str = Field(pattern=r"^ext\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    type: Literal["text", "integer", "decimal", "date", "boolean", "enum"]
    required: bool = False
    sensitivity: Literal["public", "internal", "restricted"] = "restricted"
    enum_values: list[str] = Field(default_factory=list, max_length=100)


class PresetEdit(Strict):
    preset_id: UUID
    from_version: int = Field(ge=1)
    stages: list[Stage] = Field(min_length=1, max_length=50)
    transitions: list[tuple[str, str]] = Field(max_length=2500)
    requirements: dict[str, list[str]] = Field(default_factory=dict, max_length=50)
    fields: list[ExtensionField] = Field(default_factory=list, max_length=100)
    note: str = Field(default="", max_length=2000)


class MigrateInput(RevisionInput):
    to_version: int = Field(ge=1)
    stage_map: dict[str, str] = Field(max_length=50)


class Condition(Strict):
    field: str = Field(min_length=1, max_length=128)
    op: Literal["present", "absent", "equals", "contains"]
    value: str | None = Field(default=None, max_length=2000)


class Rule(Strict):
    action: Literal[
        "record_only",
        "create_opportunity",
        "attach_to_open_opportunity",
        "attach_by_field",
    ]
    pipeline_id: UUID | None = None
    enabled: bool = True
    conditions: list[Condition] = Field(default_factory=list, max_length=30)
    # attach_by_field only: the fact or ext.* target whose value identifies the
    # external thing (a posting id), compared with the opportunity's current value.
    match_field: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def destination(self) -> "Rule":
        if self.action != "record_only" and self.pipeline_id is None:
            raise ValueError("routing action requires a pipeline")
        if (self.action == "attach_by_field") != (self.match_field is not None):
            raise ValueError(
                "match_field is required for, and only for, attach_by_field"
            )
        return self


class RoutingInput(Strict):
    connection_id: UUID
    expected_rule_ids: list[UUID] | None = Field(default=None, max_length=100)
    rules: list[Rule] = Field(max_length=100)


Rating = Literal["high", "medium", "low", "needs_information"]


class Assessment(Strict):
    """An explicit caller judgment, never inferred from inbound instructions."""

    fit: Rating
    intent: Rating
    urgency: Rating
    evidence_completeness: Rating
    uncertainty: Literal["low", "medium", "high"]
    explanation: str = Field(min_length=1, max_length=16000)
    author: Literal["human", "model"]
    model_id: str | None = Field(default=None, min_length=1, max_length=256)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def provenance(self) -> "Assessment":
        if not self.explanation.strip():
            raise ValueError("assessment explanation is required")
        if self.author == "model":
            if not (self.model_id and self.model_id.strip()) or not (
                self.prompt_version and self.prompt_version.strip()
            ):
                raise ValueError(
                    "model assessment requires model_id and prompt_version"
                )
        elif self.model_id is not None or self.prompt_version is not None:
            raise ValueError("human assessment cannot claim model provenance")
        return self


class AssessInput(RevisionInput):
    objective: str = Field(min_length=1, max_length=2000)
    assessment: Assessment | None = None
    # Omission retains the deterministic completeness check; no model is invoked.


class HandoffInput(RevisionInput):
    purpose: ContextPurpose
    destination_ref: str | None = Field(default=None, max_length=512)
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=256)


class HandoffGet(Strict):
    handoff_id: UUID


class DraftInput(RevisionInput):
    body: str = Field(min_length=1, max_length=16000)


class FollowupSchedule(RevisionInput):
    action: str = Field(min_length=1, max_length=2000)
    due_on: CalendarDate

    @model_validator(mode="after")
    def meaningful_action(self) -> "FollowupSchedule":
        if not self.action.strip():
            raise ValueError("follow-up action is required")
        return self


class FollowupResolve(RevisionInput):
    reminder_id: UUID
    outcome: Literal["completed", "cancelled"]


class PermissionCheck(Strict):
    party_ref: PartyRef
    purpose: ContextPurpose
    channel: Literal["email", "phone", "message"] | None = None


class PermissionRecord(PermissionCheck):
    recipient_scope: Literal["workspace", "named_referral"] = "workspace"
    disclosure_version: str = Field(min_length=1, max_length=256)
    evidence_observation_ref: ObservationRef | None = None


class WithdrawInput(PermissionCheck):
    reason: str | None = Field(default=None, max_length=2000)


class SuppressInput(Strict):
    party_ref: PartyRef
    reason: str | None = Field(default=None, max_length=2000)


class PermissionOutput(Strict):
    state: Literal["permitted", "absent", "withdrawn", "suppressed"]


class RecordOutput(Strict):
    ref: str
    revision: int = 1
    data: Annotated[dict[str, Any], Tiered(SensitivityTier.RESTRICTED)]


class ItemsOutput(Strict):
    items: Annotated[list[dict[str, Any]], Tiered(SensitivityTier.RESTRICTED)]


class ReferenceEvent(Strict):
    ref: str
    related_ref: str | None = None
    revision: int = 1


def _deletable(v: object) -> RecordRef:
    reference = RecordRef.parse(v) if isinstance(v, str) else v
    if (
        not isinstance(reference, RecordRef)
        or reference.module != "leads"
        or reference.record_type not in {"observation", "opportunity"}
    ):
        raise ValueError("expected an observation or opportunity reference")
    return reference


class DeleteInput(Strict):
    ref: Annotated[
        RecordRef,
        BeforeValidator(_deletable, json_schema_input_type=str),
        PlainSerializer(lambda r: r.format(), return_type=str),
    ]
