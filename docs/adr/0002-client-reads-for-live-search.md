# ADR-0002: Client components read through an opt-in shell action
Status: accepted
Date: 2026-11-27

## Context

Module screens are server components. They read through `ShellApi.call`, which
the shell limits to the module's own READ-class operations (#122), and the only
thing a client component could do was `submit` a declared MUTATE or DRAFT form,
which refreshes the page after it succeeds.

That left no way to search as someone types. Current's dependency picker (#645)
first listed 100 items in a select, then reloaded the whole page on every search
and printed the matches into the sidebar. Both were unusable. A picker needs to
ask the API for matches without leaving the dialog it is in.

## Decision

- **`ShellApi.read`.** A server action (`apps/web/src/shell/client-read.ts`) a
  client component calls with an operation name and input. It runs the same
  checks as a form submit on every call: a signed-in session, the workspace the
  page was rendered for, the module enabled and its surface routed. It never
  refreshes the page.
- **Opt-in per operation.** A module lists the reads its client components may
  make in its manifest's `web.client_reads`. The manifest refuses any name that
  is not one of the module's own READ operations. `rheo web compose` writes the
  list into `modules.generated.ts` as `clientReadOperations`, and the shell
  copies it once at load and allows only names that are also in
  `readOperations`. A module with no client reads gets no `read` at all.
- **Current declares two.** `current.item.list` and `current.item.get`, for the
  link dialog. `current.item.list` gains `match: "title"` so a picker lists only
  items whose title has the typed words; the default (`"all"`) is unchanged.

## Consequences

- Any read a module adds to `client_reads` can be called from the browser by the
  signed-in user, as often as they like. Core still checks the role on every
  call. A read that fills owner-only fields for a browser session (Finance,
  ADR-0029 in `modules/finance/docs/adr/`) must not be listed without its own
  decision.
- Each keystroke pause in a picker is one server action and one core call. The
  dialog waits 200 ms after typing stops and drops answers to older searches.
- Server actions are POST-only and Next checks their origin, as for submits.
