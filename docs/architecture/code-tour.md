# Architecture tour

rheoStream keeps workspace records in Postgres and lets web clients and agents
operate on them through the same service boundary. The core and Recallatron are
implemented; Leads, Current and Relationships are still stubs. See the
[README status](../../README.md#status) for the current release position.

This is a route through the code that exists today. The
[architecture specification](README.md) also describes work that is still planned.

## Follow a request

```text
Next.js web shell                  Agent / MCP client
        |                                  |
        +----------> FastAPI core <--------+
                     (MCP mounted here)
                              |
                  Server-resolved context ----> Control database
                              |                 registry + due index
                  Authorize and dispatch                 ^
                              |                          |
                   Core / Recallatron handler      Durable worker
                              |                          |
                              v                          v
                       Workspace database <--------------+
                       records, audit, jobs, outbox
```

The web shell calls core's internal API. MCP is mounted in the core process.
Both reach registered operations; module code does not need to know which model
or client initiated the call.

1. [Boundary factories](../../packages/core/src/rheo_core/boundary/factories.py)
   resolve the authenticated account, workspace, permissions and bound purpose
   from sessions, tokens or stored execution context.
2. [Storage routing](../../packages/core/src/rheo_core/storage/routing.py) selects
   the database from that context and the control-plane registry. It does not
   accept a database name or workspace override from an operation's payload.
3. The [dispatcher](../../packages/core/src/rheo_core/operations/dispatch.py)
   checks access and input, then runs the handler inside a workspace transaction.
   A successful mutation and its audit record commit together. Destructive,
   external and financial operations are held for approval before their handlers run.
4. A long-running operation returns an operation ID. The
   [worker](../../packages/core/src/rheo_core/work/loop.py) leases the persisted
   job and records the terminal outcome when it finishes.

Modules are trusted, operator-installed Python packages in the same process.
Their contracts and checks enforce application boundaries, not a sandbox for
hostile code. The [module contract](module-contract.md) explains registration
and the implemented install/enable lifecycle.

## Decision: a database per workspace

[A1](storage-and-workspaces.md#a1-the-per-workspace-unit-is-a-database) chose one
Postgres database per workspace, with a separate control database. Within a
workspace, core and each module own separate schemas.

A workspace connection reaches one database, so ordinary queries do not depend
on remembering a tenant filter. Workspace data and migration state also have a
separate storage unit. Server-side routing still matters: opening the wrong
workspace connection would defeat that separation.

The cost is operational. Provisioning, migrations and connection pools multiply
with workspaces. The [pool implementation](../../packages/core/src/rheo_core/storage/pools.py)
limits cached engines and keeps busy ones pinned; the
[connection-budget documentation](../../deploy/README.md#connection-budget)
explains the capacity an operator needs to reserve.

## Limitation: enqueue crosses two databases

[`enqueue`](../../packages/core/src/rheo_core/work/jobs.py) commits the job in
its workspace database, then marks that workspace due in the control database.
No transaction spans both writes. A crash after the first commit can leave a
persisted job without a fresh due-work mark, delaying discovery by the worker.

The [due-work index](../../packages/core/src/rheo_core/storage/work_index.py)
is a scheduling hint. `record_visit` caps the next visit time at the configured
reconcile interval, and its compare-and-set preserves a concurrent earlier mark.
That provides a recovery path for a missed mark; it does not make enqueue atomic
or guarantee a wall-clock completion time when workers are unavailable or overloaded.
Job leases and outcomes remain in the workspace database.

The tests below exercise the index races and revisit behaviour.

## Check the claims

| Boundary | Implementation evidence |
| --- | --- |
| Caller input cannot redirect a workspace operation | [Context-routing tests](../../tests/postgres/test_context_routing.py), including a payload naming workspace B while the call is bound to A |
| Workspace connections and transactions remain separate | [Isolation tests](../../tests/postgres/test_isolation.py), including rejection of an engine for the wrong database |
| Mutation and audit commit together | [Audit-dispatch tests](../../tests/postgres/test_audit_dispatch.py), including handler failure and commit failure |
| Missed or racing due marks have a recovery path | [Work-index tests](../../tests/postgres/test_work_index.py) and [worker-loop tests](../../tests/postgres/test_worker_loop.py) |

For the wider test map, see [acceptance evidence](../acceptance/README.md).
For the remaining design decisions, use the
[architecture decision index](README.md#architecture-decisions).
