"""The retry schedule, transcribed from ``docs/architecture/intake-and-events.md``
§ Retry.

One policy, deliberately. The same schedule governs jobs now and event deliveries when
that half lands, so the run that adds delivery adds a caller rather than a second
policy to keep in step with this one.

**Jitter is additive only.** ``[0, 0.1 x base)`` on top of the step, never subtracted:
the backoff floor has to hold, and a symmetric jitter would let the first retry land
earlier than the schedule says. The source is an injected ``random.Random`` so a test
can either seed it or pass ``None`` and get the bare schedule.
"""

import random
from datetime import timedelta
from typing import Final

BACKOFF_SECONDS: Final = (5, 20, 80, 320, 1280)
BACKOFF_CAP_SECONDS: Final = 3600


def backoff_for(attempts: int, *, jitter: random.Random | None) -> timedelta:
    """How long to wait before the attempt after ``attempts``.

    Indexed by ``attempts - 1``, so the first failed attempt waits the first step;
    past the end of the schedule every further attempt waits the cap. ``attempts``
    below one is clamped to the first step rather than refused — the caller reads it
    from the row, where it is at least one by the time any retry is being computed.
    """
    index = max(attempts, 1) - 1
    base = (
        BACKOFF_SECONDS[index] if index < len(BACKOFF_SECONDS) else BACKOFF_CAP_SECONDS
    )
    seconds = float(min(base, BACKOFF_CAP_SECONDS))
    if jitter is not None:
        seconds += jitter.uniform(0.0, 0.1 * seconds)
    return timedelta(seconds=seconds)
