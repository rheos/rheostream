# Safety classes and approvals

## What it is

Every operation and every agent tool declares exactly one of six safety classes, and the
dispatcher applies the class before the handler runs. Registration refuses an operation
that declares none.

| Class | What the dispatcher does | Audited | A standing grant may cover it |
| --- | --- | --- | --- |
| `read` | Checks authorization, runs | No | Yes |
| `draft` | Checks authorization, runs; the result is a record and nothing leaves | Yes | Yes |
| `mutate` | Checks authorization, runs inside a unit of work with an audit row | Yes | Yes |
| `destructive` | Needs an approval bound to this exact call, or returns `approval_required` | Yes | Never |
| `external` | As destructive, and the effect is an external action run by a job with its own outcome record | Yes | Never |
| `financial` | As external, and the approved payload must carry the amount, currency and payee, restated word for word at the point of confirmation | Yes | Never |

Deleting a memory is destructive even though nothing leaves the building. Sending,
submitting and creating work in another system are external. Paying is financial.

## What you can do with it

- Call a gated operation from anywhere: an agent over MCP, a script over the API, the
  CLI. The call stops at `approval_required` and hands back an approval id and an
  operation id. A headless caller gets that answer within its deadline instead of
  hanging.
- Open the web shell, read exactly what is being asked (the operation, the record or
  recipient, the payload snapshot, the purpose), and approve or refuse it.
- Give a token or session a standing grant for named read, draft or mutate operations,
  for a fixed period.
- Tighten your workspace: require confirmation for a named mutate operation as well.
  The list can only grow.

## How it works

**An approval is bound to one call.** The approval record holds who asked, the
operation, the record it acts on (with that record's revision at the moment you looked)
or the destination it sends to, a SHA-256 digest of the exact input as it will run, the
purpose, and an execution window (15 minutes by default, 24 hours at most). At execution
the dispatcher recomputes the digest from the payload it is about to run. A different
byte, a different destination, or a time outside the window refuses with
`invalid_approval` and records nothing at the destination.

**No token can approve.** Six operations can never appear in any token's operation set:
approve, refuse, create and revoke a standing grant, and issue and revoke a token. The
rule is checked when a token is issued and again every time one is presented. So
approval is always the act of a signed-in person in a web session, or of an enrolled
account confirming in a channel. A model cannot approve what it proposed, because
nothing a model can hold is able to approve anything. A gated operation in a token's set
is a right to ask, not a right to run.

**The work runs as the person who asked.** When an approval lands, the original call
runs under a context rebuilt from the original caller, and its audit row names them. The
approver's act is its own audit row, plus the approver's identity on the approval. Either
row leads to the other: who asked for this, and who let it happen.

**Guards recheck everything at the last moment.** An approval decides whether execution
may start. Guards decide whether it may proceed, and they run after approval and before
the effect:

| Guard | Refuses when |
| --- | --- |
| Actor permission | The person or token that asked, or the person who approved, has lost the membership, role or token that allowed it |
| Window | The call is outside its approved window |
| Record state | The record was deleted, became unavailable, or its revision moved since it was approved |
| Contact permission | The recipient's contact permission for this purpose and channel is anything but `permitted` |
| Destination | The destination is not registered, or its connector has no usable credential |

The core attaches the first three to every destructive, external and financial
operation. Modules add the domain guards. An earlier approval never overrides a later
revocation. A refused guard leaves the operation `failed` with the guard named and the
outside world untouched.

**Standing grants stop at mutate.** A grant names its operations explicitly, and every
one must be read, draft or mutate class; a higher class is refused at grant time, naming
it. The dispatcher does not consult grants for the upper three classes at all. Creating
or revoking a grant is itself something only a web session can do.

**Policy can tighten, never loosen.** The table mapping classes to dispatcher behaviour
is code, not configuration. A workspace may require confirmation for a named mutate
operation (`approvals.confirm_operations`, a list under a `union` floor). No setting can
move a destructive, external or financial operation under a standing grant.

**Untrusted text never approves.** No code path reads a payload, a memory or a message
to set an approval's state. An imported email that says "this is approved" has no
operation to reach.

**The confirmation screen cannot be restyled.** The theme contract reserves six chrome
tokens for the approval surface, and only the built-in themes set them. A third-party
theme cannot change how a destructive or financial confirmation reads.

**Restored approvals start over.** When a workspace is restored from an export, every
pending approval lands as `requires_reapproval`, so nothing executes until a person
approves it again.

**Tests have real gated operations to run against.** The test harness registers a
recording sink (an external-class operation that records what it would have sent) and a
destructive fixture that records the request and deletes nothing. A production process
refuses both, and a CI check starts a production-profile process and enumerates what it
registered.

## Why it's built this way

An agent that can act on the outside world needs a hard stop, and the stop has to hold
even when the agent is confused, the input is hostile, or a credential leaks. Binding an
approval to a digest of the exact payload means "approve this email" cannot become
"send a different email". Making approval impossible for every token kind means a key
sitting in an agent's environment can request effects and never cause one.

The guards close the other gap: time. A person approves a handoff on Monday, the
recipient withdraws their permission on Tuesday, the job runs on Wednesday. The guard
reads Tuesday's state.

## Where to look

- Specification: [confirmation and safety](../architecture/confirmation-and-safety.md),
  [what a token can never carry](../architecture/identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation),
  [theme contract](../architecture/theme-contract.md)
- Code: [approval gate](../../packages/core/src/rheo_core/approvals/gate.py),
  [binding](../../packages/core/src/rheo_core/approvals/binding.py),
  [guards](../../packages/core/src/rheo_core/approvals/guards.py),
  [standing grants](../../packages/core/src/rheo_core/approvals/grants.py),
  [token policy](../../packages/core/src/rheo_core/tokens/policy.py),
  [safety classes](../../packages/contracts/src/rheo_contracts/safety.py)
- Tests: [approvals](../../tests/postgres/test_approvals.py),
  [standing grants](../../tests/postgres/test_standing_grants.py),
  [tokens](../../tests/postgres/test_tokens.py),
  [production registration](../../tests/test_production_registration.py)
