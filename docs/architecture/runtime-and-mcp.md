# Runtime contract, the MCP facade, and the redaction contract

**Part of:** the [architecture specification](README.md). Designs against D4, FR 13, FR 19 to
FR 25, FR 27, and criteria 15 to 20. Closes idea-document question 12 (what reaches a model
provider) and the capability-check half of question 10.
**Decisions:** A9 (runtime contract), A10 (MCP facade), A13 (redaction tiers). See the
[decision list](README.md#architecture-decisions).

## Runtime contract, version 1

The runtime owns model conversation mechanics. rheoStream owns identity, authorization, tool
policy, domain data, audit, and durable work state (idea document). The contract is the seam
between the two, in `rheo_contracts.runtime` (`packages/contracts`), and every domain service that wants a model
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
| `context_items` | List of `ContextItem(ref, tier, text)` already permission-checked and redacted by the [context builder](#the-context-builder). `tier` is the highest tier of what went into the text: `public` for an untiered record's `display`, and for a tiered record the highest tier among the fields the policy let through. |
| `permitted_tools` | List of MCP tool names, the adapter's allow list. The tool names the caller requested in `core.runtime.run`'s `permitted_tools` input, intersected with the tools whose operations the originating actor may call and stripped of non-token-issuable operations; never wider than the actor. The [run-scoped token](#the-run-scoped-token) holds the *operations* these tools name, not the tool names. |
| `output` | `text`, `structured(json_schema)`, or `stream`. |
| `requirements` | Set of capability names the workflow needs (below). |
| `limits` | `deadline_seconds` (default and ceiling `runtime.max_deadline_seconds`, package default 600, floor `min`, and a hard ceiling of 3600 in code), `max_iterations` (default 40), `max_output_bytes` (default 1000000; recorded, not yet enforced by the adapter). |
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
| `purpose text null`, `policy_digest bytea null` | The redaction policy the session was built under (core revision `0009_runtime_session_policy`): the run's purpose and a SHA-256 over the tier-policy inputs (purpose, effective `redaction.internal_purposes`, whether contact values are released, every module's effective `exclude_types`). Written on every run that records a session. Null on rows written before 0009, which are therefore never resumed. |

Switching runtimes starts a new native session with the selected authorized context; nothing
is carried across adapters. Completed effects are never replayed because effects are external
actions with their own records, not part of the transcript.

**The binding includes the redaction policy.** A continuation resumes only when the
stored session's `purpose` and `policy_digest` equal the new run's, besides the binding
columns and the expiry. A native session holds whatever its earlier runs were shown, and
nothing re-redacts it, so resuming one built under a wider purpose (`internal_analysis`
for a `share_with_referral` run) or a looser policy (before the workspace narrowed
`redaction.internal_purposes`, excluded a record type or withdrew the contact allowance)
would keep what the current policy withholds. Any mismatch, and any row written before
revision 0009, starts a fresh session instead; the run itself is not refused.

### The run-scoped token

Every run reaches the deployment's tools through the MCP surface with a token of kind `runtime`,
issued by the core's `runtime` package through `rheo_core.tokens.issue.issue_runtime_token`
(not the `core.token.issue` operation, and never by the adapter, which opens no database) on
behalf of the originating actor: `account_id` is the account behind that
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
| `core.runtime_transcript` | `id`, `request_id`, `ordinal integer`, `kind text`, `body text`, `byte_length integer`, `recorded_at`, `retention_until timestamptz` | `kind` in `model_output`, `tool_result_summary`, `progress`. `retention_until` is `recorded_at` plus `runtime.transcript_retention_days` (default 90, floor `min`); `core.retention_sweep` removes rows past it. Rows for a run whose context named a deleted record are to be removed by the deletion cascade at once, because a transcript can restate what the model was shown; that cascade step is not built yet, so today only the sweep removes transcripts. |

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
public transcript path, no credential, and no local-settings example carrying user data
([memory](memory.md#provenance-and-links)).

The Rheo runtime producer holds its evidence in one core table beside the runtime tables,
`core.evidence_unit` (revision `0010_evidence_unit`), not in `core.runtime_transcript`. A
row is one turn waiting to become memory: `pending` with its sanitized body, or `settled` or
`gap` with the body cleared and a content-free `outcome` word. It is transient working state,
never exported. Its `source_expires_at` is its recording time plus
`automatic_memory.max_pending_hours`; the first daily `core.retention_sweep` after that
instant ages a still-pending row to `gap/expired_pending`, so its text can stay until the
next daily sweep, unless a drain job ages it sooner: Recallatron's automatic-memory drain ages
every expired pending row before it claims. The sweep skips rows another transaction holds
locked (`SKIP LOCKED`); if that transaction rolls back, the row remains pending until a later
sweep can lock it, so there is no hard one-sweep-interval upper bound. The same sweep deletes
settled and gap rows older than `runtime.transcript_retention_days`. A run's turn is recorded only when
six conditions all hold, checked in order: `automatic_memory.enabled` is true at both the
operator and the workspace level; an extraction provider resolves (the default `"none"`
resolves nothing, so production records nothing until one is configured); an enabled module
subscribes to `core.evidence.recorded`; the speaker is a person (an account, or a
person-held `cli` token, never an `mcp` or `runtime` token); the run is bound to a purpose;
and the task text is non-empty after sanitation. A failure while recording is logged without
content and the run carries on without its evidence, and the recorder only accepts a turn
attributed to the run's own account and bound purpose. The runtime producer itself has no
transcript reader or hook; the local bridge below ships both.

### The local Claude Code bridge

The second producer is `apps/bridge`, installed as the `rheo-bridge` command and run on the
person's own machine from a checkout's virtualenv. A standard-library-only hook in one
enrolled directory notes where each session's transcript is. A worker reads the human-typed
lines from those transcripts, runs the same `sanitize()` locally, and posts them to one
core operation. The rows it creates carry `producer_kind = "claude_code_local"`; revision
`0011_local_evidence` admits that value in `core.evidence_unit`'s CHECK and adds
`core.evidence_enrollment`. The frozen 0010 tuples are not edited.

**Enrollment.** One `core.evidence_enrollment` row binds one account, one machine and one
directory. The machine is `machine_fingerprint`, the sha256 of `"rheo-machine:"` plus the
hex of a 32-byte `machine.key` that never leaves the laptop. The directory is
`project_fingerprint`, the sha256 of `"rheo-project:"` plus the directory's realpath; the
path itself never leaves the laptop either. A partial unique index allows one active
enrollment per fingerprint pair. The row's id is the `authority_id` of every evidence row
ingested under it. Its purpose is always `internal_analysis` in this version, and every row
it admits is audience `member`, bound to the enrolled account. The operator runs
`rheo evidence enroll`, `rotate` and `revoke` inside the deployment's `core` container
(operations `core.evidence_enrollment.create`, `.rotate` and `.revoke`); no token may carry
`create` or `rotate`.

**The bridge token.** A `cli` token whose snapshot is exactly `core.evidence.ingest` and
whose purpose is the enrollment's; every other operation refuses it. `rotate` mints a
replacement under the same enrollment id, so rows still pending stay verifiable, and revokes
the old token. `enroll --json` and `rotate --json` print one JSON line, `enrollment_id`,
`token` and `expires_at`, and nothing else on stdout, so the line pipes straight into
`rheo-bridge set-token` over ssh. `rheo doctor` reports each active enrollment's token:
`FAIL` when it is revoked, expired or within 7 days of expiry, `warn` within 14. Rotation is
an operator step; nothing rotates on its own.

**Ingest.** `core.evidence.ingest` is an ordinary operation on the existing bearer route,
`POST /api/v1/operations/core.evidence.ingest`. It finds the one active enrollment bound to
the presenting token, compares the two fingerprints the request carries, asks the recording
gate unchanged, clamps any future clock to the server's `now`, and hands records to
`record_evidence` and gaps to `record_gaps`. Speaker and audience come from the enrollment
row, never from the request. The server re-sanitizes every record with its own `sanitize()`,
whatever version the bridge ran. The answer is four tuples of native keys, `accepted`,
`deferred`, `gapped` and `dropped`, with no text and no counts. While recording is off every
key comes back `deferred`, and the worker holds (15 minutes, doubling to a 6-hour cap) and
re-probes with a single item instead of resending its backlog. From there the runtime
producer's pipeline runs as it does for runtime rows, except that `claim_units` picks each
row's authority by that row's own `producer_kind`. `LocalEvidenceAuthority` refuses with one
of six words: `producer_mismatch`, `source_unavailable`, `speaker_mismatch`,
`enrollment_inactive`, `enrollment_mismatch` and `membership_revoked`. Revoking an enrollment
makes each of its still-pending rows settle `authority_unverified` at its next claim.

**Keys.** Nothing that leaves the laptop names a session, a message, a path or a project. A
record's native key is `cc1:` plus 64 hex, the HMAC-SHA256 under `machine.key` of the
transcript line's session id and message id; a gap's is `cc1g:` plus 64 hex. The keys do not
change with the sanitizer or the model, so a resent range is answered from what the server
already holds and writes nothing new. The cost: a lost `machine.key` means new keys, and a
replay after re-enrolling is not recognised as held.

**The laptop side.** The hook runs on `Stop` and `SessionEnd`, appends one line to
`~/.rheo-bridge/spool/` (the event, a salted hash of the session id, the transcript path and
the time, and no other payload string), spawns the worker detached if none holds
`worker.lock`, prints nothing and exits 0. The worker reads only the transcripts the spool
names, and only under `~/.claude/projects/<slug>` for the enrolled directory's slug; a path
anywhere else is refused and counted. It reads from a byte cursor that moves only in the same
SQLite commit as the server's acknowledgement, so a crash re-reads a range the server already
holds. Local limits in `~/.rheo-bridge/config.json` bound one pass: 1 MiB, 1000 records, and
24 hours before an unsent line becomes a gap. A session's local row is kept until its whole
range is acknowledged. `entrypoint` (`cli` for Claude Code CLI, `claude-desktop` for Desktop
Code) is counted locally and never sent; both clients run the same project hook.

**Gaps.** The bridge reports what it knows it cannot deliver as content-free gap rows:
`source_truncated` when a transcript shrank, was replaced or vanished under the cursor, and
`expired_pending` for a line older than the local pending limit by the time the worker could
send it. What leaves no trace is not reported: if every hook call for a session failed (a
full disk, a disabled hook), there is no locator to gap-report. No hook-finality guarantee
is claimed.

**Known limit: the slug is lossy.** The slug maps every character outside `[A-Za-z0-9]` to
`-`, so `/work/a-b`, `/work/a_b` and `/work/a/b` share one slug, and an exact-slug match is
not an exact-directory match. The mitigation is that the worker reads only paths the enrolled
directory's own hook wrote to the spool and never crawls `~/.claude/projects`, so a session
from a colliding directory is read only if that directory also runs the enrolled hook. A
bridge home holds one enrollment, so each bridge home captures one enrolled directory.

**`rheo-bridge` commands.**

| Command | What it does |
| --- | --- |
| `init --enrolled-dir <dir> --api-url <url>` | Creates `~/.rheo-bridge/` (0700), the machine key, the install salt and `config.json`, then prints both fingerprints and the `rheo evidence enroll ... --json` command for the operator. No network call. Refuses if a machine key already exists. |
| `set-token` | Reads the one JSON line from stdin, refuses a terminal, never echoes the token, stores it at 0600, and clears any back-off hold so the next drain posts at once. Also takes a `rotate --json` line. |
| `install-hook --enrolled-dir <dir> [--dry-run]` | Adds the `Stop` and `SessionEnd` hooks to `<dir>/.claude/settings.local.json` with an absolute interpreter path. Refuses a directory other than the enrolled one, and refuses inside a Git working tree unless Git ignores that file. `--dry-run` prints the diff and writes nothing. |
| `remove-hook --enrolled-dir <dir>` | Removes only this bridge's entries, and deletes the file or an emptied container only if install created it. |
| `uninstall --enrolled-dir <dir> [--purge]` | Removes the hooks, the token and the machine key, then prints the `rheo evidence revoke` command. `--purge` first prints `discarded_unacknowledged: N`, the sessions the server has not acknowledged, then deletes `~/.rheo-bridge/`. |
| `status` | Session counts, hold state, local ledger counters, `enrollment_id`, `token_expires_at` and days remaining. Never a hash, key, path or text. |
| `drain` | One worker pass in the foreground. Exit 0 ok, 1 the worker could not run, 2 the server refused the batch (rotate the token or re-enroll). |

**Enrollment `create` and `rotate` are not idempotent.** An operator retrying either needs
to know two things:

- A `create` whose response was lost, for example because the
  `ssh ... | rheo-bridge set-token` pipe broke, has still enrolled the pair, under a token
  nobody holds. Retrying `create` answers `enrollment_exists`. Find that enrollment's id with
  a read-only query in the `core` container, selecting the id and no other column:
  `SELECT id FROM core.evidence_enrollment WHERE machine_fingerprint = '<machine>' AND
  project_fingerprint = '<project>' AND state = 'active'`, using the two fingerprints
  `rheo-bridge init` printed. Then run `rheo evidence rotate <enrollment-id> --account
  <account> --workspace <workspace> --json` through the same pipe into `rheo-bridge
  set-token`. That keeps the enrollment id and revokes the token nobody holds.
  `rheo evidence revoke <enrollment-id>` followed by a fresh `enroll` also works, but mints
  a new enrollment id.
- A retried `rotate` mints a second token and revokes the first, so the token the first call
  returned is already dead. Never retry a rotate outside the pipe. If a rotate's response was
  lost, run one more `rotate` through the pipe, so `set-token` receives the token that is now
  current.

**The two gates.** Nothing here records anything until two separate steps are taken, each
authorized on its own. Gate A installs the hook in the real enrolled directory; until then
the worker has no locator to read, and the installer's tests run only against a settings
file under a temporary home. Gate B sets `automatic_memory.extraction.provider =
"claude_cli"` (default `"none"`), which registers at both composition roots but resolves
only when configured; `automatic_memory.extraction.model_id` picks its model, and empty
leaves the CLI's default. The provider runs one tool-less CLI turn per batch: no built-in
tools (`--tools ""`), an empty-snapshot run token, nothing pre-approved, and an empty
throwaway directory. Between the two gates the server defers every key and the worker holds,
so turns stay local, and those that age past the pending limit arrive later as
`expired_pending` gaps, not memories.

## `ClaudeCliRuntime`

The first adapter, `rheo_runtimes.claude_cli` in `runtimes/`. It spawns the local `claude` executable in print mode
and uses that program's own agent loop.

| Concern | Design |
| --- | --- |
| Executable | `runtime.claude_cli.executable`, a deployment setting (absolute path); never from a workspace setting or a request. Missing or non-executable at spawn is `executable_unavailable`. |
| Arguments | A fixed list: `-p`, `--output-format stream-json`, `--verbose`, `--max-turns <max_iterations>`, `--mcp-config <generated file>`, `--allowedTools <permitted tool names>`, `--resume <native_handle>` when continuing. No argument is built from task text. |
| Task input | Written to the child's stdin as data. |
| Tools | The adapter writes a per-run MCP configuration file pointing at this deployment's MCP endpoint with the [run-scoped token](#the-run-scoped-token) the core handed it, and passes `permitted_tools` as the executable's allow list. The facade enforces the token's snapshot on every call, so the allow list is a convenience and the token is the boundary. |
| Working directory | `<data_root>/workspaces/<id>/runs/<operation_id>/`, created empty, removed after the run. The parent development workspace is never the working directory. |
| Configuration directory | `<data_root>/workspaces/<id>/runtime/claude-cli/`, passed as the executable's configuration-directory variable (`CLAUDE_CONFIG_DIR`) and as `HOME`, so that everything the executable writes by convention (its session files among them) lands under the data root and nowhere else. It is per workspace, not per run, because `continuation` resumes a native session from a later operation; a per-run directory would lose it. The retention sweep removes regular files under `projects/` only (`config_dir / "projects"`), older than `runtime.transcript_retention_days`. The once-copied login seed at the configuration-directory root is not a session file and is not unlinked. |
| Credential and seeding | `runtime.claude_cli.credential_kind`, a deployment setting, is `login` or `api_key`; the package default is `login`, the personal self-host arrangement release one targets. With `login`, the operator completes the executable's interactive login once on the host into `runtime.claude_cli.login_seed_dir` (a directory under the data root, mode 0700; unset, it is `<data_root>/config/claude-cli-login`), records whose login it is in `runtime.claude_cli.credential_account_id` ([below](#the-login-credential-is-bound-to-one-account)), and the adapter **seeds** each workspace's configuration directory by copying that directory into it the first time the workspace runs; a missing or empty seed directory is `credential_invalid` at spawn, before any process starts. With `api_key`, the `model` credential slot maps to the secret reference in `runtime.claude_cli.credential_ref` (a deployment setting; the adapter's secret scope admits only `secret://file/runtime/claude_cli/...` and `secret://env/RHEO_ANTHROPIC_API_KEY`), resolved through that scope and passed in the child environment as `ANTHROPIC_API_KEY`, and the configuration directory is created empty. Either way nothing about the credential enters the request, the operation record, or the transcript (criterion 17). A hosted edition never uses `login` ([later phases](later-phases.md#phase-8-the-hosted-edition)). |
| Environment | An allowlist: locale, path, the two directory variables above, and, under `api_key`, the executable's credential variable. Nothing else from the host environment reaches the child. |
| Capability vector | `tool_calling = true`, `structured_output = true` (the adapter validates the final output against the request's schema itself and fails `output_invalid`), `streaming = true`, `continuation = true`, `cancellation = true` (the process group is killed at the deadline and whenever the job leaves the poll loop with the process still running, including a cancelled job), `usage_reporting = exact`, `isolation = advisory`. Criterion 15's gate runs against this vector: a request requiring `isolation = enforced` is the one release-one requirement it cannot meet. |
| Isolation | Reports `advisory` in release one: the adapter constrains the working directory and the tool allow list but cannot enforce a filesystem or network sandbox on its own. Workflows requiring `enforced` are refused on it until an operator-provided sandbox is configured. |
| Deadline | The core's poll loop cancels the handle once `deadline_seconds` has passed, which kills the process group; `deadline_exceeded`. |
| Stream parsing | One JSON object per line. Only the terminal `result` object is read; it yields a `usage` event when it carries a well-formed `usage` object, and no `progress`, `tool_call` or `tool_result` event is emitted yet. A process exit with no terminal `result` object is `stream_truncated`. An error `result` whose `result` is `authentication_failed` is `credential_invalid`, one whose `result` is `tool_denied` is `tool_denied`, and any other error `result` is `runtime_error`. |
| Continuation | The `session_id` in the stream is stored as `native_handle`; resumed only through the bound lookup. The phase-one runtime test starts a run, ends it, and resumes it from a second operation with `continuation`, which is what proves the per-workspace configuration directory carries the native session across runs. |
| Usage | `exact`, read from the result object before the terminal event: `input_tokens` sums fresh, cache-write and cache-read input, `output_tokens` is the CLI's, and `cost` is `total_cost_usd`. A result without a well-formed `usage` object reports nothing and the `usage_*` columns stay null. |

The adapter never opens a database, never sees a `WorkspaceContext`, and never sees a secret
reference outside its own configuration.

### The `login` credential is bound to one account

`credential_kind = login` seeds every workspace's configuration directory from one operator's
interactive login. That credential is a **person's** subscription, not a service credential, and
the deployment has no way to make it anything else. So the runtime binds it:

- `runtime.claude_cli.credential_account_id` (deployment setting, required when
  `credential_kind = login`) names the account whose login was seeded. The worker refuses to
  start, naming the setting, when `runtime.claude_cli.executable` is set under `login` without
  it; the seed directory is checked at spawn, where a missing one is `credential_invalid`.
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
`internal` or `restricted` tier. The embed input does not pass through the redaction renderer:
the release-one provider runs in process and sends nothing anywhere, and a remote provider
would have to route its input through the renderer before it could ship.

**Setting the provider in a deployment (issue #108, fixed in run 0d).** Core startup, the
worker and every CLI command call `register_module_settings()` before their first strict
resolve. It reads `modules.installed` on its own and registers the allowlisted modules'
settings, so `RHEO__recallatron__embedding__provider` or the same key in `deployment.toml`
resolves instead of failing with `SettingUndeclared`. The default image still installs without
the `local-embeddings` extra; the build argument `UV_SYNC_ARGS="--extra local-embeddings"` adds
it, and the flagship overlay sets both that argument and provider `local`. A deployment that
sets no provider runs with none: no embed job is enqueued, the `hybrid` default answers lexical
results with `dense_available = false`, and `recallatron.embedding.rebuild` refuses.

## The MCP facade

`apps/mcp`, served by the `core` process over streamable HTTP at the `mcp` surface (subdomain
mode `mcp.<base>/`; path mode `/mcp`). Bearer token only, of kind `mcp` or `runtime`; a `cli`
token is refused `token_wrong_kind`, and cookies are ignored on this surface
([presentation](identity-and-topology.md#tokens-for-cli-and-mcp-fr-4)).

**Mounting.** `rheo_app_mcp.transport.build_mcp_app` builds the gated application, and
`apps/core`'s `mcp_mount.py` serves it from the public listener. The core process's lifespan
builds it after startup, from the resolved `routing.*` settings and the loaded modules' surfaces,
binds it to the process's consumer registry, and runs the SDK's session manager until shutdown.
The server is **stateless**: each request gets a fresh transport, no `Mcp-Session-Id` is issued
or honoured, and a `POST` is answered with one JSON body. The bearer is resolved on every request
anyway, so no session state is needed, and SDK 2.2.0 would bind a session only to an
authenticated user object this gate never sets, which would let one token drive another's
session. Only `POST` reaches the facade; `GET`, `DELETE` and every other method answer 405. A
pure ASGI router in front of the FastAPI routes decides which requests are the surface:

- Path mode: exactly the surface path and that path with a trailing slash (`/mcp` and `/mcp/`),
  both answered by the one SDK route with no redirect. Longer paths under it go to FastAPI.
- Subdomain mode: every request whose `Host` is the `mcp` host, compared lowercased with the
  port and any trailing dot stripped. `/` is the endpoint; every other path on that host is a
  404, so the `mcp` host serves no `/auth/*`, `/api/*` or `/healthz`. A request on any other host
  never reaches MCP. Startup refuses an empty `routing.mcp.host`, or one whose host is also the
  shell, identity, `api` or a module host.

The runtime's `url_for(config, "mcp", "/")` (`https://<public_host>/mcp/` or
`https://mcp.<public_host>/`) is therefore served in both modes. The bearer gate still answers
first: no bearer, or a refused one, is a 401 with the refusal state and no tool runs.

**DNS-rebinding protection** is the SDK's, kept on and configured from the routing settings
rather than from its loopback default. The allowed `Host` values are exactly the surface's hosts:
`base_host` in path mode, `mcp.<base_host>` in subdomain mode, plus the same name under
`public_host` when that differs (a published port, `example.test:8443`). No wildcard port is
admitted for a real name, so a deployment reached on a non-default port says so in
`routing.public_host`, which is also what makes `url_for` print a usable URL. The allowed origins
are those authorities under `routing.scheme`; a client that sends no `Origin`, which is every
non-browser MCP client, is admitted. When `base_host` is `localhost` or `127.0.0.1` the list also
admits `localhost:*`, `127.0.0.1:*` and `[::1]:*` (and `mcp.<base_host>:*` in subdomain mode),
with origins under `http` and `https`, as the SDK's own loopback default does: a rebinding attack
presents the attacker's domain in `Host`, never a loopback name. A disallowed `Host` is 421 and a disallowed `Origin` 403, in both cases before a tool
runs.

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
- Output: the operation's output model, rendered by the facade under the [redaction
  tiers](#tiers) with the token's `purpose` (`internal_analysis` when the token carries none),
  so a tool never returns a `restricted` field, a contact value, or a secret reference to a
  model, whatever the operation returns to a person.
  `rheo_core.operations.tool_facade.call_registered_tool` fills the outcome's `model_view` and
  the transport sends that as `result`, never the raw model; an outcome with a result and no
  `model_view` sends no `result` at all. An output model tiers its fields with the `Tiered`
  marker in `Annotated` metadata (`email: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]`),
  because a tool's output is a model rather than a record and the manifest's `sensitivity` map
  is keyed by record type. The marker is read in any spelling: `X | None`, `Optional[X]`, a
  union arm or a container element (`list[Annotated[str, Tiered(...)]]`) tiers the field, the
  strictest marker winning. A field above the policy's tier is dropped, nested models,
  dataclasses, lists and mappings included; a class that marks any field withholds every field
  it leaves unmarked (extra and computed fields among them); every string left, set members and
  mapping keys included, and the error text, is masked as below; a `RootModel` renders its
  root; a set's members are rendered one by one. Wherever the walk cannot pair a value with
  its type (a serializer that reshaped it, a model that dumps to a string, a mapping key that
  does not dump as expected) it withholds the value if anything tiered could be inside it
  (a marker in its annotation, or a tiered model, dataclass, `TypedDict` or `NamedTuple`), and
  otherwise falls back to the dump with every string masked; never the raw dump. Operation
  registration refuses those shapes up front: an output holding a tiered model with a
  `@model_serializer`, a tiered field with a custom serializer, or a tiered `NamedTuple` is
  `RegistrationRefused`. A record of a type the workspace excludes
  (`<module>.redaction.exclude_types`) is dropped from a result, and a result that is itself
  one (its own `ref` names the type, checked before tiering so an unmarked `ref` still counts)
  comes back with `result: null` and a `withheld` reason, for a person's MCP token and a run's
  token alike. A write into an excluded type still succeeds and commits; the model is told
  only that the result is withheld, which avoids a retry writing it twice. A result's
  aggregate fields are not recomputed when excluded items are dropped: a `recallatron_read`
  window's `total` and positions, and a recall's provenance counts, still count them, a
  one-bit residual (the model can tell excluded memories exist) recorded here rather than
  closed. A class with no marker is untiered and
  answers its JSON dump with contact values and secret references masked, which is every output
  model in release one: Recallatron's and the core's tools return the same fields as before,
  and a contact value inside a memory's text reaches a model as `[email withheld]` or `[phone
  withheld]` unless the allowance below is on. A write-class tool (every class above `read`)
  refuses input carrying one of those mask tokens `input_invalid`, so a model shown masked text
  cannot write the mask back over the real value through `recallatron_correct`,
  `recallatron_supersede` or `recallatron_remember`. A destructive, external, or financial call
  without an approval returns `approval_required` with the approval id and the operation id,
  distinct from success and from error (criterion 19). Both ids are fields of the tool result's
  structured payload (`approval_id`, `operation_id`), beside `state` and `error`; `error_text`
  names the approval in prose as well.
- Long-running operations return `{ operation_id, state }` and the caller polls
  `core.operation.get` through `operations_get`.

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
| One (core) | `workspace_status` | read | owner, member, operator |
| One (core) | `operations_get`, `operations_list` | read | owner, member, operator |
| One (core) | `audit_list` (`limit` only; see below) | read | owner, operator |
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

`audit_list` declares `limit` and nothing else. `core.audit.list`'s two opt-ins,
`include_tool_telemetry` and `include_deletions`, open owner/operator-only collections (tool
telemetry, and the deletion ledger with its exact closure counters), so a tool call naming either
is `input_invalid` and a model reads the audit records alone: metadata (actor, operation, subject
reference, request digest, outcome) with no payload. The tool's narrowing binds the tool call
only, so the operation enforces the same rule on every surface: it refuses either opt-in
`operation_not_permitted` for an `mcp` or `runtime` token (a token whose row is gone counts as
one). Since issue #130 the `api` surface refuses `mcp` tokens outright (`token_wrong_kind`), so
over HTTP that refusal is reached first. A person with the role still asks for both through a
session or a `cli` token. The listing filters on role, so a member's token is never offered
`audit_list`.
A runtime run's token holds only the tools its caller named in `core.runtime.run`, so none of the
three reaches a run that did not ask for it.

Entity list/get have no MCP tool in release one, so no model reaches them; they are reachable over
the API by a token whose set holds them. Tool origin and delegated operation availability
are checked on every discovery/call; a cached listing grants no authority. READ query text is not
persisted in release-one tool telemetry. Metadata telemetry is workspace-scoped,
owner/operator-readable and bounded to seven days and ten thousand rows or a tighter workspace
setting; failure cannot change a tool result.

No tool in the production set is external or financial class. The recording sink and the
destructive fixture are test-harness registrations, never in the production set (criterion 18);
the sink is release one's only external-class operation anywhere.

Release one ships **no no-query context bundle**: every memory a run is shown is one its caller
named by reference in `core.runtime.run` or one the run fetched itself through
`recallatron_recall` or `recallatron_read`, and a tool that handed a model a bundle of memories
it never asked for is out of scope for this release.

## The redaction contract

What leaves the deployment for a model provider, and what does not. FR 13 and FR 27 fix the
boundary; this is the operational rule.

### Tiers

Every field of every record type is in one tier, declared in the owning module's manifest
(`sensitivity`).

The enforcement lives in `rheo_core.redaction`. A record type's fields are tiered in the
manifest's `sensitivity` map (record type, then field, then tier); a pydantic model's fields
(an event's `data`, an operation's output) are tiered with the `Tiered` marker on the field,
because a model is not a record type and a name match across the two would refuse events the
design names on purpose (a contact-permission record is `restricted` whole, and
`leads.contact_permission.withdrawn` carries its `party_ref` and `purpose`). The policy is
`TierPolicy`: `public` always, `internal` for the purposes `redaction.internal_purposes` lists,
`restricted` never as a field. Recallatron declares no tiered field, so the only change to
what it sends is the free-text mask and the exclusion list.

| Tier | Contents | Sent to a model |
| --- | --- | --- |
| `public` | Titles, stage labels, notes the workspace wrote for itself, message bodies the sender addressed to the workspace, qualification explanations | Yes, when the caller may read the record. |
| `internal` | Party display names, organization names, opportunity value estimates, dates | Yes for purposes the workspace enables (`redaction.internal_purposes`, default `respond, follow_up, internal_analysis`, `subset` floor), no otherwise. |
| `restricted` | Contact point values (email, phone, address), permission records, member credentials, every secret reference, other members' personal material, raw delivery payloads | Never by default. `redaction.contact_points_to_model` (`and` floor, package default false) allows contact point values for **every** purpose when both the operator's value and the workspace's are explicitly true (widened from `respond` only by the maintainer's decision in the issue #130 review). The workspace's opt-in must be a row: a workspace with no row for the key is not opted in whatever the deployment says, so the operator's `true` is a permission, never a default (`ResolvedSettings.set_by_workspace`). Nothing enables the rest. As built, the allowance lifts the free-text contact mask; the default renderers never release a `restricted` field, because the tier does not say which restricted fields are contact values, and a module's own `render_for_model` may release its contact point fields when `TierPolicy.contact_points_allowed` is true. Secret references are masked whatever the allowance says. |

Secrets are not a tier; they cannot enter a context item because no domain service can resolve
one. The tier for secrets exists so that a *reference* string in a record is also withheld.

### The context builder

`rheo_core.runtime.build_context(ctx, refs, *, uow)`. For each reference, in order:

1. Resolve it under `ctx` through the record resolver (`resolve_in`, which for a memory
   applies Recallatron's eligibility rule) and drop it unless it is live and readable.
2. Drop it if `<module>.redaction.exclude_types` lists its record type (below).
3. An **untiered** record type (no `sensitivity` entry and no `render_for_model`, which is
   every Recallatron type) sends its head's `display` text (for a memory, its title), tagged
   `public`: a module that tiers none of a type's fields has declared nothing withheld, and the
   tier table puts titles in `public`.
4. A **tiered** record type sends what its renderer allows. The manifest's `RecordType`
   declares `load_for_model(ctx, uow, ref)`, which reads the record's fields (a resolver
   answers only a head), and optionally `render_for_model(record, tier_policy)`, the ratified
   renderer. With none, the default renderer writes one `name: value` line per field the
   policy allows and omits the rest; a field the loader returns and the tier map does not name
   is omitted too, the fail-closed reading of a point the design leaves open. A record that
   renders to nothing is skipped, and its `display` is never the fallback, because for a tiered
   type the label's tier is exactly what nobody declared.
5. Mask free text, then cap total bytes at `runtime.max_context_bytes` (package default
   200000, floor `min`). Secret references (`secret://...`, any case) are always masked;
   email addresses and phone numbers are masked unless the contact-point allowance is on,
   which takes the operator's value **and** an explicit workspace row of `true`: a workspace
   with no row for the key is not opted in, whatever the deployment says.
   The patterns are conservative on purpose (`rheo_core/redaction/masking.py`): an address
   with a letters-only top-level domain (Unicode letters allowed), a `+` international
   number with a country code starting 2-9 and 9 to 15 digits, or a North American
   `NPA-NXX-XXXX` whose area code and exchange start 2-9, one separator throughout,
   optionally led by `1` or `+1`, a `Tel.` prefix and an extension (`x12`, `ext. 7`)
   included, non-breaking hyphens and en dashes accepted as separators; never a bare run of
   digits, an ISO date, a UUID, a dotted quad or a longer dashed id. They are a backstop
   behind the field tiers. Known limits: three space-separated numbers that satisfy the NANP
   digit rule (`256 512 1024`, `299 399 4999`) still mask; full-width digits, a seven-digit
   local number and a postal address are not recognised. The write-back refusal matches mask
   tokens after NFKC normalisation, casefolding and whitespace collapsing, so
   `[EMAIL WITHHELD]` and a no-break-space variant are refused too.

The run's task text has its secret references masked before the request is built, so no
adapter can send one. Contact values in the task are not masked: the task is the person's own
instruction, so an address they typed into it is meant to reach the model (#149).

The mask runs on the text the renderer hands over, so a resolver that truncates its own
`display` can cut an address before the mask sees it; Recallatron's label is the whole
title and is not exposed to this. The design goes further than what is built in one respect:
obtain memory items only through `recallatron.memory.recall` with the same `purpose` (which
applies the audience, purpose, link, and contact-permission filters,
[memory](memory.md#retrieval-fr-27-fr-30-criteria-27-and-31); FR 27). Derived memories carry
the intersection of their sources' audiences and purposes, so the filter on a memory is never
looser than on what it was made from (criterion 28).

**Purpose comes from the context's principal, not from the argument.** When
[`ctx.principal`](overview.md#the-workspace-context) carries a bound purpose — a runtime token's,
or a job's or approval's stored purpose on a rebuild — that purpose is the one applied.
`build_context` takes no purpose argument at all, and a stored job purpose that disagrees with
its token's refuses `purpose_mismatch` when the job's context is rebuilt, rather than narrowing
or widening silently. A run therefore cannot reach memory outside the purpose its token was
issued for by passing a different value. An unbound authenticated person or
service browsing without a bound purpose has no memory-purpose gate at all, which is the
ordinary interface case and not a bypass: the audience, link and contact-permission filters
still run in full.

### Retention and control

- The core stores context references and a digest, and transcripts for the retention period or
  until a record the run was shown is deleted, whichever comes first
  ([the cascade](deletion-export-migration.md#the-cascade); the deletion half is not built yet,
  so today only `core.retention_sweep` removes transcripts).
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
  (`<module>.redaction.exclude_types`, a list of the module's record type names, `union`
  floor so a workspace can only add to the operator's list), which the builder honours before
  tiering, for tiered and untiered types alike. The loader declares the key for every loaded
  module that owns a record type, and the manifest reserves `<module_id>.redaction.*` to the
  core so no module declares a key there itself. An operator value for it in the environment
  or `deployment.toml` resolves, because module settings are registered before the first strict
  resolve (issue #108).
