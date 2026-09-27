"""The timestamp-preservation rule (spec § Data Models, the timestamp rule; FR 3).

:func:`parse_source_time` is the one function every predecessor timestamp passes
through, so the extractor never re-derives the rule per source class. Accepted, in
this order:

1. ISO 8601 extended date-time with a UTC offset or ``Z``: normalized to UTC.
2. ISO 8601 extended date-time with no offset, or SQLite's
   ``YYYY-MM-DD HH:MM:SS[.fff]``: taken as UTC, because SQLite's
   ``CURRENT_TIMESTAMP`` and ``datetime('now')`` write UTC.
3. A date alone, exactly ``YYYY-MM-DD``: ``00:00:00`` UTC, with the property loss
   :data:`TIME_OF_DAY_UNKNOWN`.
4. An integer or float: Unix epoch seconds below :data:`EPOCH_MILLISECONDS_FLOOR`,
   milliseconds from it upward.

A date-time needs at least hours and minutes (``YYYY-MM-DDTHH:MM``); the basic
format (``20260101``), week dates and an hour alone are not among the accepted
forms.

Anything else (null, empty, unparseable text, a ``bool``, a non-finite number, a
value before 1970-01-01) is :data:`TIMESTAMP_UNPARSEABLE`; a value later than the
extraction instant plus :data:`FUTURE_TOLERANCE` is :data:`TIMESTAMP_IN_FUTURE`, so a
clock-skewed row is never recorded as the future.

The function never raises on a source value. A value outside what ``datetime`` can
represent is still placed on the right side of the rule: below the range is before
1970 (unparseable), above it is past any extraction instant (in the future).
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

TIMESTAMP_UNPARSEABLE: Final = "timestamp_unparseable"
TIMESTAMP_IN_FUTURE: Final = "timestamp_in_future"
TIME_OF_DAY_UNKNOWN: Final = "time_of_day_unknown"

#: A number at or above this is epoch milliseconds, below it epoch seconds. 10**11
#: seconds is the year 5138; 10**11 milliseconds is 1973-03-03.
EPOCH_MILLISECONDS_FLOOR: Final = 10**11
FUTURE_TOLERANCE: Final = timedelta(days=1)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_DATE_ONLY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_TIME: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}(?::?\d{2})?)?"
)


@dataclass(frozen=True)
class SourceTime:
    """An accepted timestamp, always timezone-aware UTC, with its property losses."""

    value: datetime
    losses: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnresolvedTime:
    """A timestamp the rule refuses; ``reason_code`` is one of the two codes above."""

    reason_code: str


_UNPARSEABLE: Final = UnresolvedTime(TIMESTAMP_UNPARSEABLE)
_IN_FUTURE: Final = UnresolvedTime(TIMESTAMP_IN_FUTURE)

_Outcome = SourceTime | UnresolvedTime


def _from_epoch(number: int | float) -> _Outcome:
    if isinstance(number, float) and not math.isfinite(number):
        return _UNPARSEABLE
    if number < 0:
        return _UNPARSEABLE  # before 1970, however large
    try:
        if number < EPOCH_MILLISECONDS_FLOOR:
            offset = timedelta(seconds=number)
        else:
            offset = timedelta(milliseconds=number)
        return SourceTime(_EPOCH + offset)
    except OverflowError:
        return _IN_FUTURE  # non-negative and past datetime's range


def _from_text(text: str) -> _Outcome:
    stripped = text.strip()
    if _DATE_ONLY.fullmatch(stripped):
        try:
            day = date.fromisoformat(stripped)
        except ValueError:
            return _UNPARSEABLE
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return SourceTime(midnight, (TIME_OF_DAY_UNKNOWN,))
    if not _DATE_TIME.fullmatch(stripped):
        return _UNPARSEABLE
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError:
        return _UNPARSEABLE  # out-of-range fields or offset
    if parsed.tzinfo is None:
        return SourceTime(parsed.replace(tzinfo=UTC))
    try:
        return SourceTime(parsed.astimezone(UTC))
    except OverflowError:
        # The offset pushed the instant past year 1 or year 9999.
        return _UNPARSEABLE if parsed.year < 1970 else _IN_FUTURE


def _future_limit(extracted_at: datetime) -> datetime:
    try:
        return extracted_at.astimezone(UTC) + FUTURE_TOLERANCE
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


def parse_source_time(raw: object, *, extracted_at: datetime) -> _Outcome:
    """Apply the timestamp rule to one source value, relative to ``extracted_at``.

    Every ``raw`` returns a :class:`SourceTime` or an :class:`UnresolvedTime`.
    ``extracted_at`` is the extraction instant and must be timezone-aware; a naive
    one is a programming error, not a data outcome, so it raises.
    """
    if extracted_at.tzinfo is None:
        raise ValueError("extracted_at must be timezone-aware")
    outcome: _Outcome
    if isinstance(raw, bool):  # a bool is an int subclass, never a timestamp
        outcome = _UNPARSEABLE
    elif isinstance(raw, int | float):
        outcome = _from_epoch(raw)
    elif isinstance(raw, str):
        outcome = _from_text(raw)
    else:
        outcome = _UNPARSEABLE
    if isinstance(outcome, UnresolvedTime):
        return outcome
    if outcome.value < _EPOCH:
        return _UNPARSEABLE
    if outcome.value > _future_limit(extracted_at):
        return _IN_FUTURE
    return outcome
