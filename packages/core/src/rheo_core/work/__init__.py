"""Durable execution: the outbox, event deliveries, jobs, and schedules.

The tables landed in ``storage/work_tables.py``, created by the core chain's
``0002_durable_work`` revision. The behaviour that would live in this package — a
worker, a dispatcher, delivery, the job lease, the retry budget, cancellation — is
built by the runs that follow, so until then this package is a docstring and holds no
attribute at all.
"""
