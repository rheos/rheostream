# Finance

A personal ledger and reminder module. It reads your own bank, card and bill
statements into one schema, `finance`, shows what you spend and what you owe, sends
due-date reminders, and watches crypto wallet holdings through a portfolio price service (`teak`).

It is not an accounting or tax product. Tax packs are inputs for an accountant and say
so; what the ledger does not compute is listed in the design's open questions.

## Status

Imported, not loaded. This directory holds the code, tests and decision records of
the standalone project it started as (Float), copied in without its Git history. The
distribution is `rheo-finance`, the package `rheo_finance`, the module id `finance`.

There is no `rheo.modules` entry point and no console script yet, so no deployment
loads it and nothing runs it. The wiring comes next, in its own change: the manifest,
roles created at install, the tick as a core scheduled job, workspace paths, a
Postgres advisory lock for the writer, and a model client over the runtime contract.
Until then the code still reads its database credentials from
`$FINANCE_DATA_DIR/db.env`, as the standalone project did.

Not imported, because rheoStream owns those jobs: the standalone server runtime (the
foreground scheduler, the LaunchAgent installer, `finance db up`, `db init`,
`db migrate` and `db locked`), the native PostgreSQL 17 dump and restore seam, the
direct `claude -p` model client, and the local parity check against the earlier
parsers.

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
