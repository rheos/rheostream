"""Bounded pipeline commands; presets describe data and never executable policy."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PrivateAttr,
    ValidationInfo,
    model_validator,
)
from rheo_contracts import ContextPurpose, RecordRef
from rheo_core.redaction.policy import TierPolicy
from rheo_core.redaction.tiers import SensitivityTier, Tiered
from rheo_core.refs.short import ref_value

# Explicit aliases keep audit subjects typed through the tool adapter. An opportunity
# also takes its short ID (#345), typed or bare, resolved while dispatch validates.
OpportunityRef = Annotated[
    RecordRef,
    BeforeValidator(
        lambda v, info: _parse(v, info, "opportunity"), json_schema_input_type=str
    ),
    PlainSerializer(lambda r: r.format(), return_type=str),
    Field(
        description="The opportunity's short ID (8 or more hex characters, as the "
        "UI shows it) or its full reference, leads.opportunity:<uuid>."
    ),
]
HandoffRef = Annotated[
    RecordRef,
    BeforeValidator(
        lambda v, info: _parse(v, info, "handoff"), json_schema_input_type=str
    ),
    PlainSerializer(lambda r: r.format(), return_type=str),
]
ObservationRef = Annotated[
    RecordRef,
    BeforeValidator(
        lambda v, info: _parse(v, info, "observation"), json_schema_input_type=str
    ),
    PlainSerializer(lambda r: r.format(), return_type=str),
]
PartyRef = Annotated[
    RecordRef,
    BeforeValidator(
        lambda v, info: _parse(v, info, "party", "relationships"),
        json_schema_input_type=str,
    ),
    PlainSerializer(lambda r: r.format(), return_type=str),
]


def _parse(
    v: object, info: ValidationInfo, kind: str, module: str = "leads"
) -> RecordRef:
    r = ref_value(v, info, module, kind)
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
    """One person's request to hand an opportunity on.

    ``destination_ref`` names Current or nothing. With Current, ``payload`` is the
    item's title, body, priority, area and due date, and core holds the request for
    a person's approval; with nothing, the handoff is recorded locally only.
    """

    purpose: ContextPurpose
    destination_ref: Literal["current"] | None = None
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=256)


class HandoffGet(Strict):
    handoff_id: UUID


class HandoffRestage(Strict):
    """Stage a permitted handoff again under a new approval.

    ``attempt_count`` is the count the caller last read: a match stages the next
    attempt, one higher answers a restage that already happened, anything else is
    stale."""

    handoff_id: UUID
    attempt_count: int = Field(ge=0)


class HandoffList(RefInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


HandoffState = Literal[
    "unavailable",
    "pending",
    "succeeded",
    "failed",
    "refused",
    "cancelled",
    "unresolved",
]
ProjectState = Literal["never_assigned", "linked", "cleared", "deleted"]


class HandoffSummary(Strict):
    """One handoff as its history shows it: identities, outcome and the minimal
    Current project projection. Never the request body or its snapshot."""

    ref: str
    opportunity_ref: str
    purpose: str
    destination_ref: str | None
    state: HandoffState
    result_ref: str | None
    result_detail: str | None
    approval_id: UUID | None
    external_action_id: UUID | None
    attempt_count: int
    qualification_ref: str | None
    qualification_stale: bool | None
    current_project_ref: str | None
    last_current_project_ref: str | None
    current_project_state: ProjectState
    current_link_revision: int | None
    requested_at: AwareDatetime
    resolved_at: AwareDatetime | None


class HandoffPage(Strict):
    items: list[HandoffSummary]
    next_offset: int | None


class CurrentViewInput(Strict):
    """The Leads-owned viewer bridge accepts no Current-selected input."""

    handoff_ref: HandoffRef


class CurrentViewHistory(Strict):
    """Leads-owned handoff history, without stored Current refs or errors."""

    ref: str
    opportunity_ref: str
    purpose: str
    state: HandoffState
    attempt_count: int
    requested_at: AwareDatetime
    resolved_at: AwareDatetime | None


class CurrentItemContent(Strict):
    """Display fields allowlisted from a successful ``current.item.get``."""

    title: str
    body: str
    status: str
    priority: str
    area: str | None
    due_on: date | None
    snoozed_until: AwareDatetime | None
    woke: bool
    overdue: bool
    blocked: bool
    event_count: int
    revision: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None


class CurrentProjectContent(Strict):
    """Display fields allowlisted from a successful ``current.project.get``."""

    title: str
    description: str
    status: str
    area: str | None
    due_on: date | None
    revision: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    item_counts: dict[str, int]


class CurrentItemLive(Strict):
    state: Literal["live"] = "live"
    ref: str
    content: CurrentItemContent


class CurrentItemState(Strict):
    state: Literal["deleted", "denied", "unavailable"]
    message: str


class CurrentProjectLive(Strict):
    state: Literal["live"] = "live"
    ref: str
    content: CurrentProjectContent


class CurrentProjectState(Strict):
    state: Literal["never_assigned", "cleared", "deleted", "denied", "unavailable"]
    message: str


class CurrentView(Strict):
    history: CurrentViewHistory
    item: CurrentItemLive | CurrentItemState
    project: CurrentProjectLive | CurrentProjectState


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


CONTACT_TARGETS: Final = frozenset({"person.email", "person.phone"})
"""Fact targets whose values are contact points: ``restricted`` in the tier table."""
CONTACT_KEYS: Final = frozenset({"verified_email"})
"""Row columns holding a contact point: an observation's verified sender address."""
NESTED_TYPES: Final = {
    "qualification": "qualification",
    "drafts": "draft",
    "opportunity_observation": "observation",
}
"""Keys of an opportunity's nested rows, by the record type each row is. The rows carry
no ``ref`` for the renderer's exclusion rule to read, so the projection drops them when
``leads.redaction.exclude_types`` lists their type."""
WITHHELD_KEYS: Final = frozenset({"snapshot", "external_subject_id"})
"""Row keys never sent to a model: a handoff's outbound payload, which is the raw body
a destination would receive and may hold anything its author put in it, and an
observation's source identity key for its sender. Sites often fill that key with an
email or a phone number, but it is an identifier, not a contact point, so releasing
contact values does not release it."""


