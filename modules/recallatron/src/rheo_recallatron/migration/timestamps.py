"""The timestamp-preservation rule (spec § Data Models, the timestamp rule; FR 3).

:func:`parse_source_time` is the one function every predecessor timestamp passes
through, so the extractor never re-derives the rule per source class. Accepted, in
this order:

1. ISO 8601 with a UTC offset or ``Z``: normalized to UTC.
2. ISO 8601 date-time with no offset, or SQLite's ``YYYY-MM-DD HH:MM:SS[.fff]``: taken
   as UTC, because SQLite's ``CURRENT_TIMESTAMP`` and ``datetime('now')`` write UTC.
3. A date alone: ``00:00:00`` UTC, with the property loss :data:`TIME_OF_DAY_UNKNOWN`.
4. An integer or float: Unix epoch seconds below :data:`EPOCH_MILLISECONDS_FLOOR`,
   milliseconds from it upward.

Anything else (null, empty, unparseable text, a ``bool``, a non-finite number, a
value before 1970-01-01) is :data:`TIMESTAMP_UNPARSEABLE`; a value later than the
extraction instant plus :data:`FUTURE_TOLERANCE` is :data:`TIMESTAMP_IN_FUTURE`, so a
clock-skewed row is never recorded as the future.
"""

import math
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


@dataclass(frozen=True)
class SourceTime:
    """An accepted timestamp, always timezone-aware UTC, with its property losses."""

    value: datetime
    losses: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnresolvedTime:
    """A timestamp the rule refuses; ``reason_code`` is one of the two codes above."""

    reason_code: str


def _from_epoch(number: float) -> datetime | None:
    if not math.isfinite(number):
        return None
    seconds = number if number < EPOCH_MILLISECONDS_FLOOR else number / 1000
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _from_text(text: str) -> SourceTime | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        day = date.fromisoformat(stripped)
    except ValueError:
        pass
    else:
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return SourceTime(midnight, (TIME_OF_DAY_UNKNOWN,))
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return SourceTime(parsed.replace(tzinfo=UTC))
    return SourceTime(parsed.astimezone(UTC))


def parse_source_time(
    raw: object, *, extracted_at: datetime
) -> SourceTime | UnresolvedTime:
    """Apply the timestamp rule to one source value, relative to ``extracted_at``.

    ``extracted_at`` is the extraction instant and must be timezone-aware; a naive
    one is a programming error, not a data outcome, so it raises.
    """
    if extracted_at.tzinfo is None:
        raise ValueError("extracted_at must be timezone-aware")
    accepted: SourceTime | None
    if isinstance(raw, bool):  # a bool is an int subclass, never a timestamp
        accepted = None
    elif isinstance(raw, int | float):
        moment = _from_epoch(raw)
        accepted = None if moment is None else SourceTime(moment)
    elif isinstance(raw, str):
        accepted = _from_text(raw)
    else:
        accepted = None
    if accepted is None or accepted.value < _EPOCH:
        return UnresolvedTime(TIMESTAMP_UNPARSEABLE)
    if accepted.value > extracted_at.astimezone(UTC) + FUTURE_TOLERANCE:
        return UnresolvedTime(TIMESTAMP_IN_FUTURE)
    return accepted
