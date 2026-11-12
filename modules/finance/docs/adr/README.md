# Architecture decision records

Each file here records one decision that is hard to reverse, surprising without context, and the
result of a real tradeoff. Files are named `NNNN-lowercase-slug.md`; a new ADR takes the next
number after the highest one present and follows the bureau ADR template,
`templates/adr.md` in the bureau framework (heading, `Status:`, `Date:`, Context,
Decision, Consequences). ADRs 0001 to 0009 were written in Phase 1. The remaining design decisions
(Key decisions 4, 6, 8 to 13, 15 and 17 to 20 in the design spec) each get their own
next-numbered ADR in the PR of the phase that first builds them. A changed
decision gets a new ADR, and the old one's `Status:` becomes `superseded-by-NNNN`; old bodies are
not rewritten. A decision that a later ADR narrows without replacing keeps its status and gains an
`Amended-by: ADR-NNNN` line after `Date:`; its body is not rewritten either. A record whose single
decision a later ADR replaces while its other decisions stand keeps `Status: accepted` and gains a
`Partly replaced by ADR-NNNN` line after `Date:` naming the replaced decision; its body is not
rewritten.

ADR-0017 superseded ADR-0007 for private server residency and scheduling. ADR-0021
supersedes ADR-0017: Float runs as a rheoStream module in the main workspace
and the standalone server runtime is dropped; local development paths remain supported.

ADR-0018 records payment identity and issue-window targeting; ADR-0019 records
decision target snapshots and orphan proposals; ADR-0020 records the overdue
threshold and carried balance.

ADR-0022 supersedes ADR-0001: inside rheoStream the four `docextract` modules the
ledger uses are vendored, pinned by their bytes, rather than installed from a private
git dependency. ADR-0023 records the codenames that name every institution and the
private file that holds their own wording.

ADR-0024 records how the module runs on core: roles made by `env.py`, one role per
operation on core's sessions, an advisory writer lock, the tick as a gated core job,
the uploads layout, no core operations or tools, and model calls through the runtime.
It supersedes nothing; ADR-0008's two connection names now map to the two roles.

ADR-0025 records how reminders reach Current under core: Current's four item
operations in process with a restricted service context, the gate asked before every
call, an in-process spec that no HTTPS approval opens, one transaction per sync that
commits only when synced, each call at the session role in a savepoint, and updates
scoped to the key's own source pair. It extends ADR-0005 and ADR-0014 and supersedes
nothing.

ADR-0026 records how the laptop reaches the module: `finance remote push`, `pull` and
`exec` over ssh and `docker exec`, the container found by its compose labels on every
call, file names only inside a framed stream checked against one path rule, a receiver
that stages and checks everything before it links anything into place, refusal by
default with `--replace` for the owner's edited inputs alone, keys only with `--keys`,
and fixed-code messages. It extends ADR-0024's layout and supersedes nothing.

ADR-0027 records that the tick's long steps beat the job's checkpoint so the lease
holds, and that a reminder sync in process takes a savepoint per key: a key refused
for itself is set aside with its code and the others commit, a global condition still
rolls back all, a stored item Current no longer has is filed again for an open period
and forgotten for a settled one, and `finance jobs health` names each set-aside key.
It extends ADR-0024 and replaces one bullet of ADR-0025 ("one transaction per sync,
committed only when synced") and its sentence on a hand-deleted item. ADR-0025 stays
accepted, because its other decisions stand, and carries a `Partly replaced by
ADR-0027` line; ADR-0027 names the one bullet it replaces instead of superseding the
whole record.
