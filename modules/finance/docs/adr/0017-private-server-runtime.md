# ADR-0017: Private server runtime with public source
Status: superseded-by-0021
Date: 2026-11-07

## Context

A sleeping personal computer stops catch-up scheduling. A private server can run
jobs continuously, but financial state must stay separate from public source and
release builds. Moving the runtime must preserve the existing approval and role
boundaries and leave a verified recovery path.

## Decision

Run the installed CLI and foreground worker on a private Linux server with a
dedicated private PostgreSQL 17 service. Runtime data, credentials and a copied
archive live on private mounts outside the repository and image context. Keep the
original local archive, database and backups intact for rollback; ledger-owned
data remains outside iCloud on both machines. ADR-0007's residency mechanics are
superseded, while its protection against syncing active ledger data is retained.

Release reviewed GitHub main commits through the deployment workflow as immutable
private images, with Coolify selecting the exact digest. Public source includes
only generic code, documentation and synthetic fixtures. A release is not verified
until source, image, schema and first healthy tick agree.

One shared process lock on the private data mount coordinates worker ticks,
manual writers and migration/rebuild. Reads remain concurrent. Foreground health
uses existing tick/job history; no health table or scheduling model is added.
Native PostgreSQL 17 clients provide the infrastructure seam for the existing
planned backup job, without a second backup system.

## Consequences

The Mac needs no normal background job or Docker runtime. Local Compose and the
Mac agent remain available for development and rollback. Server recovery pauses
scheduling and restores into a separately provisioned empty database before any
DSN switch; it never overwrites the active ledger or automatically downgrades a
schema. Stop the server writer before resuming local writers.

Parser imports, derived/state separation, role routing, approval, identity and
Current/reader decisions in ADRs 0001, 0003, 0005, 0008, 0010 and 0014-0016 remain
unchanged. Subscription model execution keeps its empty temporary cwd, tools-off
flags and prompt guard. Implementing these contracts does not enable a sink or
claim that a deployment or restore proof has occurred.
