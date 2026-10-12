"""The job feed collector.

``dependencies()`` is the collector's one test-double seam. Every handler reads it at
call time for its HTTP transport, address resolver, monotonic clock, sleep and wall
clock; nothing else injects doubles. ``use_dependencies`` swaps the process-wide
holder (not a context variable) so threads a test starts see the same doubles.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from rheo_core.connectors.egress import Clock, Resolver, default_resolver


@dataclass(frozen=True)
class Dependencies:
    transport: httpx.BaseTransport | None
    resolver: Resolver
    clock: Clock
    sleep: Callable[[float], None]
    now: Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


PRODUCTION = Dependencies(
    transport=None,
    resolver=default_resolver,
    clock=time.monotonic,
    sleep=time.sleep,
    now=_now,
)


class _Holder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current = PRODUCTION


_HOLDER = _Holder()


def dependencies() -> Dependencies:
    with _HOLDER.lock:
        return _HOLDER.current


@contextmanager
def use_dependencies(deps: Dependencies) -> Iterator[Dependencies]:
    """Install ``deps`` for the duration of the block, then restore the previous."""
    with _HOLDER.lock:
        previous, _HOLDER.current = _HOLDER.current, deps
    try:
        yield deps
    finally:
        with _HOLDER.lock:
            _HOLDER.current = previous
