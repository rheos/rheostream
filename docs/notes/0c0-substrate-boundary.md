# Run 0c0 — the substrate boundary

Run 0c0 builds the durable-work **substrate**: the tables, the contracts, and the
discovery index that the two scored cohorts 0c1 and 0c2 build their behaviour on top
of. It deliberately builds none of that behaviour.

This note is the record that says where the line was drawn. It exists so that whoever
freezes the 0c1 cohort reads a stated boundary rather than re-deriving one from a
diff, and so that every reduction in a cohort's task is a decision somebody made and
wrote down rather than a surprise discovered afterwards.

Its machine-checked half is `tests/test_substrate_boundary_note.py`. Its companion is
`tests/test_absent_behaviour.py`, which asserts — with a mandatory positive control on
every probe — that the behaviour named below as a cohort's really is absent from this
run's tree.

---

## 1. Base

- **Base commit:** `3c808f123f10746b3ea2ae012b1810c347b1a9b9`, on `main`.
- **Branch cut:** `bureau/20260913-0c0-durable-work-substrate`, cut from that commit.

Every measurement in this note — the cut-symbol occurrence counts, the absence claims,
the file census — was taken against that commit or against the branch built from it.
A later reader comparing a cohort's tree against this note should start from the same
commit, not from whatever `main` has become.

**Run 0v's spike is deleted at this branch cut.** The throwaway module, its web surface
and its driver are gone; `docs/notes/0v-vertical-slice-findings.md` is the deliverable
that survives, exactly as that run intended. The deletion is checked as a completeness
check rather than a token list — `tests/test_absent_behaviour.py::test_no_spike_remains`
greps the whole tracked tree for a bare, case-insensitive `spike` and asserts the files
still matching are exactly a declared survivor set, each entry carrying its reason. This
note is one of those survivors, because a record of what was removed has to name it.

---

## 2. What the substrate provides

**Eight tables, in two migration chains.**

Seven in the core chain, created by revision `0002_durable_work` in schema `core`
(`packages/core/src/rheo_core/storage/work_tables.py`):

| Table | What it holds |
|---|---|
| `outbox_event` | events written in the same transaction as the change that caused them |
| `event_delivery` | one row per event per consumer, with its delivery state |
| `consumer_processed` | the idempotency ledger a consumer writes to once it has handled an event |
| `job` | queued units of work, with their lease, attempt and cancellation columns |
| `schedule` | recurring job definitions |
| `operation` | the record of one dispatched operation and its terminal state |
| `audit_record` | the audit row an operation's audit declaration describes |

One in the control chain, created by revision `0002_work_index` in schema `control`
(`packages/core/src/rheo_core/storage/work_index_tables.py`): `workspace_work_due`,
the due-work index. It is a second `MetaData` rather than an eleventh table on the
control plane's own, so that the frozen `0001_control_plane` revision still creates
exactly the ten tables it was written to create.

**Two contract modules**, in `packages/contracts/src/rheo_contracts/`:

- `event_envelope.py` — the event envelope, frozen, carrying the ratified row plus its
  delivery context.
- `repositories.py` — the repository protocol, with `save` taking a keyword-only
  `expected_revision` and refusing a stale one with `StaleRecord`. The compare-and-set
  is in the contract because `revision` is the token a record-state guard compares an
  approval against, and a plain increment is a lost update in that path.

**The audit protocol and its registry** (`packages/core/src/rheo_core/audit/sink.py`):
the `AuditSink` protocol, a table of installed sinks keyed by owning module id
(`install_sink`, `sink_for`, `reset_sinks`, `AuditSinkRefused`), and the
`ModuleManifest.audit_sink` field a module supplies its writer through at load time.
The module loader installs a manifest's sink. **Nothing calls one.**

**The due-work index and its three functions**
(`packages/core/src/rheo_core/storage/work_index.py`):

- `workspaces_with_due_work(...)` — a `LEFT JOIN` from `control.workspace`, so a
  workspace with no index row is returned on the first pass rather than missed.
- `mark_work_due(...)` — lowers `due_at` only, never raises it.
- `record_visit(...)` — a compare-and-set against the `due_at` the read observed, so a
  mark committed during a visit cannot be overwritten by the visit's write.

One new settings key supports it: `work.due_reconcile_seconds`, defaulting to 900.

---

## 3. 0c1's task boundary

