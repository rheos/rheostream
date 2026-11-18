# ADR-0029: The owner's browser session reads the figures
Status: accepted
Date: 2026-11-18

## Context

ADR-0028 registered the eight reader reads with every restricted field left out, so
the same reads could serve a `module_read:finance` token without handing an agent an
amount. The finance web screens (#584) call those reads through the owner's own
signed-in browser session, and a ledger view with no amounts (what is due, what was
paid, the credits) leaves the owner nothing to act on.

Core's tier policy governs what reaches a model: the tool facade renders every
answer and never sends a restricted field. The HTTP path a browser uses renders
nothing; it returns the operation's registered output as it is. So the only thing
that kept amounts from the owner's screen was finance's own view.

## Decision

- **Two views per output.** The shared view is ADR-0028's, unchanged: restricted
  fields left out. The registered output is the owner view: the shared view's fields,
  plus each restricted field kept, optional, `None` by default, under core's
  `restricted` mark.
- **Filled for one caller.** The adapter fills the restricted fields only for the
  workspace owner's own browser session: an account actor under a `session` audience,
  in the owner role (`context_from_session` builds exactly that). Every other caller
  (a `module_read:finance` token, the operator, a runtime run, a connection) gets the
  shared view's values read into the owner view, so every restricted field is `None`.
- **Nothing new reaches a model.** No operation, tool, package set or token set is
  added. Core's tool facade still drops a restricted field from any tool answer, and
  core's walk finds every finance restricted path marked restricted in the registered
  output, which the read tests pin.

## Consequences

- The owner's screens show amounts, balances, payments and credits. Agents and tokens
  see what they saw under ADR-0028.
- The registered output schema gains the restricted fields, optional. A client that
  reads them must handle `None`.
- Widening a token's view of amounts stays a separate decision (#568).

Partly replaces ADR-0028's "No restricted field reaches a model" bullet: still true of
every model and token; no longer true of the owner's browser.
