"""The engine pool: one ``Engine`` per ``database_name``, bounded two ways.

The cache holds **engines only**, keyed by the database name the caller read from the
registry row. It never caches a ``workspace_id -> database_name`` mapping: that read
happens in ``routing.route()`` on every call, so a renamed or re-pointed workspace can
never be served from a stale decision.

Two bounds, because a pool count is not the scarce resource — connections are:

* **Idle close** (``storage.pool_idle_close_seconds``). An engine untouched for longer
  than the window is disposed on the next call, **unless it is busy** — a connection
  still checked out, or a :meth:`pin` / :meth:`acquire` lease that has not returned.
  See ``_expire_idle``.
* **Count cap** (``storage.pool_cache_size``) as the hard ceiling on *idle* engines,
  least-recently-used first. A busy engine is not evicted: the cache may briefly
  exceed the cap until those connections return, then the next call evicts the unused
  extras. Evicting a busy engine would ``dispose()``-detach its connections and take
  them outside every count this class reports (issue #62).

``reserved_connections`` is added on top for the engines this process holds outside
the cache. Both the core and the worker are separate processes each holding their
own; ``rheo doctor`` reports the total against the cluster's own ``max_connections``.
"""

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import QueuePool

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
        reserved_connections: int,
    ) -> None:
        if cache_size < 1 or pool_size < 1:
            raise ValueError("cache_size and pool_size must be positive")
        if idle_close_seconds <= 0:
            raise ValueError("idle_close_seconds must be positive")
        if reserved_connections < 0:
            raise ValueError("reserved_connections must not be negative")
        self._cluster_url = cluster_url
        self._cache_size = cache_size
        self._pool_size = pool_size
        self._idle_close_seconds = idle_close_seconds
        self._reserved_connections = reserved_connections
        self._engines: OrderedDict[str, Engine] = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._pins: dict[int, int] = {}
        self._lock = threading.Lock()

    @property
    def cache_size(self) -> int:
        """The hard ceiling on cached engines (``storage.pool_cache_size``)."""
        return self._cache_size

    @property
    def pool_size(self) -> int:
        """Connections one cached engine may open (``storage.pool_max_connections``)."""
        return self._pool_size

    @property
    def reserved_connections(self) -> int:
        """Connections this process holds outside the cache, told to the pool.

        The pool cannot see them and cannot verify them: the caller that constructs the
        pool owns keeping this accurate to whatever actually reserves those connections
        (in ``PostgresBackend`` that is ``control_engine`` plus one serialized
        maintenance connection). Change what reserves them and this value has to
        change with it — nothing here will notice on its own.
        """
        return self._reserved_connections

    @property
    def pooled_connections(self) -> int:
        """Configured cache budget: ``cache_size * pool_size + reserved``.

        Reported by ``rheo doctor`` so an operator can compare it with the cluster's
        own ``max_connections``, remembering that core and worker each hold one.

        This is the number the cache is *sized* for. :attr:`held_connections` is what
        the process is holding right now; it matches this figure except while a busy
        engine has blocked count-cap eviction and the cache is briefly over the cap.
        """
        return self._cache_size * self._pool_size + self._reserved_connections

    @property
    def held_connections(self) -> int:
        """Connections the cached engines plus the reservation can open right now.

        ``len(_engines) * pool_size + reserved_connections``. Equals
        :attr:`pooled_connections` unless a busy engine forced the cache past
        ``cache_size``.
        """
        with self._lock:
            return len(self._engines) * self._pool_size + self._reserved_connections

    def _busy(self, engine: Engine) -> bool:
        """A pin is held, or SQLAlchemy still has a connection checked out.

        Caller holds the lock. ``QueuePool.checkedout()`` is a hint the moment it is
        read — another thread may check one out immediately afterwards — which is why
        :meth:`pin` / :meth:`acquire` exist: they close the handout window the hint
        cannot see (issue #62).
        """
        if self._pins.get(id(engine), 0) > 0:
            return True
        pool = engine.pool
        return isinstance(pool, QueuePool) and bool(pool.checkedout())

    def _expire_idle(self, now: float) -> list[Engine]:
        """Engines untouched for longer than the idle window **and not busy**.

        Caller holds the lock.

        ``_last_used`` is stamped in :meth:`engine_for`, at hand-out, and nothing
        stamps it again when a connection comes back — so a caller holding a
        connection for longer than ``idle_close_seconds`` has its engine's idle timer
        running while it is still working. Skipping a busy engine keeps its
        ``_engines``/``_last_used`` entries untouched; it becomes eligible again the
        moment the last pin drops and the last connection is returned.
        """
        stale = [
            name
            for name, used in self._last_used.items()
            if now - used > self._idle_close_seconds
        ]
        expired = []
        for name in stale:
            engine = self._engines.get(name)
            if engine is not None and self._busy(engine):
                continue
            self._engines.pop(name, None)
            self._last_used.pop(name, None)
            if engine is not None:
                expired.append(engine)
        return expired

    def _evict_unused_over_cap(self, *, protect: str) -> list[Engine]:
        """Pop unused LRU engines until the cache is at the cap, or every leftover
        engine is busy.

        Caller holds the lock. ``protect`` is the name just handed out — it is never
        evicted on this call, even with zero checkouts, because that is the engine
        the caller is about to use. Busy engines stay; the cache may exceed
        ``cache_size`` until they are free. That is the issue #62 choice: defer
        retirement rather than ``dispose()``-detach a live connection or refuse the
        caller.
        """
        discarded: list[Engine] = []
        while len(self._engines) > self._cache_size:
            victim_name = next(
                (
                    name
                    for name, engine in self._engines.items()
                    if name != protect and not self._busy(engine)
                ),
                None,
            )
            if victim_name is None:
                break
            engine = self._engines.pop(victim_name)
            self._last_used.pop(victim_name, None)
            discarded.append(engine)
        return discarded

    @property
    def cluster_url(self) -> URL:
        """The cluster URL every engine is derived from (password hidden in ``str``)."""
        return self._cluster_url

    def pin(self, engine: Engine) -> None:
        """Increment the busy count for a **currently cached** engine.

        Does not close the handout window by itself. Call :meth:`engine_for` with
        ``pin=True`` or :meth:`acquire` so the pin is taken under the same lock as
        the hand-out. This method refuses an engine the pool no longer tracks.
        """
        with self._lock:
            if engine not in self._engines.values():
                raise ValueError(
                    "pin() only accepts an engine this pool currently holds"
                )
            key = id(engine)
            self._pins[key] = self._pins.get(key, 0) + 1

    def unpin(self, engine: Engine) -> None:
        """Drop one :meth:`pin`. Extra unpins are ignored."""
        with self._lock:
            key = id(engine)
            count = self._pins.get(key, 0) - 1
            if count <= 0:
                self._pins.pop(key, None)
            else:
                self._pins[key] = count

    @contextmanager
    def acquire(self, database_name: str) -> Iterator[Engine]:
        """The engine for ``database_name``, pinned for the duration of the block."""
        engine = self.engine_for(database_name, pin=True)
        try:
            yield engine
        finally:
            self.unpin(engine)

    def engine_for(self, database_name: str, *, pin: bool = False) -> Engine:
        """The engine for ``database_name``, created on first use and kept while hot.

        ``pin=True`` is atomic with the hand-out: the engine is busy before the lock
        is released, so a concurrent sweep cannot treat a just-returned engine as idle.
        The matching :meth:`unpin` is the caller's (or :meth:`acquire`'s).
        """
        name = check_database_name(database_name)
        now = time.monotonic()
        with self._lock:
            # Idle expiry runs first, so a caller walking many workspaces reclaims the
            # engines nobody wanted before it ever reaches the count cap.
            discarded = self._expire_idle(now)
            engine = self._engines.get(name)
            if engine is None:
                engine = create_engine(
                    self._cluster_url.set(database=name),
                    pool_size=self._pool_size,
                    max_overflow=0,
                    pool_pre_ping=True,
                )
                self._engines[name] = engine
            else:
                self._engines.move_to_end(name)
            self._last_used[name] = now
            discarded.extend(self._evict_unused_over_cap(protect=name))
            if pin:
                key = id(engine)
                self._pins[key] = self._pins.get(key, 0) + 1
        # Both branches fall through to here: ``dispose`` closes sockets and can block,
        # so it never runs under ``self._lock``, where it would serialise every other
        # workspace's call behind one slow teardown.
        for stale in discarded:
            stale.dispose()
        return engine

    def cached(self) -> tuple[str, ...]:
        """The database names currently holding an engine, least recent first."""
        with self._lock:
            return tuple(self._engines)

    def close_idle(self) -> int:
        """Dispose every engine past the idle window that is not busy; returns how
        many went.

        ``engine_for`` already does this on the way in. This is the same sweep for a
        caller that wants it without asking for an engine — a worker between passes,
        or ``rheo doctor``. A zero is therefore not evidence that nothing was idle:
        a busy engine is skipped and counted by neither, per ``_expire_idle``.
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
            self._pins.clear()
        for engine in engines:
            engine.dispose()