def _projected(
    value: Any, released: frozenset[str], contacts: bool, excluded: frozenset[str]
) -> Any:
    """``value`` without the restricted values a model may not see.

    A field row (a dict with ``target`` and ``value_text``, as opportunity field states
    and observation fields are) loses its value when the target is a contact point and
    contact values are not released, or when it is an extension field not in
    ``released``. The row stays, marked ``value_withheld``, so a model still sees that
    the field is set. Nested rows of an excluded record type (:data:`NESTED_TYPES`) are
    dropped; rows that carry their own ``ref`` are left to the renderer, which drops a
    row whose ``ref`` names an excluded type.
    """
    if isinstance(value, list):
        return [_projected(item, released, contacts, excluded) for item in value]
    if not isinstance(value, dict):
        return value
    out = {
        key: _projected(item, released, contacts, excluded)
        for key, item in value.items()
        if key not in WITHHELD_KEYS
        and (contacts or key not in CONTACT_KEYS)
        and NESTED_TYPES.get(key) not in excluded
    }
    target = out.get("target")
    if isinstance(target, str) and "value_text" in out:
        hidden = (target in CONTACT_TARGETS and not contacts) or (
            target.startswith("ext.") and target not in released
        )
        if hidden:
            del out["value_text"]
            out["value_withheld"] = True
    return out


class _Projected(Strict):
    """A Leads output whose model view drops restricted values row by row.

    The payload is ``internal`` as a whole, the tier the tier table gives opportunity
    data, so a model sees it for the purposes in ``redaction.internal_purposes``.
    What is ``restricted`` inside it depends on the row, so :meth:`model_projection`
    removes it before the tool facade renders the rest (see
    :mod:`rheo_core.redaction.render`). Extension field values are withheld unless the
    handler named their targets in :meth:`releasing`, so a handler that never looked
    up the presets' sensitivity sends none of them.
    """

    _released: frozenset[str] = PrivateAttr(default_factory=frozenset)

    def releasing(self, targets: frozenset[str]) -> Self:
        self._released = targets
        return self


class RecordOutput(_Projected):
    ref: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    revision: Annotated[int, Tiered(SensitivityTier.PUBLIC)] = 1
    data: Annotated[dict[str, Any], Tiered(SensitivityTier.INTERNAL)]

    def model_projection(self, policy: TierPolicy) -> Self:
        projected = _projected(
            self.data,
            self._released,
            policy.contact_points_allowed,
            policy.excluded_types("leads"),
        )
        return self.model_copy(update={"data": projected})


class ItemsOutput(_Projected):
    items: Annotated[list[dict[str, Any]], Tiered(SensitivityTier.INTERNAL)]

    def model_projection(self, policy: TierPolicy) -> Self:
        projected = _projected(
            self.items,
            self._released,
            policy.contact_points_allowed,
            policy.excluded_types("leads"),
        )
        return self.model_copy(update={"items": projected})


class ReferenceEvent(Strict):
    ref: str
    related_ref: str | None = None
    revision: int = 1


def _deletable(v: object, info: ValidationInfo) -> RecordRef:
    # Two types are deletable here, so a short ID must say which (no bare tail).
    short = RecordRef.short(v) if isinstance(v, str) else None
    if short is not None and short[0] == "leads":
        reference = ref_value(v, info, "leads", short[1])
    else:
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
