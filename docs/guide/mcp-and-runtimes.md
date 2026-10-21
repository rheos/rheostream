# The MCP door and replaceable runtimes

## What it is

Two halves of how models meet rheoStream.

**The MCP door** is one MCP server, served by the core process over streamable HTTP on the
`mcp` surface (`mcp.<your host>/` in subdomain mode, `/mcp` in path mode). Every agent
client reaches every module through it: Claude Code, a claude.ai custom connector, a bot,
or rheo's own model runs. A tool is a name and a description over a registered operation.
It has no handler of its own, so it cannot reach storage except through that operation.

**Runtime adapters** run a model's conversation loop when rheoStream itself starts a model
run. The Claude CLI adapter spawns `claude -p`; the OpenRouter adapter calls the
provider's API and supplies its own loop; Codex CLI is a further target held to the same
contract. The runtime owns conversation mechanics. rheoStream owns identity,
authorization, tool policy, domain data, audit and durable work state.

## What you can do with it

- Point Claude Code or any MCP client at the door with an `mcp` token. The token belongs
  to one account in one workspace and carries one set of operations.
- Add rheoStream to claude.ai as a custom connector by its URL. It signs you in with the
  same login as the web shell, shows a consent page, and ends with an ordinary scoped
  token.
- Choose how tools are listed: every permitted tool with its schema (direct mode), or at
  most five tools that let a client browse and call the rest on demand
  (discover-then-call).
- Start a model run with `core.runtime.run`, naming the tools it may use and the records
  it may see. The run gets its own short-lived token and can never reach past the person
  who started it.
- Pick the runtime and model per workspace, from the lists the operator allows.

## How the MCP door works

### Bearer tokens only, checked on every call

The door accepts tokens of kind `mcp` (a person's) and `runtime` (one model run's). A
person's `cli` token is refused by kind, and cookies are ignored. The server is stateless:
no MCP session id is issued, and the token is resolved on every request. A malformed,
expired, revoked or out-of-scope token is refused with a distinct state before any tool
runs.

### A token's scope is frozen at issuance

Its operation set is copied into rows when it is issued and never widened; a new scope is
a new token. Three named sets ship: `read_only` (every read operation), `agent_default`
(every operation an MCP tool names) and `cli_full` (everything except the six operations
no token may hold). A token never exceeds its account's role, rechecked on every
presentation, and never exceeds the authority that issued it.

### Listing grants nothing

`tools/list` shows the tools whose operation is in the token's set and whose module is
enabled in the workspace. The same check runs again on every `tools/call`, so a tool
listed before a revocation is refused after it.

### Tool inputs cannot steer storage

Tool names follow `<module>_<verb>[_<noun>]`. An input model may not declare a field named
`workspace_id`, `actor`, `tenant_id`, `database`, `schema`, `dsn`, `sql`, `table_name`,
`statement` or `principal`, among others; registration refuses it. A record reference from
another workspace resolves to nothing. The MCP package imports nothing from the core's
storage layer or any database driver, and a test checks that.

### Outputs are redacted for the model

Every field of every record type sits in one tier, declared by the module that owns it:

| Tier | Examples | Reaches a model |
| --- | --- | --- |
| `public` | Titles, stage labels, notes the workspace wrote, qualification explanations | Yes, when the caller may read the record |
| `internal` | Party display names, organization names, value estimates, dates | Only for purposes the workspace enables |
| `restricted` | Email addresses, phone numbers, addresses, permission records, credentials, secret references, raw payloads | Never by default |

The door renders every result under the token's purpose. Contact values inside free text
reach a model as `[email withheld]` or `[phone withheld]` unless both the operator and the
workspace have explicitly turned the allowance on, and secret references are masked
whatever the setting. A write-class tool refuses input that contains one of those mask
tokens, so a model shown masked text cannot write the mask back over the real value. A
workspace can mark whole record types as never sent to a model.

### Discover-then-call keeps context small

Every tool a client can see costs its schema in every session. A token issued with
`--discover` is listed at most five tools however many modules are installed:
`operations_catalog` (what this token may call), `operations_describe` (one tool's schema
and class), `operations_call` (call it), and `operations_get` and `operations_list` for
polling. `operations_call` is not a second path: it runs the direct call again for the
named tool, through every check in the same order. The grants are the authority either
way; the mode changes only what is listed.

### Connector sign-in

A claude.ai connector cannot send a header, so the door also speaks OAuth for MCP clients.
Unauthenticated, the door answers `401` with a pointer to its protected-resource document.
The client registers (only allowlisted redirect URIs), sends the browser to authorize with
a PKCE `S256` challenge, and the person signs in and sees a consent page naming the
client, the account, the workspace, the operations the grant will hold and its lifetime.
Allow issues a 60-second single-use code; redeeming it writes an ordinary `mcp` token with
`agent_default` scope. The access value lives 60 minutes and rotates in place at every
refresh; the grant ends after 30 days. A refresh token presented again more than 10
seconds after it was rotated revokes the whole grant. The whole feature is off unless the
operator turns it on, and while off nothing is advertised that does not work.

