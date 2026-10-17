"""Strict wire shapes for receipt acceptance and manual capture."""

from datetime import datetime
from typing import Literal

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


PASTE_PAGE_LIMIT = 2_000_000
"""The longest copied search-results page accepted, in characters. A job list of a
few dozen tiles is well under it."""


class PasteJobsInput(Strict):
    pipeline_ref: str
    # A copied search-results page (its HTML), or one full posting's copied text.
    kind: Literal["search_page", "posting"]
    content: str = Field(min_length=1, max_length=PASTE_PAGE_LIMIT)
    # Required for a posting: its link carries the posting id.
    posting_url: str | None = Field(default=None, max_length=2048)


class PastedJobs(Strict):
    found: int
    """Jobs recognised with a usable posting link."""
    accepted: int
    duplicate: int
    """Already received with exactly this content; nothing new was recorded."""
    skipped: int
    """Recognised but left out: no usable posting link or title."""
    warnings: list[str]
    receipt_refs: list[str]


class ConnectionInput(Strict):
    connection_ref: str


class AccountChoice(Strict):
    organization_id: str
    label: str | None


class CollectorHealth(Strict):
    """The job feed collector's state as its owner reads it. Never a secret
    reference, a client column, an endpoint or the pending authorization."""

    authorization_state: str
    last_completed_at: datetime | None
    next_run_not_before: datetime | None
    fetched: int
    imported: int
    skipped_dup: int
    refreshed: int
    filtered: int
    skipped_untracked: int
    errors: int
    detail_reads: int
    pages: int
    organization_id: str | None
    account_choices: list[AccountChoice]


class ConnectionHealth(Strict):
    last_accepted_at: datetime | None
    last_processed_at: datetime | None
    lag_seconds: int
    unresolved_failures: int
    failed_delivery_count: int
    last_error: str | None = None
    last_error_at: datetime | None = None
    # Present only for the job feed collector's poll connection.
    collector: CollectorHealth | None = None


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
