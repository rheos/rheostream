# Durable work

## What it is

The machinery that lets work outlive a crash, a restart or a lost connection. It is eight
plain tables in each workspace's `core` schema, in five groups, plus a worker:

- The outbox (`outbox_event`, `event_delivery`, `consumer_processed`): events written
  in the same transaction as the change they describe, delivered to each consumer once;
- Jobs and schedules (`job`, `schedule`): background work run under a lease;
- Operation records (`operation`): one per long-running or gated call, with a
  terminal outcome a caller can poll;
- Audit records (`audit_record`): one per mutating call, committed with the change;
- External actions (`external_action`): an approved effect on the outside world,
  executed by a job with its own outcome record.

There is no message broker, no distributed workflow engine and no event sourcing.

## What you can do with it

- Start a long-running operation from any surface and get an operation id back at once.
  Poll it with `operations_get` from an agent, or the same read over the API.
- Cancel a job. A queued job is cancelled without running; a running one stops at its
  next checkpoint and ends `cancelled`, not retried.
- See what failed. `core.work.failures` lists every failed delivery with how many later
  deliveries it is holding back, and the shell shows a banner on every screen while any
  delivery has failed.
- Retry a failed delivery, skip it with a written note, or replay a consumer that only
  rebuilds derived state.
- Read the audit log: who did what, through which door, when, and with what outcome.
- Resolve an operation whose external outcome is unknown, with an explicit outcome and a
  note.

## How it works

### A change and its event commit together

A handler never commits on its own. The service registry opens one unit of work against
the workspace database, runs the handler, writes the audit row and any events the handler
queued, and commits. Publishing an event inserts the outbox row and one delivery row for
every consumer subscribed to it in a module enabled at that moment. A consumer that is not
enabled gets no row and never sees the event.

### Order is kept per subject

Deliveries for the same consumer and the same subject run in outbox order, one at a time.
The lease query is the whole rule: a delivery is leased only when no earlier delivery for
that consumer and subject is anything but delivered or skipped. A failed head therefore
blocks the queue behind it until a person retries it or skips it with a note. Order is
never broken silently.

### Each event takes effect once

A consumer runs inside a unit of work, and the worker inserts a `consumer_processed` row
in that same transaction. If the worker crashes after the effect and before marking the
delivery done, the redelivery hits that row's primary key, and the worker marks it
delivered without running the handler again. Duplicate safety is part of the contract, so
no handler has to remember it.

### Retries back off, then stop

A failing delivery is retried after 5, 20, 80, 320 and 1280 seconds, then hourly, with
jitter, for 8 attempts in all: about two and a half hours. After that it is `failed`,
listed, and holding its queue.

### Replay rebuilds derived state

A consumer declares whether it is replay-safe. Read models and index builders are;
anything that creates records or touches the outside world is not, and replay refuses to
target it. Replay locks the delivery table for its own short transaction so a concurrent
publish cannot put one subject's events in flight twice.

### Jobs run under leases

The worker leases one queued job at a time with `FOR UPDATE SKIP LOCKED` for 60 seconds
and extends the lease every 20. An expired lease means a crashed worker, and the job is
picked up again. Each job runs across three transactions on purpose: the lease commits
alone so other workers can see it; the handler's effects and the job's success commit
together, so a job's work and its success can never be separated; any other outcome rolls
that back and records the failure in a fresh transaction.

### The worker only visits workspaces with work

A due-work index in the control plane says which workspaces have something waiting. The
worker reads it instead of walking every workspace, so an idle workspace costs nothing.
The index is a hint, not a lease: a workspace with no row counts as due, and the reconcile
interval (900 seconds by default) bounds how long enqueued work can sit unseen. Within a
pass, workspaces whose connection is already cached are served first, so a busy pass never
evicts the connection it is about to use.

### Schedules are rows

