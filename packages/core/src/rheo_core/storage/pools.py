"""The engine pool: one ``Engine`` per ``database_name``, bounded two ways.

The cache holds **engines only**, keyed by the database name the caller read from the
registry row. It never caches a ``workspace_id -> database_name`` mapping: that read
happens in ``routing.route()`` on every call, so a renamed or re-pointed workspace can
never be served from a stale decision.

Two bounds, because a pool count is not the scarce resource — connections are:

* **Idle close** (``storage.pool_idle_close_seconds``). An engine untouched for longer
  than the window is disposed on the next call. This is the bound that normally binds,
  and it is what keeps a caller that walks many workspaces from evicting the engine it
  is about to need: an engine goes away because nobody wanted it, not because someone
  else did.
* **Count cap** (``storage.pool_cache_size``) as the hard ceiling, least-recently-used
  first. It exists so open connections stay bounded even when every workspace is hot;
  ``pool_cache_size * storage.pool_max_connections`` is the process's worst-case
  connection count against the cluster, and both the core and the worker are separate
  processes each holding their own.
"""

import re
import threading
import time
from collections import OrderedDict
from typing import Final

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

_DATABASE_NAME: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def check_database_name(name: str) -> str:
    """Refuse anything that is not a lowercase identifier of at most 63 characters.

    Every database name this package interpolates into DDL (``CREATE DATABASE``,
    ``DROP DATABASE``) passes through here first, and is quoted besides.
    """
    if not isinstance(name, str) or not _DATABASE_NAME.fullmatch(name):
        raise ValueError("database names are lowercase identifiers of at most 63 chars")
    return name


class EnginePool:
    """Engines keyed by database name, on one cluster URL, idle-closed and capped."""

    def __init__(
        self,
        cluster_url: URL,
        *,
        cache_size: int,
        pool_size: int,
        idle_close_seconds: float,
    ) -> None:
        if cache_size < 1 or pool_size < 1:
            raise ValueError("cache_size and pool_size must be positive")
        if idle_close_seconds <= 0:
            raise ValueError("idle_close_seconds must be positive")
        self._cluster_url = cluster_url
        self._cache_size = cache_size
        self._pool_size = pool_size
        self._idle_close_seconds = idle_close_seconds
        self._engines: OrderedDict[str, Engine] = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def worst_case_connections(self) -> int:
        """Connections this process opens if every cached engine fills its pool.

        Reported by ``rheo doctor`` so an operator can compare it with the cluster's
        own ``max_connections``, remembering that core and worker each hold one.
        """
        return self._cache_size * self._pool_size

    def _expire_idle(self, now: float) -> list[Engine]:
        """Engines untouched for longer than the idle window. Caller holds the lock."""
        stale = [
            name
            for name, used in self._last_used.items()
            if now - used > self._idle_close_seconds
        ]
        expired = []
        for name in stale:
            engine = self._engines.pop(name, None)
            self._last_used.pop(name, None)
            if engine is not None:
                expired.append(engine)
        return expired

    @property
    def cluster_url(self) -> URL:
        """The cluster URL every engine is derived from (password hidden in ``str``)."""
        return self._cluster_url

    def engine_for(self, database_name: str) -> Engine:
        """The engine for ``database_name``, created on first use and kept while hot."""
        name = check_database_name(database_name)
        now = time.monotonic()
        with self._lock:
            # Idle expiry runs first, so a caller walking many workspaces reclaims the
            # engines nobody wanted before it ever reaches the count cap.
            discarded = self._expire_idle(now)
            engine = self._engines.get(name)
            if engine is not None:
                self._engines.move_to_end(name)
                self._last_used[name] = now
                for stale in discarded:
                    stale.dispose()
                return engine
            engine = create_engine(
                self._cluster_url.set(database=name),
                pool_size=self._pool_size,
                max_overflow=0,
                pool_pre_ping=True,
            )
            self._engines[name] = engine
            self._last_used[name] = now
            while len(self._engines) > self._cache_size:
                evicted_name, stale = self._engines.popitem(last=False)
                self._last_used.pop(evicted_name, None)
                discarded.append(stale)
        for stale in discarded:
            stale.dispose()
        return engine

    def cached(self) -> tuple[str, ...]:
        """The database names currently holding an engine, least recent first."""
        with self._lock:
            return tuple(self._engines)

    def close_idle(self) -> int:
        """Dispose every engine past the idle window; returns how many went.

        ``engine_for`` already does this on the way in. This is the same sweep for a
        caller that wants it without asking for an engine — a worker between passes,
        or ``rheo doctor``.
        """
        with self._lock:
            discarded = self._expire_idle(time.monotonic())
        for stale in discarded:
            stale.dispose()
        return len(discarded)

    def dispose_all(self) -> None:
        with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
            self._last_used.clear()
        for engine in engines:
            engine.dispose()
