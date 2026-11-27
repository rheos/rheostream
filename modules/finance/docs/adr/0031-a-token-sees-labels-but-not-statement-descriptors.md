# ADR-0031: A token sees labels but not statement descriptors
Status: accepted
Date: 2026-11-26

## Context

ADR-0028 left every field finance tiers `restricted` out of what a token gets, and
let the rest through: a `module_read:finance` token sees each read's public and
internal fields. Several of those carry the owner's own text, and a figure written
into text is not caught by the amount fields being withheld. Issue #568 asked for a
decision per field group before any real `module_read:finance` token is issued.

Two facts shaped it.

- **`internal` withholds nothing from a token today.** A token issued with no
  purpose renders under `internal_analysis`, which the package default of
  `redaction.internal_purposes` includes. Turning `internal` off means removing that
  purpose for the whole workspace, Leads included. So each group comes down to
  "a token sees it" (public) or "only the owner's browser sees it" (restricted, which
  ADR-0029 still fills for the owner's own session).
- **Statement descriptors carry figures.** A line's `display_label` is the text the
  bank or card printed for it. On the live ledger, some descriptors print the
  charge's dollar amount (a currency conversion, a transfer), and some print a long
  account or card number. The status lines, the reasons, period labels and payee
  labels held none.

## Decision

The owner chose each group (2026-11-26):

- **Statement descriptors are restricted.** `display_label` on `finance_lines` and
  `label` on `finance_upcoming` (built from the same descriptor, or the payee's label
  for a bill row). A token sees a line's dates, states, account and categories, and an
  upcoming charge's date, account, cadence and `payee_slug`, but not the statement
  text. The owner's screens still show it.
- **Payee and account names stay public.** `payee_slug`, `payee_label`, `account`,
  `account_label`, `issuer_label`, `period_label` and `no_data_accounts`, on every
  read that has them. An agent can say which bill is due when, and not how much.
  Current's reminders, which agent tokens can already read, name the same payees.
- **Categories stay public.** `category`, `tag`, `business_unit`, `business_pct` and
  `exclusion`, the owner's own classification, with no figure in them.
- **Paths and free text stay public.** Document, evidence and gap paths,
  `invoice_folder`, the status lines and the open item and coverage reasons. The paths
  are archive directory and date; the text is built from codes, counts, dates and
  labels. A test pins the columns each reason expression reads and each status fact
  projects into its text, so a new column is a deliberate change and a raw SQL
  fragment refuses, and another checks the fixture ledger's status lines and coverage
  reasons hold no figure though its lines and bills do.

Nothing moves to `internal`: it would withhold nothing until the workspace changes
its purposes, and then it would also withhold Leads data.

## Consequences

- A `module_read:finance` token can now be issued for real. What it sees is the shape
  of the ledger and who is owed when, with no amount and no statement text.
- The registered owner view gains `display_label` and `label` as optional, `None` for
  every caller but the owner's browser session. The Upcoming screen reads `label`
  through a helper that accepts a missing value; no screen reads `display_label`.
- Upcoming bill rows lose their payee label for a token; `payee_slug` names the payee.
- This holds field by field, not by masking text: a future field that copies a
  descriptor, or any other text that can hold a figure, must be tiered restricted when
  it is added. No test catches a new public field that does.