Everything below is 0c1's to build. None of it exists in the substrate.

- **The worker loop** — the process that walks workspaces and runs due work. The
  substrate offers discovery and nothing else; the loop, its cadence and its shutdown
  are the cohort's.
- **Leasing** — taking a job, holding it, and extending the hold while work continues.
  The `job` table carries the lease columns as DDL; nothing acquires or extends a
  lease.
- **The expired-lease recovery path** — noticing a lease that has lapsed because the
  process holding it died, and making that work available again.
- **Retry and backoff** — the attempt counter and its ceiling are columns; the policy
  that reads them, delays, and gives up is not written.
- **The failure list** — the read that surfaces work which has exhausted its retries.
- **Cancellation** — the column exists; the code that honours it does not.
- **The callers of `mark_work_due`, `record_visit` and `close_idle`.** All three are
  written, tested and exported, and all three have **no production call site**. The
  enqueue path that marks work due, the visitor that records its visit, and the worker
  that sweeps idle engines between passes are 0c1's.

A visitor must build **its own exclusion**. The index is a hint: two readers may
observe the same `due_at` and both visit the same workspace. Whether a unit of work
actually runs is decided by the workspace's own tables and by the visitor's own
leasing, never by the index.

---

## 4. 0c2's task boundary

- **The outbox writer and the fan-out** — writing an event in the same transaction as
  the change that caused it, and expanding one event into a delivery row per consumer.
- **Exactly-once delivery** — the delivery state machine and the use of the idempotency
  ledger that makes a redelivery harmless.
- **The operation record's lifecycle** — creating the record, moving it through its
  states, and clearing an unresolved one. The table and its constraints are substrate;
  every transition is not.
- **Audit dispatch, and the registration-time refusal** — where the call to a sink sits
  inside a transaction, what a missing registration means, and whether a registration
  is mandatory at all. The protocol and registry are substrate; the caller and the
  policy are the cohort's.
- **`core.audit.list`** — the supported read over the audit table.

---

## 5. Moved into the substrate

This is the section a cohort freeze actually weighs. Each item below is behaviour or
constraint that a cohort might otherwise have had to build or decide, and that this run
provides instead. **Every one of them shrinks the scored task by a stated amount.** The
whole point of itemising them is that the reduction is a decision on the record rather
than something a comparison discovers afterwards.

| Moved in | Criterion it touches | What it means for the cohort |
|---|---|---|
| The retained `AuditSink` protocol, the module-keyed registry, and `ModuleManifest.audit_sink` | 14 — the **declaration** half | A cohort inherits a decided interface and does not re-derive one. It still builds the call site, the transaction placement, and the missing-registration policy, which is the scored half. Recorded as **issue #45**. |
| The due-work index: `workspace_work_due`, its three functions, and `work.due_reconcile_seconds` | discovery — **absent** from 0c1's scored list | Workspace discovery is answered for every entrant identically, so the comparison is of worker behaviour rather than of who invented a better scan. Recorded as **issue #46**. |
| `consumer_processed`'s composite primary key on `(consumer_id, event_id)` | 11 | The idempotency ledger's uniqueness is a schema fact rather than something an entrant must get right in application code. |
| `operation`'s `operation_terminal_check_required` check constraint | 13 | A `succeeded` operation is unrepresentable without a recorded terminal check. The schema enforces the half of criterion 13 that would otherwise be an entrant's invariant to maintain. |

Two things to read alongside this table, both recorded in section 6 because they bear
on how far the boundary guard can be trusted: the guard's actual mechanism, and one
bounded limitation in criterion-12's coverage.

---

## 6. The cut-symbol list and its behaviour-coverage table

Twelve tokens guard the boundary. Measured at the base commit with a fixed-string grep
over `main`, excluding the two lock files: **every token below occurs zero times in
`packages/`, `apps/`, `tests/` and `scripts/`.** The only occurrences are in
`docs/architecture/**` and `docs/notes/0v-vertical-slice-findings.md`, which are
specification and history. A hit in a code path is therefore a real crossing.

Run 0v's list could not be reused. This run legitimately creates `job`, `schedule`,
`outbox_event`, `event_delivery`, `consumer_processed`, `operation` and `audit_record`,
and legitimately writes the lease, cancellation and attempt columns as DDL; thirteen of
0v's nineteen tokens would have fired on this run's own correct diff. The list below
names **behaviour** instead — the operations the cohorts register and the functions they
must write — none of which this run produces.

