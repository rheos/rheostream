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
  pay, and so is one beside revenue, raised, funding, valuation, sales, turnover,
  profit, ARR, MRR, GMV or ad spend. So is a client's history on a pasted page
  ("$8.4K total spent", "avg hourly rate paid"), and nothing after "About the
  client" is read at all. A ``$`` closing a ``{...}`` is a variable, not money.
- A range ("$25-40/hr", "$500 to $800", "$2-3k", "$500-1k") keeps both ends; a
  second number below the first is not a range end.

Only the amount's own clause before it and its own sentence after it are read,
so one sentence never lends its unit or its words to another's amount. The unit
comes from the words right after the amount: an hourly rate (``/hr``, ``per
hour``, ``an hour``) is ``hour``; a price per anything else that is not a span of
time (``per project``, ``/video``, ``each``, or "per video $30") is ``item``; per
day, week, month or year leaves the unit unknown. Without one of those, a nearby
"fixed", "flat", "total" or "budget" makes it ``total``, and anything else leaves
it unknown.

When several amounts qualify, the one that reads most like the job's own price
wins: one beside "budget", "fixed price", "price", "rate" or "total", then one
with an hourly or per-item unit, then one beside a weaker pay word ("pay", "earn",
"offer"), then the rest; a per-month price ("users pay $4.99 per month") ranks
last. Among equals the first wins. A text that states no amount gives ``None``:
nothing is inferred from silence.
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
# "25-40 USD": a range whose currency follows both numbers.
_SUFFIXED_RANGE: Final = re.compile(
    r"(?<![\w$.,])"
    + _NUMBER.format(n="n", d="d")
    + _THOUSANDS.format(k="k")
    + r"\s*(?:-|–|—|to)\s*"
    + _NUMBER.format(n="n2", d="d2")
    + _THOUSANDS.format(k="k2")
    + r"\s?(?:USD|dollars)\b",
    re.IGNORECASE,
)
_RANGE_TAIL: Final = re.compile(
    r"\s*(?:-|–|—|to)\s*(?:US\$|\$|USD\s?)?\s?"
    + _NUMBER.format(n="n", d="d")
    + _THOUSANDS.format(k="k")
)
_LARGE: Final = re.compile(r"\s?(?:mm|bn|m|b|million|billion)\b", re.IGNORECASE)
# Business size introduced before an amount ("raised $2M", "revenue of $500k")...
_NOT_PAY: Final = re.compile(
    r"\b(?:revenue|raised|raising|funding|valuation|valued|turnover|profit|"
    r"sales of|annual sales|arr|mrr|gmv|ad\s?spend|spend on ads)\b",
    re.IGNORECASE,
)
# ...or named straight after it ("$10k MRR", "$1,000,000 in sales"), so "$500
# for a sales landing page" stays a budget.
_NOT_PAY_AFTER: Final = re.compile(
    r"\s*(?:(?:a|per)\s+(?:year|month)\s+)?(?:(?:in|of)\s+)?"
    r"(?:revenue|sales|turnover|profit|arr|mrr|gmv|ad\s?spend)\b",
    re.IGNORECASE,
)
# A client's history on a pasted page ("$8.4K total spent", "$32.50 /hr avg hourly
# rate paid") is not this job's pay.
_HISTORY: Final = re.compile(
    r"\b(?:spent|rate paid|avg|average|paid out|hire rate|hires|jobs posted)\b",
    re.IGNORECASE,
)
# Where a pasted page's client panel starts; nothing after it is read.
_CLIENT_PANEL: Final = re.compile(r"\babout the client\b", re.IGNORECASE)
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
# "Price per video $30": the unit named just before the amount. Per hour and
# per day, week, month or year before it are read as such, never as an item.
_HOUR_BEFORE: Final = re.compile(r"\bper\s+(?:hour|hr)\s*:?\s*$", re.IGNORECASE)
_SPAN_BEFORE: Final = re.compile(
    r"\bper\s+(?:day|week|month|year|annum)\s*:?\s*$", re.IGNORECASE
)
_ITEM_BEFORE: Final = re.compile(r"\bper\s+[a-z][a-z-]*\s*:?\s*$", re.IGNORECASE)
# Words that say an amount is the job's price; the strongest hint.
_PRICE_WORDS: Final = re.compile(
    r"\b(?:fixed[\s-]?price|fixed|flat|total|budget|price|priced|rate|"
    r"compensation|salary)\b",
    re.IGNORECASE,
)
_TOTAL: Final = re.compile(
    r"\b(?:fixed[\s-]?price|fixed|flat|total|budget)\b", re.IGNORECASE
)
# Words that say an amount is paid, more weakly ("users pay", "earn").
_PAY: Final = re.compile(
    r"\b(?:pay|paying|paid|offer|offering|fee|earn|earns)\b", re.IGNORECASE
)
# Where a clause ends, so the words around an amount are read only from its own.
_CLAUSE_END: Final = re.compile(r"[.;!?\n]\s|\n|\band\b|\bbut\b|,\s", re.IGNORECASE)
_SENTENCE_END: Final = re.compile(r"[.;!?](?:\s|$)|\n|,\s")
# How far around an amount its context is read, in characters.
_BEFORE: Final = 40
_AFTER: Final = 30
# Above this an amount is not a freelance price at all.
_CEILING: Final = Decimal(10_000_000)
# How strongly an amount reads as the job's own price; the strongest wins.
_PRICE_HINT: Final = 3
_UNIT_HINT: Final = 2
_PAY_HINT: Final = 1


