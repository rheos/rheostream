"""The engine pool: one ``Engine`` per ``database_name``, bounded two ways.

The cache holds **engines only**, keyed by the database name the caller read from the
registry row. It never caches a ``workspace_id -> database_name`` mapping: that read
happens in ``routing.route()`` on every call, so a renamed or re-pointed workspace can
never be served from a stale decision.

Two bounds, because a pool count is not the scarce resource — connections are:

* **Idle close** (``storage.pool_idle_close_seconds``). An engine untouched for longer
  than the window is disposed on the next call, **unless it still has a connection
  checked out** — see ``_expire_idle``, which is where the idle timer's own limit is
  written down. This is the bound that normally binds, and it is what keeps a caller
  that walks many workspaces from evicting the engine it is about to need: an engine
  goes away because nobody wanted it, not because someone else did.
* **Count cap** (``storage.pool_cache_size``) as the hard ceiling, least-recently-used
  first. It exists so open connections stay bounded even when every workspace is hot;
  ``pool_cache_size * storage.pool_max_connections`` is what the cached engines can
  open, and ``reserved_connections`` is added on top for the engines this process holds
  outside the cache. Both the core and the worker are separate processes each holding
  their own; ``rheo doctor`` reports the total against the cluster's own
  ``max_connections``.

**That total is the pooled figure and not a ceiling**, which is why it is called
:attr:`EnginePool.pooled_connections` rather than a worst case. Two things this process
can hold sit outside it, and both are named at the property and in ``rheo doctor``'s
own detail rather than left in a comment: the backend's unpooled maintenance engine,
and a connection still checked out from an engine the count cap evicted, which
``dispose()`` detaches rather than closes.
"""

import re
import threading
import time
from collections import OrderedDict
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
        (in ``PostgresBackend`` that is ``control_engine``, whose own ``pool_size`` is
        ``storage.pool_max_connections``). Change what reserves them and this value has
        to change with it — nothing here will notice on its own.
        """
        return self._reserved_connections

    @property
    def pooled_connections(self) -> int:
        """Connections this process's **pools** open if every cached engine fills.

        ``cache_size * pool_size + reserved_connections``. Reported by ``rheo doctor``
        so an operator can compare it with the cluster's own ``max_connections``,
        remembering that core and worker each hold one. The reservation is a value the
        pool is handed rather than one it derives — see ``reserved_connections``.

        **Named for what it counts, because it is not a ceiling.** It was called
        ``worst_case_connections`` and claimed to be one (issue #62); two connections
        this process can hold are outside the arithmetic, and neither is derivable
        here:

        * ``PostgresBackend._maintenance_engine``, built with ``poolclass=NullPool``.
          It is bounded by how many maintenance calls run at once rather than by a
          pool size, so no term of this sum can carry it. Real exposure is small —
          provisioning, migration and ``rheo doctor`` use it serially — so it is a
          small unpooled addition rather than an unbounded one, and saying which is
          the difference between a number an operator can act on and a warning.
        * a connection still checked out from an engine that was **evicted by the
          count cap**, or by ``dispose_all``: ``dispose()`` closes idle connections
          and *detaches* checked-out ones, and a detached connection stays open until
          its holder returns it, counted by nothing here. The idle sweep no longer
          reaches this state — ``_expire_idle`` skips an engine in use — but the
          count cap's own eviction in :meth:`engine_for` does not consult that, and
          bounding it there would mean either exceeding the cap or refusing a caller,
          which is a larger decision than this figure.

        ``rheo doctor``'s connection-budget check prints both omissions in its detail
        string, because an operator comparing this number against ``max_connections``
        is the one person who needs to know what it leaves out.
        """
        return self._cache_size * self._pool_size + self._reserved_connections

    def _expire_idle(self, now: float) -> list[Engine]:
        """Engines untouched for longer than the idle window **and not in use**.

        Caller holds the lock.

        ``_last_used`` is stamped in :meth:`engine_for`, at hand-out, and nothing
        stamps it again when a connection comes back — so a caller holding a
        connection for longer than ``idle_close_seconds`` has its engine's idle timer
        running while it is still working. Without the check below that engine is
        selected here and ``dispose()``d by the caller, and ``dispose()`` **detaches**
        a checked-out connection rather than closing it: the socket stays open until
        its holder returns it, outside every count this class reports. That is not an
        exotic edge — it is the ordinary consequence of a slow caller.

        The remedy is to skip expiry while the engine is genuinely in use, not to
        stamp ``_last_used`` on return: a connection that has not been returned fires
        no return-time stamp, so that fix cannot reach the case it is for. A skipped
        engine keeps its ``_engines``/``_last_used`` entries untouched and is
        re-examined by the next sweep, so it becomes eligible again the moment its
        last connection is returned. Skipping costs nothing the idle window was
        protecting: an engine with a connection checked out is by definition not idle.

        ``QueuePool.checkedout()`` is SQLAlchemy's own count of connections handed out
        and not yet returned. It is read under the lock and is a hint the moment it is
        read — another thread may check one out immediately afterwards — which is why
        this narrows the window rather than closing it, and why the figure ``rheo
        doctor`` reports names the detached-connection residual rather than claiming
        it away.

        **The ``isinstance`` is a type narrowing, not a branch with two live arms.**
        ``checkedout`` is declared on ``QueuePool`` rather than on ``Pool``, and every
        engine this class builds is a ``QueuePool``: :meth:`engine_for` passes
        ``pool_size`` and ``max_overflow``, which are that class's own arguments. The
        other arm is the behaviour this method had before the check existed, and it is
        pinned rather than trusted — ``tests/test_engine_pool.py``'s in-use case reads
        ``engine.pool.checkedout()`` off a real engine as its own positive control, so
        a pool class that lost the method fails there rather than silently expiring a
        live engine here.
        """
        stale = [
            name
            for name, used in self._last_used.items()
            if now - used > self._idle_close_seconds
        ]
        expired = []
        for name in stale:
            engine = self._engines.get(name)
            if engine is not None:
                pool = engine.pool
                if isinstance(pool, QueuePool) and pool.checkedout():
                    continue
            self._engines.pop(name, None)
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
            if engine is None:
                engine = create_engine(
                    self._cluster_url.set(database=name),
                    pool_size=self._pool_size,
                    max_overflow=0,
                    pool_pre_ping=True,
                )
                self._engines[name] = engine
                while len(self._engines) > self._cache_size:
                    evicted_name, stale = self._engines.popitem(last=False)
                    self._last_used.pop(evicted_name, None)
                    discarded.append(stale)
            else:
                self._engines.move_to_end(name)
            self._last_used[name] = now
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
        """Dispose every engine past the idle window that is not in use; returns how
        many went.

        ``engine_for`` already does this on the way in. This is the same sweep for a
        caller that wants it without asking for an engine — a worker between passes,
        or ``rheo doctor``. A zero is therefore not evidence that nothing was idle:
        an engine with a connection still checked out is skipped and counted by
        neither, per ``_expire_idle``.
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
