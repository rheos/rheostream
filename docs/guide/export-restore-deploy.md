# Export, restore and deploy

## What it is

How a workspace leaves, comes back, gets erased in part, and runs.

- **Export** writes a whole workspace to one compressed artifact.
- **Restore** brings that artifact into an empty deployment with the same identifiers,
  and nothing fires until a person says so.
- **Erasure** deletes one record and everything derived from it, in one transaction,
  leaving a content-free record of what happened.
- **Deployment** is four containers behind a reverse proxy, with nightly backups and an
  hourly health check.

## What you can do with it

- Export a workspace from the CLI or as an owner, and get an operation id to poll.
- Restore an artifact into a deployment that has never seen that workspace.
- Compare two workspaces with `core.workspace.digest`: one call on each side, a line
  count and a SHA-256 per category.
- Delete an observation, an opportunity, a party, a memory, a work item or a project,
  each with its own per-action approval.
- Withdraw or suppress a person's contact permission without deleting anything.
- Run the stack on one server, in path mode with one name and one certificate, or in
  subdomain mode with a wildcard certificate.

## How export and restore work

**One snapshot, one file.** The export job reads the workspace from a single read-only
`REPEATABLE READ` snapshot and writes one zstd-compressed tar:

```text
manifest.json        versions, workspace id and slug, owner, each module's export format,
                     a settings digest and record counts
settings.jsonl       workspace and member settings; secret references, never values
core/                approvals, operations, the audit log, deletion records
modules/<id>.jsonl   each module's rows in its declared export format,
                     validated against its JSON Schema before the export completes
```

Secret values are absent by construction, because no exporter can resolve one. Each
module writes its own rows through a core writer that enforces its schema, reading
through a sealed snapshot so it cannot slip a second, later view into the same file.

**Formats are versioned per module.** A module bumps its export format version when its
row shape changes, and restore refuses an artifact whose version differs from the host's,
naming the module. A module's importer owns upgrading older rows within the format it
accepts.

**Restore keeps identities and fires nothing.** Restore validates the manifest, creates
the workspace with its recorded id and slug, provisions the database, and holds it in
`restoring`. Then, in one transaction, it migrates each module's schema to the host's
head and imports settings, approvals, operations, the audit log, each module's rows
(parents first), the deletion records, and finally each module's second pass, which
resolves self-references. Every pending approval lands as `requires_reapproval`, held
operations wait for approval again, and anything else unfinished lands `unresolved`.
Connections come back needing a fresh credential and member credentials needing a value.
Only then does the workspace go `active`.

**Some things are rebuilt rather than carried.** Embedding vectors are not exported,
because the model may differ on the other side; an owner runs the rebuild after restore.
A restored job feed has no schedule and asks the owner to reconnect.

## How erasure works

**One record, one transaction, every consequence.** `core.record.delete` is destructive:
per-action approval, never a standing grant. The coordinator runs in one transaction
against the workspace database, which a database per workspace makes possible without a
distributed step:

1. Recheck the approval and that the record is still at the approved revision.
2. The owning module deletes the record and everything it owns for it. Deleting an
   observation re-derives the opportunity's fields from what remains.
3. Every enabled module that declared an interest runs its participant. Recallatron
   removes memories linked to the record and everything derived from them; Leads unlinks
   observations from an erased party and keeps the delivery receipt as a tombstone.
4. The core cancels queued jobs and pending external actions that depend on the record,
   invalidates approvals bound to it, and deletes transcripts of model runs that were
   shown it.
5. Exports that contain the record are marked removed, with the reason.
6. A deletion record is written: what, by identifier only; who; which approval; which
   participants ran; and counts. It has no column that could hold the deleted content.
7. Commit. Then the marked export files are deleted, with a durable retry if a file
   cannot be removed. The record says why the artifact is gone before the artifact is
   gone, never the reverse.

If any participant fails, the whole deletion rolls back and the record survives
untouched. Deletion never reaches another workspace, shared credentials or unrelated
records.

