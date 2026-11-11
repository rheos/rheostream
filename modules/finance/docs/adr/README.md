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
`Amended-by: ADR-NNNN` line after `Date:`; its body is not rewritten either.

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
