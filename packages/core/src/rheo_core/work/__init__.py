"""Durable execution: jobs, event deliveries, and schedules.

The tables landed in ``storage/work_tables.py``, created by the core chain's
``0002_durable_work`` revision. The **job** half lives here: the repository and its
lease in ``jobs.py``, the retry schedule in ``backoff.py``, the cancellation checkpoint
in ``cancellation.py``, the job-kind registry in ``kinds.py``, and the worker's own loop
— leasing, the retry budget, cancellation — in ``loop.py``.

**The outbox half is split across two packages, and the split is the design.**
``rheo_core.events`` owns the write and every read and write against a delivery row:
the event a producer hands in, the fan-out at write, the consumer registry, and the
ordered lease with its three finishes. This package owns the *draining* of those rows,
because draining is the worker's loop and there is only one of those:
``loop.visit_workspace`` drains up to ``MAX_JOBS_PER_VISIT`` jobs out of a workspace and
then up to the same bound of deliveries, running each consumer's handler with the
``core.consumer_processed`` ledger row and the delivery's ``delivered`` write in the
handler's own transaction.

Two import edges cross between the packages, and they point in opposite directions on
purpose: ``events.deliveries`` calls this package's ``backoff_for``, because one retry
policy governs jobs and deliveries alike; and ``loop`` and ``operations`` call
``rheo_core.events``, because the drain and the failure list are readers of that
contract. Neither package holds a second copy of the other's rules.

**The repository functions in ``jobs.py`` take the caller's connection and commit
nothing** — ``jobs.enqueue`` is the one documented exception, and says so in its own
docstring. **``loop.py`` is deliberately the opposite**: it is the worker, so both
drains open and commit their own transactions there, and its module docstring carries
the transaction map for each. Keeping the two claims apart is the point — the first is
what makes a repository composable, the second is what a worker is for.

**Schedules are a one-function insertion.** ``run_due_schedules(conn, *, workspace_id,
now)`` runs on a short unit of work after ``visit_workspace`` acquires the engine
and before the job-drain loop, plus one enqueue per due row. The visit already
computes the workspace's ``next_due_at`` and folds the soonest enabled schedule
instant into that remaining write.
"""

from rheo_core.work.schedules import (
    RETENTION_SWEEP,
    RetentionSweepPayload,
    earliest_schedule_due_at,
    run_due_schedules,
    run_retention_sweep,
)

__all__ = [
    "RETENTION_SWEEP",
    "RetentionSweepPayload",
    "earliest_schedule_due_at",
    "run_due_schedules",
    "run_retention_sweep",
]
