"""Durable execution: the outbox, event deliveries, jobs, and schedules.

The tables landed in ``storage/work_tables.py``, created by the core chain's
``0002_durable_work`` revision. The **job** half of the behaviour is now real and lives
here: the repository and its lease in ``jobs.py``, the retry schedule in ``backoff.py``,
the cancellation checkpoint in ``cancellation.py``, the job-kind registry in
``kinds.py``, and the worker's own loop — leasing, the retry budget, cancellation — in
``loop.py``.

The other half is still deferred. The outbox and event delivery belong to 0c2 and the
runs after it; nothing in this package writes, reads or drains either of them yet, and
their tables are simply waiting for the run that builds the behaviour over them.

**Schedules stay a one-function insertion.** The run that builds them adds
``run_due_schedules(conn, *, now)`` at the top of ``loop.visit_workspace``, plus one
enqueue per due row. Nothing here has to be redesigned to accommodate them: the visit
already holds the workspace connection and already computes the workspace's
``next_due_at``. The same sentence is in ``visit_workspace``'s own docstring, which is
where that run will actually be reading.
"""