| # | Token | A cohort crossing contains it | This run's diff does not |
|---|---|---|---|
| 1 | `acquire_lease` | criterion 12's lease step needs a named acquisition, and this repository names such things `verb_noun` | 0c0 writes no function that takes a lease; its only job code is DDL |
| 2 | `worker_loop` | the loop itself, 0c1's central deliverable | the worker app gains one docstring and no callable |
| 3 | `heartbeat` | extending a lease while work continues | zero occurrences anywhere on `main`; nothing here extends a lease |
| 4 | `def publish(` | the outbox fan-out at write time, 0c2's | the unit of work is untouched, and a test pins its public surface |
| 5 | `core.work.replay` | 0c2's replay operation | 0c0 registers no operation at all |
| 6 | `core.work.retry` | 0c1's failure-recovery operation | same |
| 7 | `core.work.skip` | 0c1's head-of-line release | same |
| 8 | `core.work.failures` | criterion 12's failure list | same |
| 9 | `core.work.failure_summary` | the shell banner read, 0c2's | same |
| 10 | `core.audit.list` | criterion 14's supported read | same |
| 11 | `core.operation.resolve` | criterion 13's clearing of an unresolved record | same |
| 12 | `core.retention_sweep` | the core's own scheduled job, 0c1's | 0c0 creates the schedule table and seeds no row |

**Coverage, in both directions.** Every scored behaviour — taken from the public
criteria, not from any planning material — maps to at least one token, and every token
guards at least one behaviour:

| Scored behaviour | Cohort / criterion | Tokens |
|---|---|---|
| leasing | 0c1 / 12 | 1, 2 |
| recovery after process death (expired lease) | 0c1 / 12 | 1, 2, 3 |
| retry exhaustion and the failure list | 0c1 / 12 | 6, 8 |
| head-of-line release | 0c1 / 12 | 7 |
| cancellation | 0c1 / 12 | 2 |
| exactly-once outbox delivery | 0c2 / 11 | 4, 5 |
| operation records and terminal status | 0c2 / 13 | 11 |
| mandatory audit dispatch, missing registration | 0c2 / 14 | 10 |
| failure surfacing | 0c2 / 13 | 8, 9 |
| scheduled jobs | 0c1 / 12 | 12 |

> **One row here is not a transcription.** The source tables carried token 7,
> `core.work.skip`, with its own column-(i) justification — 0c1's head-of-line release
> — but no row in the coverage table referenced it, so the second direction did not
> hold for that token. The head-of-line release row above closes that gap. No token was
> added, removed or reworded; a behaviour column (i) already named simply gained the
> row that names it back. Both directions are asserted mechanically by
> `tests/test_substrate_boundary_note.py`, which is how the gap surfaced.

### (a) The mechanism, and a withdrawn claim about it

The boundary guard is a **plain substring test over raw diff text**. The build
framework's scope gate at `integration-gate.sh:430-435` runs `git diff base...HEAD`,
keeps the whole of that output as `full_diff`, and then evaluates, in one line,
whether each token appears anywhere in it. Context lines and hunk headers are part of
that text and are scanned exactly like added lines.

**There is no added-lines-only pass anywhere in the framework**, and an earlier claim
made during this run — that such a scan existed and was the binding one — was
**withdrawn as incorrect**. This is recorded rather than quietly dropped because the
token list's own justification depends on which mechanism is the real one: a guard
argued from a scan nobody implemented reads as coverage while providing none.

Two consequences follow from the real mechanism, and both are load-bearing rather than
tidiness. Four otherwise obvious leasing tokens — `lease_owner`, `lease_until`,
`cancel_requested` and `SKIP LOCKED` — stay **off** the list, because a default
three-line context window around this run's documentation amendment already pulls all
four into the scanned text from surrounding unmodified lines. And this run's amendment
to the worker-loop paragraph is confined to a narrow line range and names no
`core.work.*` operation, because widening it downward would pull a token into context
and fire the guard on a documentation edit.

**This note is the one declared place the twelve tokens appear.** It fires the guard
twelve times out of twelve, by construction, and that is a precondition of the
deliverable rather than a surprise: a record whose job is to name what was cut has to
name the tokens.

### (b) One bounded limitation in criterion-12's coverage

