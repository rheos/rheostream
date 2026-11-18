"""A price the posting text states, for a job whose feed carries no budget (#503).

Upwork leaves the budget fields empty on most postings that still say what they pay
in the description ("Pay: $3 per project", "$50 fixed price for the first video").
This reads that amount so it can be stored beside, never in place of, an
upstream budget.

What counts as an amount is narrow on purpose:

- Only money that is marked as money: ``$``, ``US$``, ``USD`` before the number, or
  ``USD``/``dollars`` after it. A bare number ("5,000 subscribers", "20 minutes")
  is never a price.
- An amount in millions or billions ("$2M", "$1.5 billion") is company size, not
  pay, and so is one introduced by revenue, raised, funding, valuation, sales,
  turnover, profit, ARR, MRR, GMV or ad spend.
- A range ("$25-40/hr", "$500 to $800") keeps both ends; a second number below the
  first is not a range end.

The unit comes from the words right after the amount: an hourly rate (``/hr``,
``per hour``, ``an hour``) is ``hour``; a price per anything else that is not a span
of time (``per project``, ``/video``, ``each``) is ``item``; per day, week, month or
year leaves the unit unknown. Without one of those, a nearby "fixed", "flat",
"total" or "budget" makes it ``total``, and anything else leaves it unknown.

When several amounts qualify, the first one with a unit or a pay word ("pay",
"budget", "rate", "price", "offer", "fee", "compensation", "earn") near it wins,
else the first. A text that states no amount gives ``None``: nothing is inferred
from silence.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

PriceUnit = Literal["hour", "total", "item"]
UNITS: Final[tuple[PriceUnit, ...]] = ("hour", "total", "item")

_NUMBER: Final = r"(?P<{n}>\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.(?P<{d}>\d{{1,2}}))?"
_THOUSANDS: Final = r"(?P<{k}>\s?[kK]\b)?"
_PREFIXED: Final = re.compile(
    r"(?:US\$|\$|\bUSD\s?)\s?" + _NUMBER.format(n="n", d="d") + _THOUSANDS.format(k="k")
)
_SUFFIXED: Final = re.compile(
    r"(?<![\w$.,])"
    + _NUMBER.format(n="n", d="d")
    + _THOUSANDS.format(k="k")
    + r"\s?(?:USD|dollars)\b",
    re.IGNORECASE,
)
_RANGE_TAIL: Final = re.compile(
    r"\s*(?:-|–|—|to)\s*(?:US\$|\$|USD\s?)?\s?"
    + _NUMBER.format(n="n", d="d")
    + _THOUSANDS.format(k="k")
)
_LARGE: Final = re.compile(r"\s?(?:mm|bn|m|b|million|billion)\b", re.IGNORECASE)
_NOT_PAY: Final = re.compile(
    r"\b(?:revenue|raised|raising|funding|valuation|valued|sales|turnover|profit|"
    r"arr|mrr|gmv|ad\s?spend|spend on ads)\b",
    re.IGNORECASE,
)
_HOUR: Final = re.compile(
    r"\s*(?:/|per\b|an\b|a\b|each\b)?\s*(?:hour|hr|hrs|h)\b|\s*hourly\b",
    re.IGNORECASE,
)
_SPAN: Final = re.compile(
    r"\s*(?:/|per\b|a\b|an\b|each\b)\s*(?:day|week|month|year|annum|yr|mo|wk)\b",
    re.IGNORECASE,
)
_ITEM: Final = re.compile(
    r"\s*(?:/\s*|per\s+|each\b|apiece\b)(?:[a-z][a-z-]*)?", re.IGNORECASE
)
_TOTAL: Final = re.compile(
    r"\b(?:fixed[\s-]?price|fixed|flat|total|budget)\b", re.IGNORECASE
)
_PAY: Final = re.compile(
    r"\b(?:pay|paying|paid|budget|rate|price|priced|offer|offering|fee|"
    r"compensation|earn|salary)\b",
    re.IGNORECASE,
)
# Where a clause ends, so the words before an amount are read only from its own.
_CLAUSE_END: Final = re.compile(r"[.;!?\n]\s|\n|\band\b|\bbut\b|,\s", re.IGNORECASE)
# How far around an amount its context is read, in characters.
_BEFORE: Final = 40
_AFTER: Final = 30
# Above this an amount is not a freelance price at all.
_CEILING: Final = Decimal(10_000_000)


@dataclass(frozen=True)
class StatedPrice:
    low: Decimal
    high: Decimal | None
    unit: PriceUnit | None


@dataclass(frozen=True)
class _Candidate:
    start: int
    price: StatedPrice
    hinted: bool


def _value(number: str, decimals: str | None, thousands: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", "") + (f".{decimals}" if decimals else ""))
    except InvalidOperation:
        return None
    return value * 1000 if thousands else value


def _clause(text: str, start: int) -> str:
    """The words before ``start`` in the amount's own clause: "We raised $2M and
    pay $30/hr" reads only "pay " before the rate, never the funding."""
    before = text[max(0, start - _BEFORE) : start]
    cut = max((m.end() for m in _CLAUSE_END.finditer(before)), default=0)
    return before[cut:]


def _unit(text: str, start: int, end: int) -> PriceUnit | None:
    after = text[end : end + _AFTER]
    if _HOUR.match(after):
        return "hour"
    if _SPAN.match(after):
        return None
    if _ITEM.match(after):
        return "item"
    if _TOTAL.search(after) or _TOTAL.search(_clause(text, start)):
        return "total"
    return None


def _candidate(text: str, match: re.Match[str]) -> _Candidate | None:
    low = _value(match["n"], match["d"], match["k"])
    if low is None or low <= 0:
        return None
    end = match.end()
    high: Decimal | None = None
    tail = _RANGE_TAIL.match(text, end)
    if tail is not None:
        top = _value(tail["n"], tail["d"], tail["k"])
        if top is not None and tail["k"] and not match["k"] and low < 1000:
            # "$2-3k" puts the thousands on both ends.
            low *= 1000
        if top is not None and top > low:
            high, end = top, tail.end()
    if _LARGE.match(text, end):
        return None
    before = _clause(text, match.start())
    if _NOT_PAY.search(before):
        return None
    if (high or low) > _CEILING:
        return None
    unit = _unit(text, match.start(), end)
    hinted = unit in ("hour", "item") or _PAY.search(before) is not None
    return _Candidate(match.start(), StatedPrice(low, high, unit), hinted)


def stated_price(text: str | None) -> StatedPrice | None:
    """The price ``text`` states, or ``None`` when it states none."""
    if not text:
        return None
    seen: set[int] = set()
    found: list[_Candidate] = []
    for pattern in (_PREFIXED, _SUFFIXED):
        for match in pattern.finditer(text):
            if match.start() in seen:
                continue
            seen.add(match.start())
            candidate = _candidate(text, match)
            if candidate is not None:
                found.append(candidate)
    if not found:
        return None
    found.sort(key=lambda c: c.start)
    hinted = [c for c in found if c.hinted]
    return (hinted or found)[0].price
