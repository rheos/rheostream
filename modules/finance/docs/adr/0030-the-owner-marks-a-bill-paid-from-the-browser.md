# ADR-0030: The owner marks a bill paid from the browser
Status: accepted
Date: 2026-11-26

## Context

A bill stays on Owed, and its reminder stays open in Current, until the statement
that shows the payment is imported, which can be weeks after the bill was paid. The
ledger already had the decision for "treat this as paid" (`presumed_paid`), but only
the confirm queue offered it, and only for history before the reminders floor. Every
finance write ran through the `finance` command; core reached only the eight reads
(ADR-0028), and the screens could only read (ADR-0029).

## Decision

- **Two writes reach core.** `finance.obligation.report_paid` records a
  `presumed_paid` decision on one owed obligation and sets what the next derive would
  set, so it leaves the list at once. `finance.obligation.unreport_paid` reverses it.
  Reporting again reverses the earlier decision first, so the button leaves at most one
  standing. Both are MUTATE, owner-only in finance, for the owner in core, with no
  tool, and audited by core's own sink with no subject (finance has no record type
  core resolves). Every other finance write stays with the command.
- **The owner's own browser session alone.** The adapter refuses every caller but the
  workspace owner's signed-in browser session (`owner_session`, as ADR-0029 fills the
  amounts) before it reads anything, so no token, model, runtime run or connection can
  mark a bill paid. The screens submit through the shell's form path: one form
  declaration per write, so the shell lets Finance submit those two and nothing else.
- **The derive checks it.** A reported bill stays presumed paid until a payment meets
  its threshold (a card's minimum, every other kind's amount due), which confirms it.
  Once every account the payee is paid from has a loaded statement ending
  `finance.payments.reported_paid_lag_days` (default 5) after the local day it was
  reported, and the payment is still not there, it goes back to owed with the decision
  still named: Owed flags it, Needs attention lists it, and its reminder returns. A
  statement not loaded yet never reads as "not paid". History before the floor is
  never checked.
- **One writer at a time.** The adapter takes the ledger writer lock, waiting ten
  seconds at most, so a click never lands inside a derive that already read the
  decisions.

## Consequences

- The owner can clear a paid bill from Owed and Current the day it is paid.
- A payment made from an account outside the payee's paying accounts reads as
  unconfirmed once those accounts' statements are in. The owner can mark it paid again
  (the check restarts from that day) or set `paid_from` for the payee.
- Narrowing `owner_session` to a live session that a rebuilt context can never
  reproduce stays #607; it now guards a write as well as the amounts.
- Partly replaces ADR-0028's "nothing above READ" and ADR-0029's "no operation is
  added": two owner writes now exist on core's side, for the browser session alone.
