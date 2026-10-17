"""Turn pasted job text into the flat bodies the job mapping accepts.

Two kinds of paste, both pure functions that never raise on input:

- :func:`parse_search_page`: a copied search-results job list (its HTML). Three tile
  markups are recognised: the legacy ``section.air3-card-section`` tile, the
  ``article[data-test=JobTile]`` redesign, and the ``div[data-test=job-tile]`` feed
  redesign. A tile's text is a snippet, so it never becomes a description: only a full
  posting supplies one, as with the job feed.
- :func:`parse_posting`: one full posting, as its URL and the copied page text. The
  first non-empty line is the title, the whole text the description, and the proposal
  band in the activity section the competition count.

Each recognised job yields a :class:`PastedJob` whose :meth:`PastedJob.to_body` uses
the job feed's body keys and value forms, so intake, routing and derivation treat a
pasted job exactly like a feed sighting. A job is keyed by the ``~02`` posting id in
its link on a configured job host; one without it is reported and skipped, never
matched on a blank id. Unknown stays unknown: nothing absent is written as zero or
``false``.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from rheo_leads.jobfeed.normalize import BANDS, posting_id_valid

JobType = Literal["fixed", "hourly"]

POSTING_LIMIT: Final = 16000
"""The longest full-posting text accepted, the limit a pasted inquiry has too."""

_JOB_PATH: Final = re.compile(r"/jobs/(?:[^/]*_)?(?P<token>~02[0-9]+)/?")
_OPENING_UID: Final = re.compile(r"[0-9]+")
_SPEND: Final = re.compile(r"^\$([\d,]+(?:\.\d+)?)([KM])?\+?$", re.IGNORECASE)
_SPEND_UNIT: Final = {"K": 1_000, "M": 1_000_000}
_RATING: Final = re.compile(r"Rating is ([\d.]+) out of 5")
_MONEY: Final = re.compile(r"\$([\d,]+(?:\.\d+)?)")
_HOURLY_RANGE: Final = re.compile(
    r"Hourly:\s*\$([\d,]+(?:\.\d+)?)\s*-\s*\$([\d,]+(?:\.\d+)?)"
)
_HOURLY_SINGLE: Final = re.compile(r"Hourly:\s*\$([\d,]+(?:\.\d+)?)")
_AGO: Final = re.compile(r"^(\d+)\s+(minute|hour|day)s?\s+ago$", re.IGNORECASE)
# "Proposals: Less than 5", "Proposals: 20 to 50", "Proposals: 50+"; the colon and
# spacing vary because the text arrives by copy and paste.
_POSTING_BAND: Final = re.compile(
    r"proposals:?\s*(?:less than\s*(?P<lt>\d+)|(?P<lo>\d+)\s*to\s*(?P<hi>\d+)"
    r"|(?P<plus>\d+)\s*\+)",
    re.IGNORECASE,
)
# A tile's tier label to the feed's own band names, so both store one count.
_TILE_BANDS: Final = {"Less than 5": "Fewer than 5", **{band: band for band in BANDS}}


@dataclass(frozen=True)
class PastedJob:
    posting_id: str
    posting_url: str
    title: str
    description: str | None = None
    job_type: JobType | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    proposal_count: int | None = None
    # False for a band's upper bound; pasted text never carries an exact count.
    proposal_count_exact: bool | None = None
    posted_on: date | None = None
    client_payment_verified: bool | None = None
    client_spend: Decimal | None = None
    client_rating: Decimal | None = None

    def to_body(self, observed_at: datetime) -> dict[str, str | int]:
        """The flat delivery body, in the job feed's keys and value forms."""
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must carry a time zone")
        counted = self.proposal_count is not None
        values: dict[str, str | int | None] = {
            "title": self.title,
            "description": self.description,
            "posting_url": self.posting_url,
            "posting_id": self.posting_id,
            "job_type": self.job_type,
            "budget_min": _decimal(self.budget_min),
            "budget_max": _decimal(self.budget_max),
            "proposal_count": self.proposal_count,
            "proposal_count_exact": _flag(self.proposal_count_exact),
            "proposal_observed_at": observed_at.isoformat() if counted else None,
            "posted_on": None if self.posted_on is None else self.posted_on.isoformat(),
            "client_payment_verified": _flag(self.client_payment_verified),
            "client_spend": _decimal(self.client_spend),
            "client_rating": _decimal(self.client_rating),
            "observed_at": observed_at.isoformat(),
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class ParsedPaste:
    jobs: tuple[PastedJob, ...]
    skipped: int
    """Recognised jobs left out: no usable posting link, or no title."""
    warnings: tuple[str, ...]


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _flag(value: bool | None) -> str | None:
    return None if value is None else ("true" if value else "false")


def posting_link(url: object, job_hosts: Sequence[str]) -> tuple[str, str] | None:
    """The canonical posting URL and its ``~02`` id, for a link on a job host.

    A relative ``/jobs/...`` link is taken as on the first host, and a link without
    a scheme as ``https``. The canonical URL drops the title slug and any query, so
    every paste of one posting carries the same URL.
    """
    if not isinstance(url, str) or not url.strip() or not job_hosts:
        return None
    text = url.strip()
    if text.startswith("/"):
        text = f"https://{job_hosts[0]}{text}"
    elif "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in job_hosts:
        return None
    match = _JOB_PATH.fullmatch(parts.path)
    if match is None or not posting_id_valid(match.group("token")):
        return None
    token = match.group("token")
    return f"https://{job_hosts[0]}/jobs/{token}", token


def _money(text: str) -> Decimal | None:
    cleaned = text.strip().lstrip("$").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except ArithmeticError:
        return None
    return value if value.is_finite() and value >= 0 else None


def _spend(text: str) -> Decimal | None:
    match = _SPEND.match(text.strip())
    if match is None:
        return None
    value = _money(match.group(1))
    if value is None:
        return None
    unit = match.group(2)
    return value * _SPEND_UNIT[unit.upper()] if unit else value


def _rating(text: str) -> Decimal | None:
    value = _money(text)
    return value if value is not None and value <= 5 else None


def _posted_on(text: str, today: date) -> date | None:
    """A relative "N minutes/hours/days ago" or "yesterday" as a date, else unknown."""
    label = re.sub(r"^Posted\s+", "", text.strip(), flags=re.IGNORECASE)
    if label.lower() == "yesterday":
        return today - timedelta(days=1)
    match = _AGO.match(label)
    if match is None:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    if unit == "day":
        return today - timedelta(days=amount)
    # Minutes and hours ago: today, unless they reach back past midnight. Without the
    # time of day that cannot be told, so only whole days move the date.
    return (
        today
        if unit == "minute" or amount < 24
        else today - timedelta(days=amount // 24)
    )


def _band(label: str, warnings: list[str]) -> tuple[int | None, bool | None]:
    tier = re.sub(r"^Proposals:\s*", "", label.strip())
    band = _TILE_BANDS.get(tier)
    if band is None:
        if tier:
            warnings.append(f"Unknown proposals tier {tier!r}")
        return None, None
    return BANDS[band], False


def _budget_label(
    label: str, fixed_amount: str | None
) -> tuple[JobType | None, Decimal | None, Decimal | None]:
    """(job type, minimum, maximum) from a job-type label and any separate fixed
    budget text. A fixed price is a maximum only."""
    if label.startswith("Fixed"):
        amount = _MONEY.search(fixed_amount or label)
        return "fixed", None, _money(amount.group(1)) if amount else None
    if label.startswith("Hourly"):
        rng = _HOURLY_RANGE.search(label)
        if rng:
            return "hourly", _money(rng.group(1)), _money(rng.group(2))
        single = _HOURLY_SINGLE.search(label)
        return "hourly", None, _money(single.group(1)) if single else None
    return None, None, None


def _text(tag: Tag | None, separator: str = "") -> str:
    return tag.get_text(separator, strip=True) if tag is not None else ""


def _attr(tile: Tag, name: str) -> Tag | None:
    found = tile.find(None, {"data-test": name})
    return found if isinstance(found, Tag) else None


def _href(anchor: Tag | None) -> str | None:
    if anchor is None:
        return None
    href = anchor.get("href")
    return href if isinstance(href, str) and href else None


@dataclass
class _Tile:
    title: str | None
    url: str | None
    job_type: JobType | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    proposal_count: int | None = None
    proposal_count_exact: bool | None = None
    posted_on: date | None = None
    payment_verified: bool | None = None
    client_spend: Decimal | None = None
    client_rating: Decimal | None = None


def _legacy_tile(tile: Tag, today: date, warnings: list[str]) -> _Tile:
    anchor = tile.find("a", attrs={"data-ev-label": "link"})
    anchor = anchor if isinstance(anchor, Tag) else None
    found = _Tile(title=_text(anchor) or None, url=_href(anchor))
    job_type = _text(_attr(tile, "job-type"))
    budget = _attr(tile, "budget")
    found.job_type, found.budget_min, found.budget_max = _budget_label(
        job_type, _text(budget) if budget is not None else None
    )
    if job_type.startswith("Fixed") and budget is None:
        found.budget_max = None
    posted = _attr(tile, "posted-on")
    found.posted_on = _posted_on(_text(posted), today) if posted is not None else None
    status = _attr(tile, "payment-verification-status")
    if status is not None:
        found.payment_verified = _text(status) == "Payment verified"
    spend = _attr(tile, "client-spendings")
    amount = _attr(spend, "formatted-amount") if spend is not None else None
    found.client_spend = _spend(_text(amount)) if amount is not None else None
    found.client_rating = _sr_rating(_attr(tile, "js-feedback"))
    tier = _attr(tile, "proposals-tier")
    if tier is not None:
        found.proposal_count, found.proposal_count_exact = _band(_text(tier), warnings)
    return found


def _sr_rating(container: Tag | None) -> Decimal | None:
    if container is None:
        return None
    hidden = container.find("span", class_="sr-only")
    match = _RATING.search(_text(hidden if isinstance(hidden, Tag) else None))
    return _rating(match.group(1)) if match else None


def _article_tile(tile: Tag, today: date, warnings: list[str]) -> _Tile:
    anchor = tile.find(
        "a",
        attrs={
            "data-test": lambda value: (
                value is not None and "job-tile-title-link" in value
            )
        },
    )
    anchor = anchor if isinstance(anchor, Tag) else None
    found = _Tile(title=_text(anchor) or None, url=_href(anchor))
    label = _attr(tile, "job-type-label")
    if label is not None:
        fixed = _attr(tile, "is-fixed-price")
        found.job_type, found.budget_min, found.budget_max = _budget_label(
            _text(label, " "), _text(fixed, " ") if fixed is not None else None
        )
        if found.job_type == "fixed" and fixed is None:
            found.budget_max = None
    published = _attr(tile, "job-pubilshed-date")  # sic: the provider's own spelling
    if published is not None:
        span = published.find("span")
        found.posted_on = _posted_on(
            _text(span if isinstance(span, Tag) else published), today
        )
    payment = _attr(tile, "payment-verified")
    if payment is not None:
        text = _text(payment, " ")
        # The unverified text contains the verified one, so it is checked first.
        if "Payment unverified" in text:
            found.payment_verified = False
        elif "Payment verified" in text:
            found.payment_verified = True
    spent = _attr(tile, "total-spent")
    if spent is not None:
        found.client_spend = _spend(re.sub(r"\s*spent$", "", _text(spent)))
    found.client_rating = _sr_rating(_attr(tile, "total-feedback"))
    tier = _attr(tile, "proposals-tier")
    if tier is not None:
        found.proposal_count, found.proposal_count_exact = _band(_text(tier), warnings)
    return found


def _feed_tile(tile: Tag, today: date, warnings: list[str], host: str) -> _Tile:
    heading = _attr(tile, "job-title")
    anchor = heading.find("a", href=True) if heading is not None else None
    anchor = anchor if isinstance(anchor, Tag) else None
    url = _href(anchor)
    if url is None:
        marker = tile.find(None, {"data-ev-opening_uid": True})
        uid = marker.get("data-ev-opening_uid") if isinstance(marker, Tag) else None
        if isinstance(uid, str) and _OPENING_UID.fullmatch(uid):
            # Other tile markups link the same opening as ~02<uid>.
            url = f"https://{host}/jobs/~02{uid}"
    found = _Tile(title=_text(heading, " ") or None, url=url)
    # The direct child holding the title also holds the posted and budget labels.
    # Reading labels only there keeps description prose from posing as metadata.
    header: Tag | None = heading
    while header is not None and header.parent is not tile:
        header = header.parent if isinstance(header.parent, Tag) else None
    if header is not None:
        for span in header.find_all("span"):
            if span.find_parent(attrs={"data-test": "job-title"}) is not None:
                continue
            label = span.get_text(" ", strip=True)
            if label.startswith("Posted "):
                found.posted_on = _posted_on(label, today) or found.posted_on
            if label.startswith(("Hourly", "Fixed-price")):
                found.job_type, found.budget_min, found.budget_max = _budget_label(
                    label, None
                )
    for footer in tile.find_all("div", recursive=False):
        if (
            footer is header
            or footer.get("data-test") == "chip-group"
            or "job-tile-actions" in (footer.get("class") or [])
        ):
            continue
        for span in footer.find_all("span"):
            label = span.get_text(" ", strip=True)
            if label == "Payment unverified":
                found.payment_verified = False
            elif label == "Payment verified" and found.payment_verified is None:
                found.payment_verified = True
        rating = _attr(footer, "rating-minimal")
        if rating is not None and re.fullmatch(r"[0-5](?:\.\d+)?", _text(rating, " ")):
            found.client_rating = _rating(_text(rating, " "))
    tier = _attr(tile, "proposals-tier")
    if tier is not None:
        found.proposal_count, found.proposal_count_exact = _band(_text(tier), warnings)
    return found


def _tiles(soup: BeautifulSoup) -> list[tuple[str, Tag]]:
    found: list[tuple[str, Tag]] = []
    for tile in soup.find_all(
        "section",
        class_=lambda c: (
            bool(c) and "air3-card-section" in c and "air3-card-hover" in c
        ),
    ):
        found.append(("legacy", tile))
    for tile in soup.find_all("article", attrs={"data-test": "JobTile"}):
        found.append(("article", tile))
    for tile in soup.find_all("div", attrs={"data-test": "job-tile"}):
        found.append(("feed", tile))
    return found


def parse_search_page(
    html: str, job_hosts: Sequence[str], *, today: date
) -> ParsedPaste:
    """Every job tile in a copied search-results page (module docstring)."""
    warnings: list[str] = []
    if not html or not html.strip() or not job_hosts:
        return ParsedPaste(jobs=(), skipped=0, warnings=())
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - a parse failure is reported, never raised
        return ParsedPaste(jobs=(), skipped=0, warnings=("The page could not be read",))
    jobs: dict[str, PastedJob] = {}
    skipped = 0
    for markup, tile in _tiles(soup):
        if markup == "legacy":
            found = _legacy_tile(tile, today, warnings)
        elif markup == "article":
            found = _article_tile(tile, today, warnings)
        else:
            found = _feed_tile(tile, today, warnings, job_hosts[0])
        if found.title is None and found.url is None:
            continue
        link = posting_link(found.url, job_hosts)
        if link is None or found.title is None:
            skipped += 1
            warnings.append(
                f"Skipped a job without a posting link: {found.title or found.url}"
                if link is None
                else f"Skipped a job without a title: {link[0]}"
            )
            continue
        url, posting_id = link
        # The same posting twice in one page: the first tile stands.
        jobs.setdefault(
            posting_id,
            PastedJob(
                posting_id=posting_id,
                posting_url=url,
                title=found.title,
                job_type=found.job_type,
                budget_min=found.budget_min,
                budget_max=found.budget_max,
                proposal_count=found.proposal_count,
                proposal_count_exact=found.proposal_count_exact,
                posted_on=found.posted_on,
                client_payment_verified=found.payment_verified,
                client_spend=found.client_spend,
                client_rating=found.client_rating,
            ),
        )
    return ParsedPaste(
        jobs=tuple(jobs.values()), skipped=skipped, warnings=tuple(warnings)
    )


def proposal_band(text: str) -> int | None:
    """The proposal band in pasted posting text as its upper bound, else ``None``:
    "Less than 5" is 4, "20 to 50" is 50, "50+" is 51."""
    match = _POSTING_BAND.search(text)
    if match is None:
        return None
    if match.group("lt") is not None:
        return max(int(match.group("lt")) - 1, 0)
    if match.group("hi") is not None:
        return int(match.group("hi"))
    return int(match.group("plus")) + 1


def parse_posting(url: str, text: str, job_hosts: Sequence[str]) -> ParsedPaste:
    """One full posting from its URL and copied page text (module docstring)."""
    link = posting_link(url, job_hosts)
    body = text.strip() if isinstance(text, str) else ""
    if link is None:
        return ParsedPaste(
            jobs=(),
            skipped=1,
            warnings=("The link is not a job posting on a job host",),
        )
    title = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not title:
        return ParsedPaste(jobs=(), skipped=1, warnings=("The posting text is empty",))
    if len(body) > POSTING_LIMIT:
        return ParsedPaste(
            jobs=(),
            skipped=1,
            warnings=("The posting text exceeds 16,000 characters",),
        )
    count = proposal_band(body)
    posting_url, posting_id = link
    return ParsedPaste(
        jobs=(
            PastedJob(
                posting_id=posting_id,
                posting_url=posting_url,
                title=title[:512],
                description=body,
                proposal_count=count,
                proposal_count_exact=None if count is None else False,
            ),
        ),
        skipped=0,
        warnings=(),
    )
