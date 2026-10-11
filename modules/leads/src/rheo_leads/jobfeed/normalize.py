"""Turn job feed rows into the flat values the job feed mapping accepts.

Pure functions only. ``normalize_row`` validates one feed row and returns ``None``
when it fails, so a caller counts it and moves on; one bad row never aborts a batch.
Every value is normalized here to exactly what the Job search preset's field kinds
accept (decimals as canonical strings, integers, ``"true"``/``"false"``, ISO dates),
so the installed mapping copies them through untransformed.

Unknown stays unknown: a value the row does not carry is ``None`` and is left out of
the body, never written as zero or guessed. A feed row never supplies a description:
only a successful detail read does (``apply_detail``).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from rheo_leads.jobfeed.presets import Purpose

JobType = Literal["fixed", "hourly"]

# Proposal-count bands to the stored upper bound. "50+" has no upper bound, so it is
# stored as 51: over the discovery cap and flagged not exact.
BANDS: Mapping[str, int] = MappingProxyType(
    {
        "Fewer than 5": 4,
        "5 to 10": 10,
        "10 to 15": 15,
        "15 to 20": 20,
        "20 to 50": 50,
        "50+": 51,
    }
)

# A posting id is "~02" and ASCII digits, nothing else. It is the first segment of a
# delivery's event id, whose separator is ".", so it must never contain a dot.
POSTING_ID_PATTERN = re.compile(r"~02[0-9]+")
EVENT_ID_SEPARATOR = "."
# The token is taken loosely here and checked against POSTING_ID_PATTERN afterwards.
_JOB_PATH = re.compile(r"/jobs/(?:[^/]+_)?(?P<token>~02[^/_]*)/?")
_FEED_ID = re.compile(r"[0-9A-Za-z_~-]{1,64}")
_AMOUNT = re.compile(r"\$([0-9,]+(?:\.[0-9]+)?)")
_PAYMENT = {"VERIFIED": True, "NOT_VERIFIED": False}

# The keys ``to_body`` can emit. The installed mapping reads exactly these (``platform``
# is a mapping constant); ``observed_at`` feeds the identity row.
BODY_KEYS: tuple[str, ...] = (
    "title",
    "description",
    "posting_url",
    "posting_id",
    "job_type",
    "budget_min",
    "budget_max",
    "proposal_count",
    "proposal_count_exact",
    "proposal_observed_at",
    "posted_on",
    "client_payment_verified",
    "client_spend",
    "client_rating",
    "client_reviews",
    "client_hire_rate",
    "applied",
    "observed_at",
)
# The fields whose change makes a sighting worth delivering. Both observation times
# are stamped at response time, so neither is ever a change.
COMPARABLE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "posting_url",
    "job_type",
    "budget_min",
    "budget_max",
    "proposal_count",
    "proposal_count_exact",
    "posted_on",
    "client_payment_verified",
    "client_spend",
    "client_rating",
    "client_reviews",
    "client_hire_rate",
    "applied",
)


def parse_number(value: object) -> Decimal | None:
    """A number from ``$``/comma-formatted text or a plain number; ``None`` for
    ``None``, booleans, unparseable or non-finite input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _flag(value: bool) -> str:
    # The boolean field kind accepts only these two strings. A Python bool would be
    # rendered "True" by the mapping and silently dropped as invalid evidence.
    return "true" if value else "false"


