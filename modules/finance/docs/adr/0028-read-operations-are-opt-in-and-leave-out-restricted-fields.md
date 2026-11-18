# ADR-0028: Read operations are opt-in and leave out restricted fields
Status: accepted
Date: 2026-11-12

## Context

ADR-0024 kept every finance operation out of core, so no token, package set or agent
reached the ledger. An agent that may read the ledger's state (what is owed and when,
which months have no statement, which documents need entry) still needs a way in, and
two things stood in the way of simply registering the reads.

- **Core cannot see finance's tier marks.** Finance tiers its output fields with its
  own `Tiered` marker. Core's renderer reads only core's, so to core every finance
  output is untiered and would be answered whole, amounts included.
- **Four of the eight reads hold restricted fields.** `finance.txn.list`,
  `finance.openitem.list`, `finance.owed.list` and `finance.owed.trace` carry amounts,
  totals and a headline written from them.

## Decision

- **The eight reader reads register in core.** Every READ spec on the reader
  connection that is not owner-only becomes a core READ operation with its tool:
  `finance_status`, `finance_coverage`, `finance_documents`, `finance_sinks`,
  `finance_lines`, `finance_open_items`, `finance_owed` and `finance_trace`. Each is
  declared for the owner alone, unaudited (READ writes no audit row) and with no
  idempotency.
- **Opt-in only.** The manifest sets `package_sets="explicit_only"`. No named package
  set (`read_only`, `cli_full`, `agent_default`), no connector grant and no routine
  token reaches them; a `module_read:finance` token does, and it holds these eight and
  nothing else.
- **No restricted field reaches a model.** Each registered output is a view derived
  from finance's own model: a field finance tiers restricted is left out, a public or
  internal one carries core's marker of the same tier, and a field with no finance
  tier refuses the import. The view is filled by reading only its own fields off the
  finance result, so a restricted value is never copied. What an agent sees is every
  field finance tiers public or internal, as finance tiers it: structure, dates,
  states, counts, labels and the status lines' text. Payee, account and document
  labels are public under finance's own tiers and do reach it.
- **The reader role, on core's transaction.** The adapter binds the workspace's
  layout, runs the handler as `finance_reader` with `SET LOCAL ROLE` on core's own
  work transaction, and resets the role before it returns, because core goes on using
  that transaction. After a failure it does not reset: core's rollback ends the
  transaction and the role with it.
- **Fixed-code refusals.** Every failure of a read, in the handler or in building its
  view, leaves as an `OperationRefused` whose state and detail are one fixed code,
  with no class name, message or value and nothing chained: the class's code from the
  table the tick shares (`finance_layout_invalid`, `finance_settings_invalid`,
  `finance_schema_behind`, `finance_operation_refused`, `finance_storage_failed` and
  the rest), `finance_output_invalid` for a handler output of another type, and
  `finance_read_failed` for anything else. Only core's own refusals
  (`ModuleNotReady`, `StorageRefusal`) pass unchanged, and core answers those with its
  own safe state and text.

## Consequences

Replaces one bullet of ADR-0024, "No operations or tools in core". ADR-0024 stays
accepted: its other decisions stand, so this record names the bullet it replaces
instead of superseding the whole record, and ADR-0024 carries a `Partly replaced by
ADR-0028` line naming that bullet. Core's workspace export still carries no finance
row, since the module declares no exportable record type, and state still moves with
`finance state export`. Loading the manifest now loads the ledger's operation
catalogue; importing `rheo_finance` alone still imports no core. The reads run where
the workspace's data root is mounted; a missing layout refuses
`finance_layout_invalid`. `finance_lines` has no row cap, so an unfiltered call can
return every line; that is tracked separately.
