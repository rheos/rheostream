# ADR-0027: The tick keeps its lease and sets a refused reminder aside
Status: accepted
Date: 2026-11-12

## Context

Two things stood between the tick and the owner opening `finance.jobs.enabled`.

- **#546.** Core's worker leases a job for 60 seconds and lets a second worker take a
  leased row once the lease runs out. The tick checked the cancellation token before
  each due job and while a model call waited (ADR-0024), but nothing inside a step
  renewed the lease. A first archive walk, a full reparse or a long rates fetch could
  outlast it, and a second worker could start the same tick beside the first.
- **#548.** A reminder sync in process was one transaction committed only when every
  key synced (ADR-0025). One key that Current refused (a retired area, an item that is
  not the sink's, an item deleted by hand) stopped every key of every sync until the
  owner handled it, and its code was only logged, because the annotation rolled back.

## Decision

- **Long steps beat the job's checkpoint.** `FinanceContext.progress` is a no-op by
  default; the tick's wiring sets it to the token's checkpoint, the same callable the
  tick and the model client get. The archive walk and the Downloads filer beat before
  each entry, stage 1 before each document, derive before each stage and each
  obligations round, the rates fetch before each series and the reminder sync before
  each key. The checkpoint is throttled, so a beat costs one round trip at most every
  20 seconds and keeps at least 40 seconds of lease. Every beat sits outside the
  excepts around it: a lost lease or a cancel leaves the step, the step rolls back and
  the tick stops unrecorded. This extends ADR-0024's "checks the cancellation token
  between jobs". The step packages still import nothing of core.
- **A savepoint per reminder key.** A sync in process is still one session, one
  writer lock and one commit, and each key's pass now runs in its own savepoint, so a
  key's Current writes and its `sink_ticket` writes commit or roll back together.
- **A key refused for itself is set aside.** For `refused`, `record_stale`,
  `not_owned`, `not_found`, `incomplete_convergence` and `protocol_error` the key's
  savepoint rolls back, its row keeps the code and the attempt time (a new key gets a
  `post_pending` claim with the code), and the loop goes on. The sync commits when the
  loop finishes and reports `partial`, which the job records as `ok=false, failed`.
  A claim fronts no item in process, so it is created from the period's latest wanted
  fields, not the snapshot it was claimed with, and one whose period settles or leaves
  the desired set before its first create is acknowledged with no call.
- **A global condition still rolls back all.** `sink_off`, `not_configured`,
  `current_unavailable`, the HTTPS transport's own codes, a payload refusal, a storage
  error, a lost lease and a cancel stop the sync and leave Current and the sink rows
  as they were.
- **A stored item Current no longer has.** In process Current's `not_found` is a code
  of its own. When the read of a stored item finds it gone, an open period is claimed
  afresh and created once more, which Current folds into any holder of the pair; a
  settled period is forgotten and never created again (ADR-0014). The read happens
  only when the period's wanted state changed or the key still carries a code: an
  acknowledged, unchanged key with no code makes no call, and a key whose fields went
  back to the acknowledged ones is read once, which clears its code. That read checks
  the item's source pair as every mutation does, so an item that is not the key's
  keeps it set aside with `not_owned`, even when every field matches. A write that
  finds the item gone sets the key aside until the next read.
- **Health names the keys.** `finance.job.health` returns each key whose row carries a
  code, with the code (`other` for a stored code outside the fixed set) and when it
  last synced, at most twenty and a count of the rest; `finance jobs health` prints one
  line per key. The status view is unchanged: its retry count already counts those
  rows.

## Consequences

Replaces one bullet of ADR-0025, "One transaction per sync, committed only when
synced", and its Consequences sentence on a hand-deleted item or a repeating refusal
stopping every key. ADR-0025 stays accepted: its other decisions stand, so this record
names the bullet it replaces instead of superseding the whole record, and ADR-0025
carries a `Partly replaced by ADR-0027` line naming that bullet. Over HTTPS a
refusal still stops the keys after it, the earlier keys having committed in their own
steps, and `not_found` is still `refused`; only the read that clears a stored code
applies there too. A claim over HTTPS may front a create whose answer was lost, so its
retry replays the snapshot it was claimed with, which Current folds into the item it
may already hold, and one whose period settles or leaves the desired set is still
created and closed. A refused key is retried every tick with no backoff. One pdftotext
call, one derive stage or one rates reply that alone runs past 40 seconds can still
lose the lease; those are tracked separately.
