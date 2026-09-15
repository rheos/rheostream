"""The cancellation checkpoint a handler is given, and the two exceptions it raises.

One mechanism serving three jobs: :meth:`CancellationToken.checkpoint` extends the
lease (the heartbeat), re-reads ``cancel_requested`` (the cancellation), and detects a
lost lease — all from the one ``UPDATE ... RETURNING`` in
:func:`~rheo_core.work.jobs.extend_lease`.

**It runs in its own short transaction, never the handler's.** An extension written
inside the handler's transaction would be invisible to every other worker until that
transaction commits, which is the whole point of a heartbeat.

The cost is stated rather than hidden: a handler that checkpoints more often than
``heartbeat_seconds`` observes a cancellation up to that long late. The commit-point
predicate on the terminal writes is what bounds the window at the other end.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine

from rheo_core.work.jobs import extend_lease


class JobCancelled(Exception):
    """Raised inside a handler when the job it is running has been cancelled."""


class JobLeaseLost(Exception):
    """Raised inside a handler when the row is no longer this worker's to finish."""


class CancellationToken:
    """The handler's one way to ask whether it should still be running.

    **``heartbeat_seconds`` and ``clock`` are constructor parameters, not module
    lookups, and both are required.** The heartbeat cadence is declared by the worker
    loop, which is a later module than this one; a token that imported it would invert
    the build order and leave this package unbuildable on its own. ``clock`` is the
    loop's own controlled clock — the same callable the loop leases and finishes under
    — so this class never reads the process clock itself.
    """

    __slots__ = (
        "_clock",
        "_engine",
        "_extended_at",
        "_heartbeat_seconds",
        "_job_id",
        "_lease_seconds",
        "_owner",
    )

    def __init__(
        self,
        engine: Engine,
        *,
        job_id: UUID,
        owner: str,
        lease_seconds: int,
        heartbeat_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._engine = engine
        self._job_id = job_id
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._extended_at: datetime | None = None

    def checkpoint(self, now: datetime | None = None) -> None:
        """Extend the lease and observe the cancellation flag.

        ``now`` defaults to the captured clock, which is what lets a handler signature
        carry no clock of its own: without the default every handler would reach for
        the wall clock to satisfy a required parameter, while the loop around it leased
        and finished under an injected one. A test that wants a specific instant still
        passes ``now=`` and the token uses it.

        **Throttled against that ``now``, never the wall clock.** The token remembers
        the ``now`` of its last extension and returns without touching the database
        when the next call is less than ``heartbeat_seconds`` after it — otherwise a
        handler checkpointing in a tight loop issues one ``UPDATE`` per iteration. The
        last-extension time starts unset, so the **first** checkpoint after the acquire
        always round-trips and a handler that checkpoints once still sees a
        cancellation.
        """
        at = self._clock() if now is None else now
        last = self._extended_at
        if last is not None and at - last < timedelta(seconds=self._heartbeat_seconds):
            return
        with self._engine.begin() as conn:
            cancel_requested = extend_lease(
                conn,
                job_id=self._job_id,
                owner=self._owner,
                now=at,
                lease_seconds=self._lease_seconds,
            )
        if cancel_requested is None:
            raise JobLeaseLost(f"job {self._job_id} is no longer leased to this worker")
        self._extended_at = at
        if cancel_requested:
            raise JobCancelled(f"job {self._job_id} was cancelled")