@dataclass(frozen=True)
class StatedPrice:
    low: Decimal
    high: Decimal | None
    unit: PriceUnit | None


@dataclass(frozen=True)
class _Candidate:
    start: int
    price: StatedPrice
    score: int


def _value(number: str, decimals: str | None) -> Decimal | None:
    try:
        return Decimal(number.replace(",", "") + (f".{decimals}" if decimals else ""))
    except InvalidOperation:
        return None


def _clause(text: str, start: int) -> str:
    """The words before ``start`` in the amount's own clause: "We raised $2M and
    pay $30/hr" reads only "pay " before the rate, never the funding."""
    before = text[max(0, start - _BEFORE) : start]
    cut = max((m.end() for m in _CLAUSE_END.finditer(before)), default=0)
    return before[cut:]


def _following(text: str, end: int) -> str:
    """The words after an amount up to the end of its sentence, so "$30. 20
    videos. Total budget $600." never lends "total" to the $30."""
    after = text[end : end + _AFTER]
    stop = _SENTENCE_END.search(after)
    return after if stop is None else after[: stop.start()]


def _unit(before: str, after: str) -> tuple[PriceUnit | None, bool]:
    """The unit, and whether the amount is per day, week, month or year."""
    if _HOUR.match(after) or _HOUR_BEFORE.search(before):
        return "hour", False
    if _SPAN.match(after) or _SPAN_BEFORE.search(before):
        return None, True
    if _ITEM.match(after) or _ITEM_BEFORE.search(before):
        return "item", False
    if _TOTAL.search(after) or _TOTAL.search(before):
        return "total", False
    return None, False


def _range(
    text: str, match: re.Match[str], low: Decimal
) -> tuple[Decimal, Decimal | None, int]:
    """The low end, the high end if the amount opens a range, and where it ends.
    "$2-3k" puts the thousands on both ends, "$500-1k" only on the second."""
    end = match.end()
    if match["k"]:
        low *= 1000
    tail = _RANGE_TAIL.match(text, end)
    if tail is None:
        return low, None, end
    top = _value(tail["n"], tail["d"])
    if top is None:
        return low, None, end
    if tail["k"]:
        top *= 1000
    lows = [low * 1000, low] if tail["k"] and not match["k"] else [low]
    for start in lows:
        if top > start:
            return start, top, tail.end()
    return low, None, end


def _suffixed_range(
    match: re.Match[str], value: Decimal
) -> tuple[Decimal, Decimal, int] | None:
    """Both ends of "25-40 USD", with the thousands as ``_range`` places them."""
    top = _value(match["n2"], match["d2"])
    if top is None:
        return None
    low = value * 1000 if match["k"] else value
    if match["k2"]:
        top *= 1000
    lows = [low * 1000, low] if match["k2"] and not match["k"] else [low]
    for start in lows:
        if top > start:
            return start, top, match.end()
    return None


def _candidate(text: str, match: re.Match[str]) -> _Candidate | None:
    value = _value(match["n"], match["d"])
    if value is None or value <= 0:
        return None
    if text[match.end() : match.end() + 1] == "}":
        return None  # a shell or template variable ("{print $1}"), not money
    high: Decimal | None
    if match.groupdict().get("n2") is not None:
        found = _suffixed_range(match, value)
        if found is None:
            return None
        low, high, end = found
    else:
        low, high, end = _range(text, match, value)
    if _LARGE.match(text, end):
        return None
    before = _clause(text, match.start())
    after = _following(text, end)
    if any(p.search(before) or p.search(after) for p in (_NOT_PAY, _HISTORY)):
        return None
    if _NOT_PAY_AFTER.match(text, end):
        return None
    if (high or low) > _CEILING:
        return None
    unit, per_span = _unit(before, after)
    if per_span:
        # A monthly price is usually the product's ("users pay $4.99 per
        # month"), not the job's; it is kept only when nothing else is stated.
        score = 0
    elif _PRICE_WORDS.search(before) or _PRICE_WORDS.search(after):
        score = _PRICE_HINT
    elif unit in ("hour", "item"):
        score = _UNIT_HINT
    elif _PAY.search(before):
        score = _PAY_HINT
    else:
        score = 0
    return _Candidate(match.start(), StatedPrice(low, high, unit), score)


def stated_price(text: str | None) -> StatedPrice | None:
    """The price ``text`` states, or ``None`` when it states none."""
    if not text:
        return None
    panel = _CLIENT_PANEL.search(text)
    if panel is not None:
        text = text[: panel.start()]
    taken: list[tuple[int, int]] = []
    found: list[_Candidate] = []
    for pattern in (_SUFFIXED_RANGE, _PREFIXED, _SUFFIXED):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in taken):
                continue
            taken.append(match.span())
            candidate = _candidate(text, match)
            if candidate is not None:
                found.append(candidate)
    if not found:
        return None
    best = max(c.score for c in found)
    return min((c for c in found if c.score == best), key=lambda c: c.start).price