### No approvals through MCP

A destructive tool returns `approval_required` with the approval id and the operation id,
distinct from success and from error. Approving happens elsewhere.

## How runtimes work

### A run is an operation

`core.runtime.run` builds a runtime request bound to a durable operation, the originating
actor, the workspace, an audience and a purpose. The task text travels as data and is
never interpolated into a command. Context items arrive already permission-checked and
redacted by the core's context builder. Limits come with it: a deadline (600 seconds by
default, never more than 3600), an iteration cap (40) and an output cap.

### Capabilities are checked before anything starts

Each adapter reports what it can do: tool calling, structured output, streaming,
continuation, cancellation, usage reporting, and whether it can enforce filesystem and
network isolation or only advise it. A workflow that needs a capability the adapter lacks
is refused before the adapter is invoked, naming the missing one.

### Every outcome has a fixed shape

An adapter yields progress, tool calls, tool results, usage and a final output, and ends
with exactly one terminal event. Failures come from a fixed list:
`executable_unavailable`, `credential_invalid`, `credential_not_owned`, `tool_denied`,
`deadline_exceeded`, `stream_truncated`, `output_invalid`, `iteration_limit`,
`runtime_error`. Usage is marked `exact` or `estimate`, never mixed.

### The run's token is the boundary

Each run reaches the door with its own `runtime` token: the operations its permitted tools
name, intersected with what the person who started it may do, minus everything no token
may hold. It expires at the run's deadline and is deleted when the run ends. The adapter's
own tool allow list is a convenience; the token is what the door enforces.

### Purpose is fixed by the token

A run's purpose is fixed on its token. The context builder takes no purpose argument at
all, so a run cannot reach memory outside its purpose by asking for a different one.

### Sessions resume only under the same binding

A native session handle is stored with its runtime, credential scope, actor, audience,
purpose and a digest of the redaction policy it was built under. Resuming needs every one
of them to match. There is no "latest session", and a session built under a wider policy
is never resumed under a narrower one.

### The Claude CLI adapter

It spawns the configured executable with a fixed argument list and writes the task to
stdin. It gives the run an empty working directory under the data root, removed
afterwards, and a per-workspace configuration directory, so everything the CLI writes
lands under the data root. Its environment is an allowlist with at most one credential
variable. It never opens a database, never sees a workspace context, and never sees a
secret reference. A personal login or subscription token is bound to one account: a run
started by anyone else fails `credential_not_owned` before a process starts. A
deployment-owned API key carries no such binding.

### The OpenRouter adapter

It sends the task and context, receives tool-call requests, dispatches each through the
same door with the same run token, and repeats up to the iteration limit. Nothing runs
locally, so it reports enforced isolation. Its model and provider allow lists and its
fallback policy sit under the operator's floor and can never widen who receives private
context.

### What the core keeps about a run

One row per run with a digest of the redacted context, never the text; which records it
was shown; usage; and transcripts that expire after 90 days by default, or sooner when a
record the run was shown is deleted. No secret, no tool argument and no raw context text
is stored.

## Why it's built this way

Models change faster than anything else in the stack. Keeping the model's loop behind an
adapter means no module, channel or workflow assumes one vendor, and switching runtimes is
a setting.

One MCP door means one place to enforce scope, redaction and refusal, however many modules
are installed. It also means fewer servers in every agent session: the design goal is to
put as many of an operator's MCP services as possible behind this one surface, and
discover-then-call keeps the visible tool list small as it grows.

## Where to look

- Specification: [runtime contract, MCP facade, redaction](../architecture/runtime-and-mcp.md),
  [tokens and connector sign-in](../architecture/identity-and-topology.md#tokens-for-cli-and-mcp-fr-4)
- Code: [MCP facade](../../apps/mcp/src/rheo_app_mcp),
  [MCP mount](../../apps/core/src/rheo_app_core/mcp_mount.py),
  [tool facade](../../packages/core/src/rheo_core/operations/tool_facade.py),
  [tokens](../../packages/core/src/rheo_core/tokens),
  [redaction](../../packages/core/src/rheo_core/redaction),
  [connector sign-in](../../packages/core/src/rheo_core/oauth),
  [runtime orchestration](../../packages/core/src/rheo_core/runtime),
  [runtime contract](../../packages/contracts/src/rheo_contracts/runtime.py),
  [Claude CLI adapter](../../runtimes/src/rheo_runtimes/claude_cli.py)
- Tests: [MCP boundary](../../tests/test_mcp_boundary.py),
  [MCP surface](../../tests/test_mcp_surface.py),
  [MCP redaction](../../tests/postgres/test_mcp_redaction.py),
  [discover-then-call](../../tests/postgres/test_mcp_discover_call.py),
  [OAuth boundary](../../tests/postgres/test_oauth_boundary.py),
  [runtime gate](../../tests/test_runtime_gate.py),
  [Claude CLI adapter](../../tests/test_claude_cli.py),
  [continuation binding](../../tests/postgres/test_runtime_continuation_binding.py)
