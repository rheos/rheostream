# Finance

A personal ledger and reminder module. It reads your own bank, card and bill
statements into one schema, `finance`, shows what you spend and what you owe, sends
due-date reminders, and watches crypto wallet holdings through a portfolio price service (`teak`).

It is not an accounting or tax product. Tax packs are inputs for an accountant and say
so; what the ledger does not compute is listed in the design's open questions.

## Status

Wired, not loaded on the flagship. This directory holds the code, tests and decision
records of the standalone project it started as (Float), copied in without its Git
history. The distribution is `rheo-finance`, the package `rheo_finance`, the module id
`finance`, with a `rheo.modules` entry point and a `finance` console script.

Discovery is not loading: core loads only the ids in `modules.installed`, and the
flagship's list does not name `finance`. Adding it there is the owner's later step.
How the module runs on core is ADR-0024.

Not imported, because rheoStream owns those jobs: the standalone server runtime (the
foreground scheduler, the LaunchAgent installer, `finance db up`, `db init`,
`db migrate` and `db locked`), the native PostgreSQL 17 dump and restore seam, the
direct `claude -p` model client, and the local parity check against the earlier
parsers.

## Install and enable

1. Name `finance` in the deployment's `modules.installed`, then install and enable it
   in the workspace (`core.module.install`, `core.module.enable`). Install runs the
   frozen chain on core's connection; `migrations/env.py` makes the two NOLOGIN roles,
   `finance_owner` and `finance_reader`, and runs every revision as the owner.
2. Set `FINANCE_WORKSPACE_ID` (the workspace's UUID, not a secret) for the `finance`
   command, and run `finance init-data` once. It creates the layout under the
   workspace's uploads directory, mode 0700:
   `<data root>/workspaces/<id>/uploads/finance/{data,archive,inbox,keys}`. Nothing
   else creates these directories, so a missing volume refuses
   (`finance_layout_missing`) instead of looking like an empty archive.
3. Put the settings file in `data/`. The archive and the downloads inbox are the
   layout's; a settings file that sets either is refused. A credential file must sit in
   `keys/`, mode 0600; secrets are files there, never deployment variables.
4. Move the state of an old standalone ledger with `finance state export --out DIR
   --source-dsn-file FILE` (FILE: a 0600 env file with `FINANCE_OWNER_DSN`), then
   `finance state import --from DIR` and `finance rebuild` on the workspace.

### Core settings

| Key | Scope | Default |
| --- | --- | --- |
| `finance.jobs.enabled` | workspace | false |
| `finance.model.classify_model_id`, `suggest_model_id`, `extract_model_id` | deployment | empty: the CLI's default model |
| `finance.model.account_id` | workspace | empty: no model steps |

The `tick` schedule (`finance.tick`, every 15 minutes) is written at enable but
enqueues only once the workspace itself sets `finance.jobs.enabled` to true with
`core.settings.set`. The module registers no core operations or tools, so no token,
package set or agent reaches it, and core's workspace export carries none of its rows.

### A non-superuser application role

`env.py` creates the roles only when the application connects as a superuser. Under
any other application role, an operator creates both once and grants them to it:

```sql
CREATE ROLE finance_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE finance_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT finance_owner, finance_reader TO <application role> WITH INHERIT TRUE, SET TRUE;
```

Install then reuses them; without them it refuses `finance_roles_unavailable`.

## Principles

- Source is public; runtime data is private. Statements, exports, keys and the ledger
  itself never enter the repository or an image build context. The suite's
  private-data guard and the repository's fixture-provenance gate hold every commit
  to that.
- Read-only. It never moves money and never holds a bank password.
- Nothing goes out until you approve it. Every outbound connection (reminders,
  Telegram, rate and price fetches, vendor APIs) is off until you turn it on once,
  from your own terminal.

## Reminders

The module owns the amount-due Current item for each payee period in the `bills`
area. The stable `finance:<payee-slug>:<period>` key appears in both `source_ref` and
the body's `Period-key:` line. Intake can skip a bill already tracked here or
supersede its own duplicate. Changes reconcile by stored ref, including settlement
and later returned payments. A complete desired-state read closes previously synced
keys it omits; a failed read leaves existing items open. The Current sink stays off
until the owner approves its declared policy and enables it.

Current's preview lists exact pending changes, including priority becoming overdue
at local midnight. Its confirmation code can change at that boundary; refresh the
preview if confirmation refuses an older code. This does not invalidate lasting
sink-spec authorization. Rendered times and relative ages stay outside the digest.

## Provenance and license

- Code, tests and records: the owner's standalone project at commit
  `555bd8349590582b10bfd3c074ef3ac6f4495394`, with every institution renamed to a
  codename, now under this repository's AGPL-3.0.
- `src/rheo_finance/parsers/vendored/`: four files of `doc-extraction-api` (the
  owner's own project) at commit `0ad7253f6586eea62cabc32d7d69823e3987e67b`, one
  unchanged and three with recorded patches (codenames, then invented labels and
  shapes read through `identities`), also under AGPL-3.0.
  `tests/guards/test_vendored_bytes.py` pins each one.
- `tests/fixtures/synthetic/`: invented layout text in the codenames, every entry in
  the repository's `tests/fixtures/provenance.json`.

## Names

Every bank, card issuer, utility, payment service and exchange is named by a neutral
codename (`alder`, `olive`, `sumac`, ...), and so is the published exchange-rate
source (`rowan`). Where a parser or a reference pattern must match an institution's
own wording, it reads that wording from the owner's private
`$FINANCE_DATA_DIR/identities.toml` through `identities.real`; nothing in this tree
names the institutions. See ADR-0023.

## Decision records

`docs/adr/` keeps the standalone project's records under their own numbers;
[docs/adr/README.md](docs/adr/README.md) indexes them.

## Tests

The suite runs on the repository's test cluster like the rest of `make test`. Each
pytest session (each xdist worker) provisions one `ws_*` database, creates the
`finance_owner` and `finance_reader` roles as NOLOGIN when they are missing, and
migrates the frozen chain `0001` to `0042` under the owner role. A local run:

```sh
make test-fast PYTEST_ARGS=modules/finance
```