def _count(value: object) -> int | None:
    """A non-negative whole number, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    number = parse_number(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _posted_on(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.date()


def _budget(value: object) -> tuple[Decimal | None, Decimal | None]:
    """``$`` amounts: one is both ends, two or more are first and last. "Up to"
    leaves no minimum and a "+" leaves no maximum."""
    if not isinstance(value, str):
        return None, None
    amounts = _AMOUNT.findall(value)
    low = parse_number(amounts[0]) if amounts else None
    high = parse_number(amounts[-1]) if len(amounts) > 1 else low
    if "+" in value:
        high = None
    if value.strip().lower().startswith("up to"):
        low = None
    return low, high


def _job_type(value: object) -> JobType | None:
    # The preset enum is fixed|hourly; anything else is left out, never "unknown".
    if value == "fixed":
        return "fixed"
    if value == "hourly":
        return "hourly"
    return None


def posting_id_valid(value: str) -> bool:
    """True only for "~02" and digits; such an id can never hold the separator."""
    return (
        POSTING_ID_PATTERN.fullmatch(value) is not None
        and EVENT_ID_SEPARATOR not in value
    )


@dataclass(frozen=True)
class NormalizedPosting:
    posting_id: str
    title: str
    posting_url: str
    description: str | None
    job_type: JobType | None
    budget_min: Decimal | None
    budget_max: Decimal | None
    proposal_count: int | None
    # True for an exact count, False for a band's upper bound, None with no count.
    proposal_count_exact: bool | None
    proposal_observed_at: datetime | None
    posted_on: date | None
    client_payment_verified: bool | None
    client_spend: Decimal | None
    client_rating: Decimal | None
    client_reviews: int | None
    client_hire_rate: Decimal | None
    applied: bool
    observed_at: datetime
    purpose: Purpose
    # Index of the originating default search, for tie-breaks between sightings.
    search_ordinal: int
    # The raw band text when the row carried a known band, so "50+" is recognisable.
    count_band_label: str | None
    # The feed row's own ``id``, which a detail read sends. Never delivered, never
    # compared: ``to_body`` lists its keys explicitly and leaves it out.
    feed_id: str | None = None

    def to_body(self) -> dict[str, str | int]:
        """The flat delivery body: unknown values are left out, never zero."""
        values: dict[str, str | int | None] = {
            "title": self.title,
            "description": self.description,
            "posting_url": self.posting_url,
            "posting_id": self.posting_id,
            "job_type": self.job_type,
            "budget_min": _optional_decimal(self.budget_min),
            "budget_max": _optional_decimal(self.budget_max),
            "proposal_count": self.proposal_count,
            "proposal_count_exact": _optional_flag(self.proposal_count_exact),
            "proposal_observed_at": _optional_time(self.proposal_observed_at),
            "posted_on": None if self.posted_on is None else self.posted_on.isoformat(),
            "client_payment_verified": _optional_flag(self.client_payment_verified),
            "client_spend": _optional_decimal(self.client_spend),
            "client_rating": _optional_decimal(self.client_rating),
            "client_reviews": self.client_reviews,
            "client_hire_rate": _optional_decimal(self.client_hire_rate),
            "applied": _flag(self.applied),
            "observed_at": self.observed_at.isoformat(),
        }
        return {key: value for key, value in values.items() if value is not None}

    def comparable_fields(self) -> dict[str, str]:
        """Canonical text per comparable field that carries a value."""
        body = self.to_body()
        return {key: str(body[key]) for key in COMPARABLE_FIELDS if key in body}


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _optional_flag(value: bool | None) -> str | None:
    return None if value is None else _flag(value)


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _feed_id(value: object) -> str | None:
    """The row's own id when it is a short plain token, else unknown. It goes back
    upstream as an argument, so anything else is not sent."""
    if type(value) is not str or _FEED_ID.fullmatch(value) is None:
        return None
    return value


def _canonical_url(url: object, job_hosts: Sequence[str]) -> tuple[str, str] | None:
    """The canonical posting URL on the first configured host, and its posting id."""
    if not isinstance(url, str) or not job_hosts:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in job_hosts:
        return None
    match = _JOB_PATH.fullmatch(parts.path)
    if match is None or not posting_id_valid(match.group("token")):
        return None
    return f"https://{job_hosts[0]}{parts.path.rstrip('/')}", match.group("token")


def normalize_row(
    row: object,
    job_hosts: Sequence[str],
    *,
    observed_at: datetime,
    purpose: Purpose,
    search_ordinal: int,
) -> NormalizedPosting | None:
    """Validate and normalize one feed row, or ``None`` when it fails validation."""
    if isinstance(job_hosts, str):
        raise TypeError("job_hosts must be a sequence of host names")
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must carry a time zone")
    if not isinstance(row, Mapping):
        return None
    located = _canonical_url(row.get("url"), job_hosts)
    title = row.get("title")
    if located is None or not isinstance(title, str) or not title.strip():
        return None
    count = row.get("proposal_count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        return None
    tier = row.get("proposals_tier")
    band = tier if isinstance(tier, str) and tier in BANDS else None
    exact: bool | None = None
    if count is not None:
        exact = True
    elif band is not None:
        count, exact = BANDS[band], False
    low, high = _budget(row.get("budget"))
    published = row.get("published_date")
    client = row.get("client")
    client = client if isinstance(client, Mapping) else {}
    verification = client.get("verification_status")
    url, posting_id = located
    return NormalizedPosting(
        posting_id=posting_id,
        title=title,
        posting_url=url,
        description=None,
        job_type=_job_type(row.get("job_type")),
        budget_min=low,
        budget_max=high,
        proposal_count=count,
        proposal_count_exact=exact,
        proposal_observed_at=observed_at if count is not None else None,
        posted_on=_posted_on(published if published else row.get("created_date")),
        client_payment_verified=(
            _PAYMENT.get(verification) if isinstance(verification, str) else None
        ),
        client_spend=parse_number(client.get("total_spent")),
        client_rating=parse_number(client.get("rating")),
        # Missing reviews stay unknown: they never imply a new client.
        client_reviews=_count(client.get("total_reviews")),
        # The hire rate comes only from a detail read's client record.
        client_hire_rate=None,
        applied=row.get("applied") is True,
        observed_at=observed_at,
        purpose=purpose,
        search_ordinal=search_ordinal,
        count_band_label=band,
        feed_id=_feed_id(row.get("id")),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _kept[T](fresh: T | None, current: T | None) -> T | None:
    return current if fresh is None else fresh


def apply_detail(
    posting: NormalizedPosting, detail_row: object, job_hosts: Sequence[str]
) -> NormalizedPosting:
    """A copy carrying the full description and the live client record from a
    successful detail read; any other detail returns ``posting`` unchanged.

    A detail value overrides the feed's, and a value the detail lacks keeps what the
    posting already holds, so re-applying the first read to a duplicate sighting (or
    a later, thinner read) never restores a stale feed metric. A detail carries no
    proposal count, so the count is never touched. The detail is matched to the
    posting by id at the call site; ``job_hosts`` is accepted for call symmetry with
    ``normalize_row`` and is not consulted.
    """
    if not isinstance(detail_row, Mapping) or detail_row.get("status") != "ok":
        return posting
    content = _mapping(
        _mapping(_mapping(detail_row.get("data")).get("marketplaceJobPosting")).get(
            "content"
        )
    )
    description = content.get("description")
    if not isinstance(description, str) or not description.strip():
        return posting
    record = _mapping(detail_row.get("client_record"))
    hire_percent = parse_number(record.get("hire_rate_percent"))
    return replace(
        posting,
        description=description,
        client_spend=_kept(
            parse_number(record.get("spend_total")), posting.client_spend
        ),
        client_reviews=_kept(
            _count(record.get("feedback_count")), posting.client_reviews
        ),
        client_rating=_kept(
            parse_number(record.get("feedback_score")), posting.client_rating
        ),
        client_hire_rate=_kept(
            None if hire_percent is None else hire_percent / 100,
            posting.client_hire_rate,
        ),
    )