Criterion 12's coverage rests on tokens 1 and 2, `acquire_lease` and `worker_loop`.
Both assume an entrant names things `verb_noun` the way this repository does —
`open_unit_of_work`, `install_sink`. **An entrant that takes a lease inline, or under a
differently-named function, would trip neither token.**

This is recorded as a known limitation, not filed as a defect, and the reason is that
the tokens which would close the gap are exactly the four that measurement disqualifies
above. It sits here beside the two substrate declarations of section 5 — the retained
audit protocol (**issue #45**) and the due-work index (**issue #46**) — so that a
cohort freeze inherits a **known limitation rather than a false guarantee**.

### (c) The two self-declared exemptions to this guard

There are **TWO** exemptions, not one, and the count is itself the point: an exemption
list that a later reader believes is shorter than it is, is worse than a longer one
honestly stated.

1. **This note.** It fires the guard 12/12, as (a) states. The exemption is scoped to
   the file: a hit is a real crossing if and only if it falls outside
   `docs/notes/0c0-substrate-boundary.md`. What keeps it honest is that the file's own
   content is asserted — `tests/test_substrate_boundary_note.py` pins both tables in
   both directions, so a thirteenth token, or a token present without a coverage row,
   fails. The exemption has nowhere to smuggle anything.
2. **The A-NARROWED `worker_loop` waiver.** Token 2 is exempt **only** where its
   occurrence falls inside the literal string `test_no_worker_loop_exists`, in
   `tests/test_absent_behaviour.py`, and nowhere else — not elsewhere in that file, and
   not in any other file. A bare occurrence of the token anywhere else still fires. The
   waiver exists because the acceptance criteria fix that probe's name exactly, which
   makes the hit certain before a line of it is written.

**Why renaming the probe was rejected on the merits.** Not on a revision budget, and
not because renaming was harder — a later reader must be able to see from this section
alone that the guard was narrowed on reasoning rather than for convenience. Three
reasons:

1. **The name is pinned twice.** The criteria fix it as the function's name *and* as a
   key in the exact-match `{probe: covers}` map that `test_every_criterion_is_covered`
   asserts. That map exists precisely to stop permuted labels, so renaming to dodge a
   substring collision would edit both halves of the assertion built to prevent
   relabelling.
2. **Its four sibling probes are each named for the absence they assert.** Renaming
   this one would make it the only probe in the module whose name does not say what it
   checks — a readability cost paid permanently to satisfy a grep.
3. **The hit is a false positive the criteria themselves guarantee.** The guard targets
   **behaviour**; this probe asserts that behaviour's **absence**. The token appears in
   the tree precisely because something is checking that the thing it names is not
   there. Silencing that by renaming would hide the check rather than the crossing.

---

## 7. What is deliberately not provided

**Three functions with no production caller.** Each is written and tested and reachable,
and nothing in the shipped code calls it:

- `mark_work_due` — waits for 0c1's enqueue path. Nothing enqueues durable work yet, so
  there is nothing to mark.
- `record_visit` — waits for 0c1's visitor. Recording a visit is meaningless until
  something visits.
- `close_idle` — waits for 0c1's worker, which is the caller the sweep was written for:
  a process that wants engines reclaimed between passes without asking for one.

Shipping them now rather than with their callers is deliberate: they are the contract a
cohort builds against, and a cohort that had to invent them would be compared on
inventing them rather than on the behaviour being scored.

**Two bounds a cohort meets rather than discovers.** Both are properties of the
substrate, stated here so that an entrant plans around them instead of finding them
under load:

1. **`mark_work_due` is a control-database write on the enqueue path.** The control
   engine is created with `pool_size` equal to the configured maximum connections and
   no overflow, which is the same small budget every other control-plane read and write
   contends for. Every enqueue therefore competes for that one pool, so under load the
   contention is on the **enqueue** side rather than on the visitor's.
2. **The index is a hint, and its only guarantee is a bounded upper delay.** It is not
   a queue and not a lease. An enqueue writes to a workspace database while the mark
   writes to the control database, and no transaction spans the two; a crash between
   them loses the mark with no error and no log line. The reconcile interval is what
   bounds that loss. A workspace is rediscovered within one interval regardless, which
   is the whole of what the index promises.

---

## Publication note

This is a public record, written from this repository's own code and its public
acceptance criteria. Nothing in it is quoted from any private planning material, and it
names no path outside this repository.