**Deletion is not withdrawal.** Withdrawing contact permission leaves every record in
place: queued actions fail at their guard and memories stop being retrievable for that
purpose. Deleting removes the record and leaves permissions and suppressions untouched.
They are two actions with two confirmations, and neither calls the other.

**Erased data stays erased.** A delivery receipt outlives its observation with only its
identifier, digest, length and times. If the source sends the same event again, it is
recorded as a conflict with no body, so a retrying sender cannot land an erased person's
data back.

## How deployment works

**Four containers and a proxy.** `core` (the API, sign-in and the MCP door), `worker`
(the same image with a different command), `web` (the shell) and `postgres` (with
pgvector and `pg_trgm`). The reverse proxy is any proxy that can route by host and path
and terminate a certificate. Its rules are the same in both modes: `/auth/*`, the API and
the MCP door go to `core`; everything else on an application host goes to `web`.

**Two topologies, one configuration.** Path mode needs one name and one certificate and
is what a fresh install produces. Subdomain mode gives each surface its own host
(`circuit.`, `auth.`, `api.`, `mcp.`, and one per module such as `leads.` and `recall.`)
behind a wildcard certificate. The proxy routes only the hosts the configuration names,
never a bare wildcard.

**Health checks avoid the database.** `core` and `web` answer `/healthz` without
touching Postgres. The worker has no port, so it touches a heartbeat file each loop, and
its probe fails once that file is ten minutes old.

**Startup migrates, upgrades are explicit.** Startup runs the core migration chain over
every active workspace, one at a time. Module upgrades are a separate audited operator
command that runs only migrations explicitly marked non-destructive; a destructive
schema change needs an export newer than the upgrade's first attempt. While a module is not ready,
its jobs and deliveries wait a minute without spending a retry.

**Backups are verified before anything old is deleted.** A nightly host job dumps every
database in the cluster with `pg_dumpall`, because one database per workspace means a
single-database dump would miss workspaces. It writes to a hidden partial file, checks
the gzip stream, a minimum size and the dump's closing line, and only then renames it.
Retention keeps the newest dump from each of the last 14 days and the last 8 Sundays,
about 20 files, never compares dates with the clock, and never deletes the newest good
dump. A failed run deletes nothing.

**Two ways back.** A cluster backup restores with Postgres's own procedure. The
application-level export and restore is the other path, and it is the one the acceptance
tests drill: export, restore into an empty deployment, compare digests.

## Why it's built this way

Owning a system means being able to leave with it. A workspace is one database, so it is
one artifact, with the same identifiers on the other side and a digest to prove the copy
matches. Restore is deliberately inert: approvals, operations and credentials all come
back waiting for a person, so moving a workspace can never fire off the emails it had
queued.

Erasure has to be complete or it is not erasure. Running the owner, every participant and
the core's own cleanup in one transaction means a deletion either reaches every copy the
system controls, from derived memories to export files to model transcripts, or does
nothing at all.

## Where to look

- Specification: [deletion, export and restore](../architecture/deletion-export-migration.md),
  [module upgrade and lifecycle](../architecture/module-contract.md#lifecycle),
  [processes](../architecture/overview.md#processes),
  [routing table](../architecture/identity-and-topology.md#the-routing-table),
  [deployment guide](../../deploy/README.md)
- Code: [exports](../../packages/core/src/rheo_core/exports),
  [deletion coordinator](../../packages/core/src/rheo_core/deletion),
  [compose stack](../../deploy/compose.yaml),
  [flagship overlay](../../deploy/compose.flagship.yaml),
  [backup script](../../deploy/backup/rheostream-pgdumpall.sh),
  [doctor alert](../../deploy/doctor/rheostream-doctor-alert.py)
- Tests: [export and restore](../../tests/postgres/test_export_restore.py),
  [record deletion](../../tests/postgres/test_record_deletion.py),
  [module upgrade](../../tests/postgres/test_module_upgrade.py),
  [backup retention](../../tests/test_backup_retention.py),
  [doctor alert](../../tests/test_doctor_alert.py)
