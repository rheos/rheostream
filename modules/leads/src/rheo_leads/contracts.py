"""Strict wire shapes for receipt acceptance and manual capture."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AcceptDeliveryInput(Strict):
    connection_ref: str
    source_event_id: str = Field(min_length=1, max_length=512)
    body: str
    content_type: str = "application/json"
    source_occurred_at: AwareDatetime | None = None
    signing_key_generation: int | None = Field(default=None, ge=1)
    funnel_ref: str | None = None
    campaign_ref: str | None = None


class CaptureInput(Strict):
    funnel_ref: str
    campaign_ref: str | None = None
    body: dict[str, object]


class AcceptedDelivery(Strict):
    receipt_ref: str
    outcome: str


class ConnectionInput(Strict):
    connection_ref: str


class ConnectionHealth(Strict):
    last_accepted_at: datetime | None
    last_processed_at: datetime | None
    lag_seconds: int
    unresolved_failures: int
    failed_delivery_count: int


class DeliveryReceived(Strict):
    receipt_ref: str
    connection_ref: str
    transport: str


class ObservationAccepted(Strict):
    observation_ref: str
    receipt_ref: str
    connection_ref: str
    funnel_ref: str
    campaign_ref: str | None
    party_ref: str | None
    completeness: int
