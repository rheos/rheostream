"""Tier 1: the deterministic client filters, before any model is asked.

The Upwork path of the job-triage filters this module ports: an established client
must have a verified payment method, enough spend, a good rating and a working hire
rate; a new client (no spend and no reviews, both known) needs only the payment
method. Competition above the proposal cap holds a job back either way.

**An unknown value never fails a clause.** A posting carries what its source showed,
and a missing spend, rating, hire rate or payment status is unavailable data, not a
bad client. Unknown payment is the one soft clause that still reaches the model, as a
condition the owner confirms. A platform other than Upwork carries none of these
fields, so it passes untouched.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from rheo_leads.scoring.profile import Tier1Policy

EXTENSION: Final = "ext.jobsearch."
UPWORK: Final = "upwork"


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _integer(value: str | None) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _rating(value: str | None) -> Decimal | None:
    """A client rating; 0 is the feed's placeholder for no rating yet, so unknown."""
    rating = _decimal(value)
    return None if rating == 0 else rating


def _flag(value: str | None) -> bool | None:
    return {"true": True, "false": False}.get(value or "")


@dataclass(frozen=True)
class JobFacts:
    """What the filters and the scorer read about one job; ``None`` is unknown."""

    title: str
    description: str | None = None
    platform: str | None = None
    job_type: str | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    proposal_count: int | None = None
    proposal_count_exact: bool | None = None
    payment_verified: bool | None = None
    client_spend: Decimal | None = None
    client_rating: Decimal | None = None
    client_reviews: int | None = None
    client_hire_rate: Decimal | None = None

    @classmethod
    def from_fields(cls, title: str, fields: Mapping[str, str]) -> "JobFacts":
        """From an opportunity's resolved field values, keyed by target."""

        def ext(name: str) -> str | None:
            return fields.get(EXTENSION + name)

        description = fields.get("message")
        job_type = ext("job_type")
        reviews = _integer(ext("client_reviews"))
        hire_rate = _decimal(ext("client_hire_rate"))
        if hire_rate == 0 and reviews == 0:
            # An empty client record reports 0% for a client with no history,
            # which is no hire rate at all, not a client who never hires.
            hire_rate = None
        return cls(
            title=title,
            description=description if description and description.strip() else None,
            platform=ext("platform"),
            job_type=job_type if job_type in ("fixed", "hourly") else None,
            budget_min=_decimal(ext("budget_min")),
            budget_max=_decimal(ext("budget_max")),
            proposal_count=_integer(ext("proposal_count")),
            proposal_count_exact=_flag(ext("proposal_count_exact")),
            payment_verified=_flag(ext("client_payment_verified")),
            client_spend=_decimal(ext("client_spend")),
            client_rating=_rating(ext("client_rating")),
            client_reviews=reviews,
            client_hire_rate=hire_rate,
        )

    @property
    def on_default_platform(self) -> bool:
        return self.platform in (None, UPWORK)


@dataclass(frozen=True)
class Tier1Result:
    """``failed`` clauses hold the job back; ``soft`` ones go on to the model."""

    failed: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()
    new_client: bool = False

    @property
    def passed(self) -> bool:
        return not self.failed


def _money(value: Decimal) -> str:
    return f"${value:,.0f}" if value == value.to_integral_value() else f"${value:,.2f}"


def _percent(value: Decimal) -> str:
    return f"{value * 100:.0f}%"


def run_tier1(job: JobFacts, policy: Tier1Policy) -> Tier1Result:
    """The filters for one job, each failing clause as a sentence the owner reads."""
    if not job.on_default_platform:
        return Tier1Result()
    over_cap = (
        job.proposal_count is not None and job.proposal_count > policy.max_proposals
    )
    crowded = f"More than {policy.max_proposals} proposals already."
    unknown_payment = "Payment verification is unknown; confirm it before applying."
    new_client = job.client_spend == 0 and job.client_reviews == 0
    if new_client:
        if job.payment_verified is False:
            return Tier1Result(
                failed=("A new client without a verified payment method.",),
                new_client=True,
            )
        if over_cap:
            return Tier1Result(failed=(crowded,), new_client=True)
        soft = (unknown_payment,) if job.payment_verified is None else ()
        return Tier1Result(soft=soft, new_client=True)

    failed: list[str] = []
    if job.payment_verified is False:
        failed.append("The client's payment method is not verified.")
    if job.client_spend is not None and job.client_spend < policy.min_client_spend:
        failed.append(
            f"The client has spent {_money(job.client_spend)}, below the "
            f"{_money(policy.min_client_spend)} minimum."
        )
    if job.client_rating is not None and job.client_rating < policy.min_client_rating:
        failed.append(
            f"The client's rating is {job.client_rating}, below "
            f"{policy.min_client_rating}."
        )
    if job.client_hire_rate is not None and job.client_hire_rate < policy.min_hire_rate:
        failed.append(
            f"The client hires on {_percent(job.client_hire_rate)} of their jobs, "
            f"below {_percent(policy.min_hire_rate)}."
        )
    if over_cap:
        failed.append(crowded)
    soft = (unknown_payment,) if job.payment_verified is None else ()
    return Tier1Result(failed=tuple(failed), soft=() if failed else soft)