A module's manifest declares its schedules; enabling the module creates them for that
workspace. The core runs a daily retention sweep that removes expired transcripts,
telemetry and settled outbox rows (kept 30 days by default).

### An operation record cannot claim success without proof

Long-running calls return an operation id immediately and do their work as a job. The
record moves through `pending`, `running`, `approval_required` and on to a terminal state.
A database check constraint refuses `succeeded` unless the record also names what was
checked (`record_exists`, `provider_status`, `sink_recorded` or `handler_returned`). A
model's text reply is not proof that a domain action happened.

### Unknown outcomes stay visible

When an external effect times out and its provider offers neither idempotency nor a status
lookup, the operation goes `unresolved`. That is terminal for the record and an open item
for a person, cleared only by `core.operation.resolve` with an outcome and a note.

### The dispatcher writes the audit row

Every operation above the read class gets an audit row: actor, entry point, operation,
safety class, subject, a SHA-256 digest of the canonical request, and the outcome. A
successful row commits inside the operation's own transaction, so a mutation and its
record are never separable. A failed or refused row is written after the rollback, in its
own short transaction. Registration refuses any non-read operation that does not declare
how it is audited. The row never holds a secret or a reference to one.

### Idempotency

An operation can declare natural idempotency: a unique index under which a repeat is the
same row. Intake is idempotent on the connection and the sender's event id; a handoff on
the opportunity and its idempotency key. Operations that must hand back a created
reference on a retry, such as Current creating work from a handoff, use keyed idempotency
with a stored result.

### External actions are jobs with their own record

An approved effect runs as a job that depends on its destination, so withdrawing the
recipient's permission or deleting the destination party cancels or refuses it. The lease
prevents two executions of one action. A retry after an unknown outcome first reconciles
by the provider's request id, and otherwise goes `unresolved`.

## Why it's built this way

The gap between saving data and announcing it is where most background systems lose work.
Here there is no gap: the change, its audit row and its events are one commit. The worker
can die at any instruction and the worst case is that something runs again, safely,
because the consumer's dedup row is in the same transaction as its effect.

Ordinary tables were enough for every requirement, and each table exists because a
requirement forces it. A broker or workflow engine would add a second system to operate
and a distributed transaction to reason about, for a deployment that runs on one server.

Head-of-line blocking is a deliberate trade. A failed delivery stops its subject's queue
rather than letting later events overtake it, and the failure is pushed to where a person
already looks: a banner on every screen and the connection health in Leads.

## Where to look

- Specification: [events, durable work, operations, audit](../architecture/intake-and-events.md#part-two-events-durable-work-operations-audit),
  [architecture tour](../architecture/code-tour.md),
  [substrate boundary](../notes/0c0-substrate-boundary.md)
- Code: [publish](../../packages/core/src/rheo_core/events/publish.py),
  [deliveries](../../packages/core/src/rheo_core/events/deliveries.py),
  [outbox retention](../../packages/core/src/rheo_core/events/retention.py),
  [worker loop](../../packages/core/src/rheo_core/work/loop.py),
  [jobs](../../packages/core/src/rheo_core/work/jobs.py),
  [backoff](../../packages/core/src/rheo_core/work/backoff.py),
  [due-work index](../../packages/core/src/rheo_core/storage/work_index.py),
  [dispatcher](../../packages/core/src/rheo_core/operations/dispatch.py),
  [operation records](../../packages/core/src/rheo_core/operations/records.py),
  [audit](../../packages/core/src/rheo_core/audit)
- Tests: [event delivery](../../tests/postgres/test_event_delivery.py),
  [consumer publication](../../tests/postgres/test_consumer_publication.py),
  [worker loop](../../tests/postgres/test_worker_loop.py),
  [work index](../../tests/postgres/test_work_index.py),
  [work failures](../../tests/postgres/test_work_failures.py),
  [work recovery](../../tests/postgres/test_work_recovery.py),
  [audit dispatch](../../tests/postgres/test_audit_dispatch.py)
