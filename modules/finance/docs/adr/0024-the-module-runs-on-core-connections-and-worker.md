# ADR-0024: The module runs on core's connections and worker
Status: accepted
Date: 2026-11-11
Partly replaced by ADR-0028: "No operations or tools in core"

## Context

ADR-0021 made Float a rheoStream module and left role setup and data access to the
module's own records. The standalone project logged in as two Postgres roles through
DSNs in `db.env`, took a file lock under its data directory, ran its own scheduler,
and called `claude -p` itself. Inside rheoStream the application owns the connection,
the worker owns scheduling, the deployment owns secrets and the runtime contract owns
model calls. Issue #543 wires the module into all four.

## Decision

- **Roles from `env.py`.** The migration environment uses only the connection core's
  install or upgrade passes. It makes sure `finance_owner` and `finance_reader`
  exist (NOLOGIN, created by a superuser application role, or created by the operator
  and granted to a non-superuser one, per the README), gives schema `finance` to the
  owner, runs every revision as the owner and resets the role before core's install
  goes on in the same transaction. ADR-0008's two connection names are now these
  roles, not two DSNs.
- **One role per operation.** `dispatch` opens a fresh core session per operation and
  runs `SET LOCAL ROLE finance_owner` or `finance_reader` by the spec's connection.
  The role ends with the transaction `dispatch` ends, so a pooled connection goes back
  as the session user.
- **An advisory writer lock.** The flock under the data directory is replaced by a
  transaction-level advisory lock on the workspace database, polled up to 30 seconds,
  then `writer_busy`. The CLI and the worker are separate processes on one database.
- **The tick is a core job.** `finance.tick` runs every 15 minutes once the workspace
  itself sets `finance.jobs.enabled`. It commits in short sessions of its own, checks
  the cancellation token between jobs, and fails with a fixed code alone, because the
  worker stores the failure's text.
- **The uploads layout.** Data, archive, inbox and keys live under the workspace's
  uploads directory. Nothing creates them but `finance init-data`, because an empty
  archive on an unmounted volume would let derive empty the ledger. Secrets are
  private files in `keys/`, never deployment variables.
- **No operations or tools in core.** The manifest declares none, and no exportable
  record type, so no token, package set or workspace export reaches the ledger. State
  moves with `finance state export` and `import`.
- **Model calls through the runtime.** Each stage's model and the run's account are
  core settings; the client goes through core's one-shot runner, after the prompt
  guard.

## Consequences

Nothing is superseded. The flagship loads the module only when its
`modules.installed` names it, which is the owner's later step. A deployment whose
application role is not a superuser needs the two roles made by hand first.
