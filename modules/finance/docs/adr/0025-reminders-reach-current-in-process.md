# ADR-0025: Reminders reach Current in process
Status: accepted
Date: 2026-11-11

## Context

The Current sink sent each of Current's four item operations as an HTTPS POST and
committed a short transaction around each step (ADR-0014). Under core both modules
live in one workspace database, and Current admits a service context on `create`,
`update` and `set_status`. Over HTTPS the sink needed a base URL, a token file and a
network round trip per request, and a lost create answer was possible. Current also
lets a service edit any item, not only the ones it filed.

## Decision

- **Four operations through `call_in_transaction`.** Under core the wiring binds an
  in-process client for the tick and the CLI. It calls `current.item.create`, `get`,
  `update` and `set_status` with a system service context limited to those four
  names. Finance imports nothing of Current: it names the operations only.
- **The gate before every call.** The sink asks ADR-0005's three-part gate before
  each in-process call, as `http.Client` asks it before each socket. No socket is
  opened.
- **Its own spec.** The in-process spec has the same four request templates, no host,
  no key file and a `transport = in_process` data input. Every HTTPS spec carries a
  `token_var` input, so no HTTPS approval opens the in-process gate and none carries
  over: the owner approves again under core.
- **One transaction per sync, committed only when synced.** The session holds
  finance's writer lock, finance's `sink_ticket` writes, Current's items, events and
  core's audit rows. A refusal, a pending result, a payload refusal or a storage error
  leaves Current and the sink rows as they were; the next tick tries again. The other
  tick steps keep their own commits.
- **Role discipline.** Finance's own reads and writes run as `finance_owner` in role
  blocks on that transaction. Each Current call runs in a savepoint after `RESET
  ROLE`, at the session role, and the block returns to `finance_owner` after it. A
  refused call rolls back to the savepoint, role included.
- **Scope.** An update or status change reaches only an item that carries
  `float.obligation` and the key's own `finance:<token>:<period>` pair, checked on
  every read before the write and on each create answer; otherwise `not_owned`.
- **Fixed codes.** A refusal leaves as `current_unavailable`, `record_stale`,
  `refused`, `protocol_error` (an answer the sink cannot read) or `not_owned` (the
  scope check above), with nothing of its state or detail kept. Each is logged once:
  a refused call by the session, naming the operation and the code; a code the sink
  raises after a call returns, by the sync, with the code alone. Storage errors pass
  unchanged.
- **HTTPS stays** the path when nothing is bound: the module's own HTTP tests and the
  source export.

## Consequences

Extends ADR-0005 (another gate caller) and ADR-0014 (the same key and two dedup
layers); supersedes nothing. A hand-deleted Current item or a repeating refusal (a
retired `bills` area) now stops every key of the sync, not only the keys after it,
until it is handled. In core mode the refusal code is logged, not stored on the sink
row, because that write rolls back with the rest. A ledger rebuilt without the binding
and one rebuilt with it differ in `sink_spec`.
