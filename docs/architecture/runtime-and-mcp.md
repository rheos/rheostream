# Runtime contract, the MCP facade, and the redaction contract

**Part of:** the [architecture specification](README.md). Designs against D4, FR 13, FR 19 to
FR 25, FR 27, and criteria 15 to 20. Closes idea-document question 12 (what reaches a model
provider) and the capability-check half of question 10.
**Decisions:** A9 (runtime contract), A10 (MCP facade), A13 (redaction tiers). See the
[decision list](README.md#architecture-decisions).

## Runtime contract, version 1

The runtime owns model conversation mechanics. rheoStream owns identity, authorization, tool
policy, domain data, audit, and durable work state (idea document). The contract is the seam
between the two, in `packages/contracts.runtime`, and every domain service that wants a model
depends on it and on nothing more specific (FR 19, guardrail 4).

### Request binding

A `RuntimeRequest` is built by the core's `runtime` package from a `WorkspaceContext` and an
operation; no module constructs one.

| Field | Meaning |
| --- | --- |
| `operation_id` | The durable operation this run belongs to. Every run is an operation. |
| `actor`, `workspace_id`, `audience` | Copied from the originating operation record (a run inside a job carries the operation's actor and audience, not the worker's `system` actor); the runtime may not change them. The actor must have an account behind it, because the run-scoped token is issued to that account: an operation started by the operator or by a schedule cannot start a run and is refused `runtime_actor_required` before any adapter is invoked. |
| `purpose` | From the purpose vocabulary; drives redaction and permission filtering. |
| `task` | The instruction text, as data. Never interpolated into a shell command. |
| `context_items` | List of `ContextItem(ref, tier, text)` already permission-checked and redacted by the context builder. |
| `permitted_tools` | List of MCP tool names, the adapter's allow list. Derived from the operation's declared tool needs intersected with the tools whose operations the originating actor may call; never wider than the actor. The [run-scoped token](#the-run-scoped-token) holds the *operations* these tools name, not the tool names. |
| `output` | `text`, `structured(json_schema)`, or `stream`. |
| `requirements` | Set of capability names the workflow needs (below). |
| `limits` | `deadline_seconds` (default and ceiling `runtime.max_deadline_seconds`, package default 600, floor `min`), `max_iterations` (default 40), `max_output_bytes`. |
| `continuation` | `None`, or the id of a `runtime_session` row the caller may resume. |
| `credential_slot` | A slot name such as `model`. The adapter maps it to a secret reference from its private configuration; the reference never enters the request ([secrets](storage-and-workspaces.md#a4-the-secret-store-fr-13-guardrail-14)). |
| `runtime_id`, `model_id` | Chosen from the workspace's permitted set, itself a subset of the operator's (`runtime.allowed_runtimes`, `runtime.allowed_models`, floor `subset`). Runtime and model are separate settings. |

### Capability discovery (FR 20)

```text
RuntimeAdapter.capabilities() -> RuntimeCapabilities
  tool_calling: bool
  structured_output: bool
  streaming: bool
  continuation: bool
  cancellation: bool
  usage_reporting: "exact" | "estimate" | "none"
  isolation: "enforced" | "advisory"      # can the adapter bound filesystem and network access
```

`RuntimeGate.check(request.requirements, adapter.capabilities())` runs before the adapter is
invoked and refuses with `UnmetCapability(name)` naming the first unmet requirement (criterion
15). A workflow that requires `isolation = enforced` is refused on an adapter that reports
`advisory`; that is the "CLI without enforceable isolation is rejected before the workflow
starts" case in the idea document's scenario table.

### Normalized outcomes

The adapter yields `RuntimeEvent`s and ends with exactly one terminal event:

| Event | Fields | Notes |
| --- | --- | --- |
| `progress` | `text`, `fraction` | Forwarded to the operation record. |
| `tool_call` | `tool`, `arguments_digest` | Arguments are not stored in the runtime request record; the tool's own audit row carries the request digest. |
| `tool_result` | `tool`, `outcome` | `ok`, `refused`, `approval_required(approval_id)`, `error`. |
| `usage` | `input_tokens`, `output_tokens`, `cost`, `kind` | `kind` is `exact` or `estimate`, kept separate. |
| `final_output` | `text` or `structured` | Structured output is validated client-side against the request's schema; a mismatch is `failure(output_invalid)`. |
| `failure` | `kind`, `detail` | Terminal. |
| `cancelled` | | Terminal. |
| `approval_required` | `approval_id` | Terminal for this run: the run stops, the operation goes `approval_required`, and a later run resumes with `continuation` after approval. |

Failure kinds, fixed: `executable_unavailable`, `credential_invalid`, `credential_not_owned`,
`tool_denied`, `deadline_exceeded`, `stream_truncated`, `output_invalid`, `iteration_limit`,
`runtime_error`.
The five criterion 16 induces are the first five. Each maps to an operation `failed` with the kind
as `error_code`, within the deadline, with no `succeeded` possible: the operation's
`terminal_check_kind` for a run is `handler_returned` only when a `final_output` was received, so
"a text reply is not proof a domain action succeeded" is enforced by the domain operation that
consumed the output checking its own record, not by the runtime.

### Native session handles

| `core.runtime_session` column | Meaning |
| --- | --- |
| `id uuid` | The handle the core hands out as `continuation`. |
| `runtime_id`, `credential_scope text`, `actor_kind`, `actor_id`, `audience_kind`, `audience_id` | The binding. A lookup must supply all of them; there is no "latest". |
| `native_handle text` | The adapter's own session id, opaque to the core. |
| `created_at`, `last_used_at`, `expires_at` | Sessions expire with `runtime.session_ttl_hours` (default 72). |

Switching runtimes starts a new native session with the selected authorized context; nothing
is carried across adapters. Completed effects are never replayed because effects are external
actions with their own records, not part of the transcript.

### The run-scoped token

Every run reaches the deployment's tools through the MCP surface with a token of kind `runtime`,
issued by the core's `runtime` package through `core.token.issue` (never by the adapter, which
opens no database) on behalf of the originating actor: `account_id` is the account behind that
actor, `issued_from = runtime`, `purpose` the run's, `expires_at` the run's deadline, and the
snapshot is the operations the run's `permitted_tools` name, intersected with the originating
actor's own permitted set and stripped of the non-token-issuable set
([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)).
The permitted set is therefore enforced by the facade on every call, not only by an adapter's
allow list, and tool outputs are rendered under the run's purpose. The token and its snapshot
rows are deleted when the run ends; `runtime_request.permitted_tools` keeps what the run could
reach.

### What the core records about a run

| Table | Columns | Notes |
| --- | --- | --- |
| `core.runtime_request` | `id`, `operation_id`, `runtime_id`, `model_id`, `purpose`, `context_digest bytea`, `permitted_tools text[]`, `requirements text[]`, `deadline_seconds`, `max_iterations`, `max_output_bytes`, `terminal_event text`, `failure_kind null`, `usage_input_tokens null`, `usage_output_tokens null`, `usage_cost null`, `usage_kind null`, `started_at`, `ended_at null` | One row per run. `context_digest` is a digest of the redacted text, never the text. `permitted_tools` and `requirements` stay arrays because nothing queries them. |
| `core.runtime_request_context` | `request_id`, `ref text` | Primary key both columns. Which references the run was sent; a child table because the [deletion cascade](deletion-export-migration.md#the-cascade) queries it to find the transcripts of every run whose context named an erased record. |
| `core.runtime_transcript` | `id`, `request_id`, `ordinal integer`, `kind text`, `body text`, `byte_length integer`, `recorded_at`, `retention_until timestamptz` | `kind` in `model_output`, `tool_result_summary`, `progress`. `retention_until` is `recorded_at` plus `runtime.transcript_retention_days` (default 90, floor `min`); `core.retention_sweep` removes rows past it. Rows for a run whose context named a deleted record are removed by the deletion cascade at once, because a transcript can restate what the model was shown. |

No secret value or reference, no tool argument, and no raw context text is in any of the three
tables (criterion 17).

### Automatic memory's two producers

Automatic memory has two producers: a permission-bound Rheo runtime evidence handoff and a
separately enrolled local Claude Code CLI/Desktop Code bridge. Existing
model-output/tool-summary/progress rows do not by themselves prove attributable explicit
statements; the automatic-memory carrier adds a typed speaker/evidence boundary before
extraction. Local capture requires both machine and project/workspace enrollment and excludes
arbitrary claude.ai/cloud chats. Stop checkpoints and SessionEnd finalization hand off locators
quickly; durable workers parse, sanitize, digest and extract. Raw tool results/file contents are
not blindly promoted. Acknowledged ranges recover while sources remain readable; pre-handoff
crashes, tails, failed hooks and expired/missing sources remain explicit gaps. No hook-finality
or spawned-process durability guarantee is claimed.

This amendment fixes the later carrier's bounds and what it may ask of the core. It adds no
public transcript path, no credential, and no local-settings example carrying user data; the
tables above are unchanged by it, and run 1a1 ships no transcript reader, extractor or hook
([memory](memory.md#provenance-and-links)).

## `ClaudeCliRuntime`

The first adapter, in `runtimes/claude_cli`. It spawns the local `claude` executable in print mode
and uses that program's own agent loop.

| Concern | Design |
| --- | --- |
| Executable | `runtime.claude_cli.executable`, a deployment setting (absolute path); never from a workspace setting or a request. Missing or non-executable at spawn is `executable_unavailable`. |
| Arguments | A fixed list: `-p`, `--output-format stream-json`, `--verbose`, `--max-turns <max_iterations>`, `--mcp-config <generated file>`, `--allowedTools <permitted tool names>`, `--resume <native_handle>` when continuing. No argument is built from task text. |
| Task input | Written to the child's stdin as data. |
| Tools | The adapter writes a per-run MCP configuration file pointing at this deployment's MCP endpoint with the [run-scoped token](#the-run-scoped-token) the core handed it, and passes `permitted_tools` as the executable's allow list. The facade enforces the token's snapshot on every call, so the allow list is a convenience and the token is the boundary. |
| Working directory | `<data_root>/workspaces/<id>/runs/<operation_id>/`, created empty, removed after the run. The parent development workspace is never the working directory. |
| Configuration directory | `<data_root>/workspaces/<id>/runtime/claude-cli/`, passed as the executable's configuration-directory variable (`CLAUDE_CONFIG_DIR`) and as `HOME`, so that everything the executable writes by convention (its session files among them) lands under the data root and nowhere else. It is per workspace, not per run, because `continuation` resumes a native session from a later operation; a per-run directory would lose it. The retention sweep removes regular files under `projects/` only (`config_dir / "projects"`), older than `runtime.transcript_retention_days`. The once-copied login seed at the configuration-directory root is not a session file and is not unlinked. |
| Credential and seeding | `runtime.claude_cli.credential_kind`, a deployment setting, is `login` or `api_key`; the package default is `login`, the personal self-host arrangement release one targets. With `login`, the operator completes the executable's interactive login once on the host into `runtime.claude_cli.login_seed_dir` (a directory under the data root, mode 0700), records whose login it is in `runtime.claude_cli.credential_account_id` ([below](#the-login-credential-is-bound-to-one-account)), and the adapter **seeds** each workspace's configuration directory by copying that directory into it the first time the workspace runs; a missing or empty seed directory is `credential_invalid` at spawn, before any process starts. With `api_key`, the `model` credential slot maps to a secret reference in the adapter's private configuration, resolved through the adapter's secret scope and passed in the child environment as the executable's own credential variable, and the configuration directory is created empty. Either way nothing about the credential enters the request, the operation record, or the transcript (criterion 17). A hosted edition never uses `login` ([later phases](later-phases.md#phase-8-the-hosted-edition)). |
| Environment | An allowlist: locale, path, the two directory variables above, and, under `api_key`, the executable's credential variable. Nothing else from the host environment reaches the child. |
| Capability vector | `tool_calling = true`, `structured_output = true` (the adapter validates the final output against the request's schema itself and fails `output_invalid`), `streaming = true`, `continuation = true`, `cancellation = true` (the process group is killed on the token), `usage_reporting = exact`, `isolation = advisory`. Criterion 15's gate runs against this vector: a request requiring `isolation = enforced` is the one release-one requirement it cannot meet. |
| Isolation | Reports `advisory` in release one: the adapter constrains the working directory and the tool allow list but cannot enforce a filesystem or network sandbox on its own. Workflows requiring `enforced` are refused on it until an operator-provided sandbox is configured. |
| Deadline | A timer kills the process group at `deadline_seconds`; `deadline_exceeded`. |
| Stream parsing | One JSON object per line. A process exit with no terminal `result` object is `stream_truncated`. An authentication error object is `credential_invalid`. A tool refusal from the facade surfaces as `tool_result(refused)` and, if the run cannot proceed, `tool_denied`. |
| Continuation | The `session_id` in the stream is stored as `native_handle`; resumed only through the bound lookup. The phase-one runtime test starts a run, ends it, and resumes it from a second operation with `continuation`, which is what proves the per-workspace configuration directory carries the native session across runs. |
| Usage | Reported as `exact` when the result object carries it. |

The adapter never opens a database, never sees a `WorkspaceContext`, and never sees a secret
reference outside its own configuration.

### The `login` credential is bound to one account

`credential_kind = login` seeds every workspace's configuration directory from one operator's
interactive login. That credential is a **person's** subscription, not a service credential, and
the deployment has no way to make it anything else. So the runtime binds it:

- `runtime.claude_cli.credential_account_id` (deployment setting, required when
  `credential_kind = login`) names the account whose login was seeded. A deployment that sets
  `login` without it fails startup naming the setting, in the same pass that validates the seed
  directory.
- Before any process is spawned, the runtime compares the run's originating account — the account
  behind `RuntimeRequest.actor`, which the [run-scoped token](#the-run-scoped-token) is already
  issued to — with that setting. A mismatch is the terminal failure `credential_not_owned`, and
  the operation `failed` with that code. No child process starts and nothing is written to the
  seeded directory.
- `credential_kind = api_key` carries no such binding, because a deployment-owned API credential
  is a service credential and serving several members is what it is for.

**Why this is a release-one rule and not a hosted-edition one.** R1 gives every workspace
membership and roles from the first day, and `rheo member add` is the release-one path to a second
member. Without the binding, that member's first run spawns the executable against the operator's
personal subscription — one person's plan silently serving another person's usage, which is
exactly the arrangement a personal subscription is not. The spec already said "a hosted edition
never uses `login`"; the check is what makes the sentence true one member earlier than the hosted
edition, where it is first *noticed* rather than first *true*.

The consequence is deliberate and narrow: on a `login` deployment, a second member can use every
surface except a model run, and the refusal names the reason. An operator who wants runs for
everyone moves the deployment to `api_key`, which is one setting and a secret reference.

## The OpenRouter adapter (phase six, shape only)

Recorded so the contract above is known to fit a structurally different adapter. It calls the
provider's API and supplies its own loop: send the task and context, receive tool-call requests,
dispatch each through the same facade with the same run-scoped token, return results, repeat to
`max_iterations`. It reports `tool_calling`, `structured_output`, `streaming`, `cancellation`,
and `usage_reporting = exact`; `continuation` is implemented by the core storing the message list
under the `runtime_session`, and `isolation = enforced` because nothing runs locally. Model and
provider allow lists, required-parameter enforcement, and a fallback policy that cannot widen who
receives private context are adapter settings under the operator floor. It changes no domain
module, which is the point of the second adapter (D4).

## The embedding provider

Embeddings are model-provider calls and sit behind the same kind of seam:
`EmbeddingProvider.embed(texts) -> vectors` with `model_id` and `dimensions` reported. The memory
module's dense strategy depends on the protocol. Release one ships one implementation, and it
runs in process: `LocalEmbeddingProvider`, fastembed's ONNX build of
`sentence-transformers/all-MiniLM-L6-v2` at 384 dimensions, selected by
`recallatron.embedding.provider = "local"` (deployment scope, default `none`). It reads no
credential and makes no HTTP call to embed anything, so no memory text leaves the deployment.
The one network access is the first fetch of the model artifact into
`<data_root>/models/fastembed/` ([data root](storage-and-workspaces.md#the-data-root-fr-10));
later loads read it from disk. It ships as Recallatron's `local-embeddings` extra, and the
module registers it only when that extra is installed. The model loads on first use in every
process that embeds: the worker, for memories, and whichever process serves a `dense` or
`hybrid` recall, for the query. One `embed()` call serves memories and queries alike, and a
returned vector that is not unit length fails the call.

What release one's provider receives is the memory's stored title and body, composed as
`embed_input(title, body)` (the title, one newline, the body), or a recall's bare query.
Recallatron's manifest declares an empty `sensitivity`, so no memory field carries the
`internal` or `restricted` tier and nothing is redacted from that input today. The redaction
contract applies to the embed input the day a memory field is tiered.

**Known limit, issue #108.** A real deployment cannot set `recallatron.embedding.provider` yet.
The module loader resolves deployment settings, to read `modules.installed`, before any
module's keys are registered, so the key fails startup with `SettingUndeclared` whether it comes
from `RHEO__recallatron__embedding__provider` or from `deployment.toml`. The container image
also installs without the `local-embeddings` extra. Both are deferred to run 0d, and until they
land a deployment runs with no provider: no embed job is enqueued, the `hybrid` default answers
lexical results with `dense_available = false`, and `recallatron.embedding.rebuild` refuses.

## The MCP facade

`apps/mcp`, served by the `core` process over streamable HTTP at the `mcp` surface (subdomain
mode `mcp.<base>/`; path mode `/mcp`). Bearer token only, of kind `mcp` or `runtime`; a `cli`
token is refused `token_wrong_kind`, and cookies are ignored on this surface
([presentation](identity-and-topology.md#tokens-for-cli-and-mcp-fr-4)).

**Session resolution.** The token resolves to an `access_token` row, and the boundary builds the
`WorkspaceContext` from it: workspace, actor `token`, role from the token's account membership,
`operation_set` from the token's snapshot rows, `audience = token`. A malformed, wrong-kind,
expired, revoked, or scope-invalid token is refused at the transport with a distinct state and
no tool runs (criterion 9).

**Listing.** `tools/list` returns the registered tools whose operation is in the token's operation
set and whose module is enabled in the workspace, filtered again on every `tools/call`, so a tool
listed before a revocation is refused after it (idea document: discovery grants nothing).

**Signature conventions.**

- Name: `<module>_<verb>[_<noun>]`; core tools use `workspace_`, `operations_`, `audit_`.
- Input: the operation's input model, with the reserved names forbidden at registration
  (criterion 6). Record references are strings in the documented form; a reference from another
  workspace resolves to nothing and the call fails `not_found`.
- Class: declared on the tool and required to equal the operation's ([module contract](module-contract.md#operations-tools-events)).
- Output: the operation's output model, rendered by the facade through the owning module's
  `render_for_model` under the [redaction tiers](#tiers) with the token's `purpose`
  (`internal_analysis` when the token carries none), so a tool never returns a `restricted`
  field, a contact value, or a secret reference to a model, whatever the operation returns to a
  person. A destructive, external, or financial call without an approval returns
  `approval_required` with the approval id and the operation id, distinct from success and from
  error (criterion 19).
- Long-running operations return `{ operation_id, state }` and the caller polls `operations_get`.

**No SQL, no repository.** A tool has no handler; `apps/mcp` imports the service registry and the
contracts and nothing from `rheo_core.storage` or any driver (criterion 20).

**Approvals are never given through MCP.** No token of any kind can carry `core.approval.*`:
the operations are non-token-issuable, refused at issuance and again at presentation
([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)),
so there is no MCP session in which the approval operation exists, and a model cannot approve
what it proposed. Approval happens in the web interface (R3 item 8). A tool may *request* an
approval on behalf of the actor, which is what `approval_required` is, and a destructive tool in
a token's set is that request right and nothing more.

**Release-one tool set.** Each names one operation and restates its class and roles; the
operation tables in the module documents are authoritative.

| Phase | Tool | Class | Roles |
| --- | --- | --- | --- |
| One (core) | `workspace_status`, `operations_get`, `operations_list` | read | owner, member |
| One (core) | `audit_list` | read | owner, operator |
| Two (memory) | recallatron_recall, recallatron_read | read | owner, member, service |
| Two (memory) | recallatron_remember, recallatron_derive | mutate | owner, member, service |
| Two (memory) | recallatron_correct, recallatron_supersede | mutate | owner, member |
| Two (memory) | recallatron_forget | destructive | owner, member |
| Three (relationships) | `relationships_find_party`, `relationships_get_party`, `relationships_list_review` | read | owner, member |
| Three (relationships) | `relationships_merge_parties`, `relationships_unmerge`, `relationships_resolve_review` | mutate | owner, member |
| Three (relationships) | `relationships_delete_party` | destructive | owner |
| Three (leads) | `leads_search`, `leads_list`, `leads_get`, `leads_get_handoff` | read | owner, member |
| Three (leads) | `leads_connection_health` | read | owner |
| Three (leads) | `leads_capture`, `leads_update`, `leads_qualify`, `leads_transition` | mutate | owner, member |
| Three (leads) | `leads_handoff` | mutate; writes the handoff record and returns `unavailable` with no destination (criterion 64) | owner, member |
| Three (leads) | `leads_prepare_followup` | draft | owner, member |
| Three (leads) | `leads_delete` | destructive | owner |

Entity list/get are service-only in release one. Tool origin and delegated operation availability
are checked on every discovery/call; a cached listing grants no authority. READ query text is not
persisted in release-one tool telemetry. Metadata telemetry is workspace-scoped,
owner/operator-readable and bounded to seven days and ten thousand rows or a tighter workspace
setting; failure cannot change a tool result.

No tool in the production set is external or financial class. The recording sink and the
destructive fixture are test-harness registrations, never in the production set (criterion 18);
the sink is release one's only external-class operation anywhere.

Release one ships **no no-query context bundle**: every memory a run is shown arrives through
`recallatron.memory.recall` or `recallatron.memory.read` with a query the caller supplied, and a
tool that handed a model a bundle of memories it never asked for is out of scope for this
release.

## The redaction contract

What leaves the deployment for a model provider, and what does not. FR 13 and FR 27 fix the
boundary; this is the operational rule.

### Tiers

Every field of every record type is in one tier, declared in the owning module's manifest
(`sensitivity`):

| Tier | Contents | Sent to a model |
| --- | --- | --- |
| `public` | Titles, stage labels, notes the workspace wrote for itself, message bodies the sender addressed to the workspace, qualification explanations | Yes, when the caller may read the record. |
| `internal` | Party display names, organization names, opportunity value estimates, dates | Yes for purposes the workspace enables (`redaction.internal_purposes`, default `respond, follow_up, internal_analysis`), no otherwise. |
| `restricted` | Contact point values (email, phone, address), permission records, member credentials, every secret reference, other members' personal material, raw delivery payloads | Never by default. `redaction.contact_points_to_model` (`and` floor, package default false) may allow contact point values for `respond`; nothing enables the rest. |

Secrets are not a tier; they cannot enter a context item because no domain service can resolve
one. The tier for secrets exists so that a *reference* string in a record is also withheld.

### The context builder

`core.runtime.build_context(ctx, purpose, refs)`: resolve each reference under `ctx`, drop those
not readable, obtain memory items only through `recallatron.memory.recall` with the same
`purpose` (which applies the audience, purpose, link, and contact-permission filters,
[memory](memory.md#retrieval-fr-27-fr-30-criteria-27-and-31); FR 27), render each readable
record through its module's `render_for_model(record, tier_policy)` which omits fields above the
allowed tier, and cap total bytes at `runtime.max_context_bytes` (package default 200000, floor
`min`). Restricted values that appear inside free text (an email address inside a message body)
are masked by pattern before sending unless the `respond` allowance is on. Derived memories carry
the intersection of their sources' audiences and purposes, so the filter on a memory is never
looser than on what it was made from (criterion 28).

**Purpose comes from the context's principal, not from the argument.** When
[`ctx.principal`](overview.md#the-workspace-context) carries a bound purpose — a runtime token's,
or a job's or approval's stored purpose on a rebuild — that purpose is the one applied: an
omitted argument uses it, and an argument naming a different purpose refuses rather than
narrowing or widening silently. A run therefore cannot reach memory outside the purpose its
token was issued for by passing a different value here. An unbound authenticated person or
service browsing without a bound purpose has no memory-purpose gate at all, which is the
ordinary interface case and not a bypass: the audience, link and contact-permission filters
still run in full.

### Retention and control

- The core stores context references and a digest, and transcripts for the retention period or
  until a record the run was shown is deleted, whichever comes first
  ([the cascade](deletion-export-migration.md#the-cascade)).
  The provider's copy is outside the deployment's control, and the documentation says so rather
  than claiming otherwise. The CLI's local files are directed under the data root by the
  per-workspace configuration directory and swept with the transcripts; the contract claims only
  that the deployment removes what it can see, and the phase-one runtime test (a run resumed
  from a second operation) is where a builder verifies the executable writes nowhere else
  before the claim is widened.
- The workspace owner controls the internal-purpose list and the contact-point allowance within
  the operator's floor, the runtime and model allow lists within the operator's, and transcript
  retention within the operator's maximum.
- A record type can be marked `never_to_model` at the workspace level per module
  (`<module>.redaction.exclude_types`), which the builder honours before tiering.
