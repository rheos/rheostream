"""Bounded wire inputs. Verified evidence is separate from ordinary contact hints."""

from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from rheo_core.redaction.tiers import SensitivityTier, Tiered

from rheo_relationships.references import CandidateRef, MergeRef, PartyRef


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PartyInput(Strict):
    ref: PartyRef


class CreateInput(Strict):
    kind: Literal["person", "organization"]
    display_name: str = Field(min_length=1, max_length=512)


class UpdateInput(PartyInput):
    revision: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=512)


class FindInput(Strict):
    query: str = Field(min_length=1, max_length=512)
    kind: Literal["person", "organization"] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ContactInput(Strict):
    party_ref: PartyRef
    revision: int = Field(ge=1)
    contact_point_id: UUID


class AddContactInput(Strict):
    party_ref: PartyRef
    revision: int = Field(ge=1)
    kind: Literal["email", "phone", "address", "url"]
    value: str = Field(min_length=1, max_length=2048)


class AddAffiliationInput(Strict):
    person_ref: PartyRef
    organization_ref: PartyRef
    revision: int = Field(ge=1)
    role_label: str | None = Field(default=None, max_length=512)
    valid_from: date | None = None
    valid_to: date | None = None


class EndAffiliationInput(Strict):
    person_ref: PartyRef
    revision: int = Field(ge=1)
    affiliation_id: UUID
    valid_to: date


class Hints(Strict):
    name: str | None = Field(default=None, max_length=512)
    email: str | None = Field(default=None, max_length=512)
    phone: str | None = Field(default=None, max_length=128)
    organization_name: str | None = Field(default=None, max_length=512)
    organization_domain: str | None = Field(default=None, max_length=512)


class ResolveInput(Strict):
    source_namespace: str = Field(min_length=1, max_length=512)
    external_subject_id: str | None = Field(default=None, min_length=1, max_length=512)
    verified_email: str | None = Field(default=None, min_length=1, max_length=512)
    hints: Hints = Hints()
    evidence_ref: str = Field(min_length=1, max_length=512)
    occurred_at: AwareDatetime


class ResolveOutput(Strict):
    party_ref: str
    outcome: str
    review_candidate_refs: list[str]


class MergeInput(Strict):
    source_ref: PartyRef
    target_ref: PartyRef
    source_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)
    evidence: str | None = Field(default=None, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class UnmergeInput(Strict):
    merge_record_ref: MergeRef
    note: str | None = Field(default=None, max_length=4000)


class ReviewInput(Strict):
    candidate_ref: CandidateRef
    decision: Literal["accept", "reject"]
    survivor: PartyRef | None = None
    note: str | None = Field(default=None, max_length=4000)


class ListReviewInput(Strict):
    state: Literal["open", "accepted", "rejected", "withdrawn"] = "open"
    limit: int = Field(default=50, ge=1, le=100)


class PartyOutput(Strict):
    ref: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    kind: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    display_name: Annotated[str, Tiered(SensitivityTier.INTERNAL)]
    canonical_ref: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    alias_refs: Annotated[list[str], Tiered(SensitivityTier.PUBLIC)]
    contact_points: Annotated[list[dict[str, Any]], Tiered(SensitivityTier.RESTRICTED)]
    affiliations: Annotated[list[dict[str, Any]], Tiered(SensitivityTier.INTERNAL)]
    revision: Annotated[int, Tiered(SensitivityTier.PUBLIC)]


class ItemsOutput(Strict):
    items: list[dict[str, Any]]


class MergeOutput(Strict):
    merge_record_ref: str


class ChangeOutput(Strict):
    id: UUID
    revision: int


class AliasesOutput(Strict):
    canonical_ref: str
    alias_refs: list[str]
