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
How the module runs on core is ADR-0024; how its reminders reach Current there is
ADR-0025.

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
   command, and run `finance init-data` once; from a laptop, `finance remote exec --
   init-data` sets the variable itself. It creates the layout under the workspace's
   uploads directory, mode 0700:
   `<data root>/workspaces/<id>/uploads/finance/{data,archive,inbox,keys}`, with a
   commented settings template and header-only manual files. Nothing else creates
   these directories, so a missing volume refuses (`finance_layout_missing`) instead
   of looking like an empty archive.
3. Export an old standalone ledger's state on the laptop with `finance state export
   --out DIR --source-dsn-file FILE` (FILE: a 0600 env file with `FINANCE_OWNER_DSN`),
   then push the settings, the manual files, the archive and the export in one go:
   `finance remote push --replace --data OLD_DATA_DIR --archive ARCHIVE_DIR --state
   DIR`, adding `--keys KEYS_DIR` when the settings name key files. `--replace`
   replaces init-data's templates with your own files. The archive and the downloads
   inbox are the layout's; a settings file that sets either is refused, so remove
   those keys before pushing. A credential file sits in `keys/`, mode 0600; secrets
   are files there, never deployment variables.
4. Load the pushed export and rebuild: `finance remote exec -- state import --pushed`,
   then `finance remote exec -- rebuild`. On the server itself, `finance state import
   --from DIR` reads an export from any directory.

### Core settings

| Key | Scope | Default |
| --- | --- | --- |
| `finance.jobs.enabled` | workspace | false |
| `finance.model.classify_model_id`, `suggest_model_id`, `extract_model_id` | deployment | empty: the CLI's default model |
| `finance.model.account_id` | workspace | empty: no model steps |

The `tick` schedule (`finance.tick`, every 15 minutes) is written at enable but
enqueues only once the workspace itself sets `finance.jobs.enabled` to true with
`core.settings.set`. The tick renews its job's lease inside its long steps, per file,
document, stage, series and reminder key (ADR-0027). Core's workspace export carries
none of the module's rows.

### Agent reads

The module registers eight read operations in core, each with its tool, for the
workspace owner alone (ADR-0028):

| Tool | Operation | What it shows |
| --- | --- | --- |
| `finance_status` | `finance.status.get` | the status lines |
| `finance_coverage` | `finance.coverage.get` | statement coverage by account and month |
| `finance_documents` | `finance.document.list` | documents with their kind, status and period |
| `finance_sinks` | `finance.sink.state` | each sink's setting and approval state |
| `finance_lines` | `finance.txn.list` | ledger lines, filtered by account, dates and state |
| `finance_open_items` | `finance.openitem.list` | open items and counts per kind |
| `finance_owed` | `finance.owed.list` | obligations with dates, status and flags, credits and bill gaps |
| `finance_trace` | `finance.owed.trace` | each obligation with its payments and evidence |

No restricted field reaches a model: every field finance tiers restricted (amounts,
totals, the owed headline) is left out of what these operations return. An agent sees
the fields finance tiers public or internal (structure, dates, states, counts, labels
and the status lines' text), as finance tiers them. Labels (payees, accounts,
document paths) are public under finance's own tiers and do reach it.

The reads are opt-in. No routine token, named package set or connector grant reaches
them, and the module's other operations are not registered in core at all. The one
way in is a token issued for them, by the operator, inside the core container:

```sh
rheo token issue --account <account id> --workspace <workspace id> --set module_read:finance --kind mcp
```

The token holds the eight reads and nothing else, and lasts as long as core's
`identity.token_max_days.mcp` allows (30 days by default). Each read runs
as `finance_reader` and needs the workspace's data layout mounted where it runs; a
missing layout refuses with `finance_layout_invalid`.

A failed read refuses with one fixed code as both state and detail, and nothing else:
the code the tick's table gives its class (`finance_layout_invalid`,
`finance_settings_invalid`, `finance_schema_behind`, `finance_schema_unknown`,
`finance_operation_refused`, `finance_storage_failed`), `finance_output_invalid` when
a handler answers another type, or `finance_read_failed` for any other failure, a
view that cannot be built included. Core's own refusals (a module that is not ready,
an unavailable workspace) come back as core gives them.

### A non-superuser application role

`env.py` creates the roles only when the application connects as a superuser. Under
any other application role, an operator creates both once and grants them to it:

```sql
CREATE ROLE finance_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE finance_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT finance_owner, finance_reader TO <application role> WITH INHERIT TRUE, SET TRUE;
```

Install then reuses them; without them it refuses `finance_roles_unavailable`.

## Working from a laptop

The `finance` command on a laptop reaches the module in the worker container over
ssh and `docker exec` (ADR-0026). It needs a config file outside the repository,
readable by its owner alone (mode 0600, not a link), named by `--config FILE` or
`$FINANCE_REMOTE_CONFIG`; there is no default path:

```toml
destination = "finance-host"          # an ssh_config Host alias, or ops@finance.example.test
compose_project = "sample-project"    # the worker's com.docker.compose.project label
compose_service = "worker"            # its com.docker.compose.service label
workspace_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"  # passed as FINANCE_WORKSPACE_ID
docker = ["docker"]                   # optional, e.g. ["sudo", "-n", "docker"]
```

Each command finds the one running container with both labels, then runs `finance`
in it. The remote account's login shell must be sh-compatible.

- `finance remote push` copies files into the layout. `--archive DIR` and `--inbox
  DIR` take every regular file, recursively. `--data DIR` takes `settings.toml`,
  `identities.toml` and `manual/` only, never `db.env`, `dumps/` or anything else
  beside them. `--state DIR` takes a `finance state export` directory that reads back
  whole, into `data/transfer`. `--keys DIR` takes the top-level files, each readable
  by its owner alone, written 0600; keys go only with this flag. Hidden files and
  links are skipped and counted.
- `finance remote pull --into DIR` copies `data/packs` into an existing directory.
- `finance remote exec -- ARGS` runs `finance ARGS` in the container with your
  terminal, and returns its status.

Nothing on the laptop is changed by a push, and nothing in the container by a pull.
Each file lands atomically: it is staged under a temporary name, then linked into
place, so no final name ever holds part of a file. A transfer cut, killed or refused
before the commit step writes nothing. A refusal during the commit step, whatever its
code, keeps the files already linked and says how many: `refused. target_appeared (3
files written before it).` When ssh or the reply fails after the stream was sent, the
outcome is not known and the message does not say "Nothing was written."
An existing file with the same bytes is skipped; one with different bytes refuses
the whole push before anything is written (`target_differs`). `--replace` replaces
only `data/settings.toml`, `data/identities.toml` and `data/manual/**`; statements,
inbox files, keys and transfer files are never replaced (give a new key a new name),
and a pull never replaces a file.

Messages are a code and counts, never a host, path or file name: for example
`finance remote push: 12 new, 3 identical, 1 replaced, 2 skipped.` or `finance remote
push: refused. target_differs (2 files). Nothing was written.` The codes are
`remote_config_not_set`, `remote_config_refused`, `ssh_not_found`, `ssh_failed`,
`ssh_timeout`, `container_not_found`, `container_ambiguous`, `remote_reply_invalid`,
`remote_failed`, `argument_refused`, `into_refused`, `source_missing`,
`source_unreadable`, `source_not_regular`, `source_changed`, `name_refused`,
`key_mode_refused`, `state_export_invalid`, `nothing_to_push`, `packs_missing`,
`io_failed`, the receiver's `frame_invalid`, `stream_truncated`, `hash_mismatch`,
`staging_refused`, `staging_busy`, `staging_cross_device`, `target_outside_layout`,
`target_not_regular`, `target_differs` and `target_appeared`, and the layout's
`finance_layout_missing`, `finance_workspace_not_set`,
`finance_workspace_unavailable` and `finance_module_not_enabled`. Push and pull do not
show ssh's own diagnostics: use `exec` or plain ssh to see why a connection failed.

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

Under core, reminders go to Current in process (ADR-0025), and the
`current_base_url`, `current_credential_file` and `current_token_var` keys are
ignored. Current must be enabled in the workspace, with its `bills` area (Current's
first migration seeds it). Approve the sink with `finance sink approve current` under
core: an approval given over HTTPS does not carry over. A sync is one transaction
with a savepoint per key (ADR-0027). A key Current refuses for itself is set aside
with its code on its row and the other keys sync; a condition of the whole sync (the
sink off, Current unavailable) leaves Current and the sink's rows as they were. A
set-aside key is tried again at every sync with its period's latest fields. A new key
refused before its item existed is not filed while its period stays settled or out of
the desired set, and is filed if the period reopens. That holds in process only: over
HTTPS such a claim is still created and then closed. A Current item deleted by hand
is filed again for an open period the next time its period changes. `finance jobs
health` lists the first twenty set-aside keys with their codes, then a count of the
rest; the others appear there as the keys before them clear when they sync. Status
line 6's queued retries count every one of them, and its "not synced since" is the
last time every key synced. Over HTTPS a refusal still stops the keys after it.

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
