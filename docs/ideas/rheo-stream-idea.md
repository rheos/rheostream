# rheoStream — idea document

**Status:** Foundational idea document, not a final specification or build plan  
**Started:** 2026-09-08  
**Project hub:** `rheo.stream`

**Architecture review, 2026-09-08:** Expanded custom-funnel support, a shared
framework core, domain packs, module lifecycle, and the public-code/private-data
boundary. The first reference configuration supports freelance development and
software entrepreneurship. New design details remain recommendations for
specification work.

## Purpose of this document

This document gathers the current rheoStream idea into one coherent starting
point. Its job is to establish the product thesis, vocabulary, boundaries,
architectural direction, and important constraints before detailed requirements,
a final technical specification, or an implementation plan are written.

Some statements below are settled decisions. Others are preferred directions or
deliberately open questions. Keeping those categories separate is important: the
project should be able to move forward without accidentally turning an early
implementation convenience into a permanent product constraint.

## The idea

**rheoStream** is an open, self-hostable framework for composing agent-assisted
working environments. Its first reference configuration serves a freelance web
developer and software entrepreneur: **Leads** develops opportunities,
**Current** carries active work, and **Recallatron** retains useful context. These
are the first official modules, with further modules and integrations added as
different professional workflows need them. An agent named **rheo** operates the
enabled capabilities through a stable, goal-oriented MCP interface. People can
interact through Claude, Telegram, direct voice, and eventually other clients. The
same domain contracts should support private installations, collaborating teams,
and hosted workspaces without fixing all three to one storage topology.

Job search is one optional workflow. A user can instead bring business inquiries,
referrals, assessment responses, product interest, or other opportunities from
their own websites and tools. Assessment sites and specialist service funnels
illustrate that category; the architecture must work for any user's custom funnel.

An HR consultant, regulatory approval consultant, or sales representative should
be able to assemble a suitable working environment from the same infrastructure.
They share needs such as intake, relationships, commitments, evidence, permissions,
reminders, and history. Their specialized records, decision rules, and professional
procedures belong in modules and configuration, outside the framework core.

> Leads enter the stream. Current carries the work. Recallatron remembers.

## Why rheoStream should exist

The present tools contain useful capabilities, but their product boundaries and
names reflect the order in which they were built:

- MOL began as an Upwork triage system and grew into multi-platform opportunity
  discovery, qualification, proposal preparation, and application support.
- M.O.T. became the place where selected work is organized and advanced.
- Recallatron emerged inside M.O.T. as a memory and retrieval system rather than
  as a cleanly independent product boundary.
- Rheo-bot and various Claude routines provide pieces of an agent interface, but
  there is not yet one stable harness through which all the parts cooperate.
- Tuttle demonstrates a strong local-first approach to the administrative side of
  independent work: clients, contracts, projects, time, invoices, tax reserves,
  and cash flow.

The opportunity is not merely to rename these applications or put a chat window
in front of them. It is to reconstruct the useful parts as a coherent system with
clear domain boundaries, a shared agent interface, portable data, and an
architecture that can be released publicly and operated as a service later. The
first client's configuration should be a useful product built on that framework, with
the same extension contracts available to later configurations.

## Product thesis

rheoStream should feel like one continuous working environment rather than a
collection of dashboards. A lead discovered today can become tomorrow's project;
the decisions, people, promises, and lessons surrounding it can remain available
months later. rheo provides the conversational continuity, while the underlying
modules remain explicit systems of record.

The central product ideas are:

1. **Conversation is an interface, not the database.** rheo can reason and act
   across the system, but authoritative records live in the domain modules.
2. **One agent can have several surfaces.** Interactive Claude, Telegram, and
   voice should enter the same session and authorization model instead of becoming
   separate bots with diverging capabilities.
3. **Modules cooperate through contracts.** Leads, Current, Recallatron, and
   integrations expose services and events rather than writing directly into one
   another's tables.
4. **Local ownership and hosted convenience can coexist.** A self-hosted user can
   keep their data locally. A hosted customer can receive isolated managed
   storage. A hybrid customer can connect a local application to hosted services
   without uploading everything.
5. **Automation must remain inspectable.** Source provenance, agent actions,
   external side effects, and significant state changes should be visible and
   auditable.
6. **The public system starts with the new vocabulary.** Legacy MOL, M.O.T.,
   ministry, and Upwork-specific names should not fossilize into rheoStream's
   public APIs, packages, schemas, configuration, or documentation.
7. **Sources and workflows vary independently.** A source describes where evidence
   came from; a workflow describes what a workspace does with it. Adding a funnel
   must not require adding job fields or a special case to the Leads core.
8. **Modules are optional in practice.** A business user can use Leads without job
   search, and a Current or Recallatron user need not install Leads at all.
9. **Professional context is composed.** Domain packs select modules, connectors,
   vocabulary, and workflow presets. The core has no profession switch or required
   freelance workflow; different packs may coexist in one workspace.
10. **Public code and private workspace data are separate.** The repository ships
    reusable definitions and synthetic examples. Personalization and operational
    records live in private local or hosted storage, including the first client's.

## Product vocabulary

### rheoStream

The name of the overall project, public open-source distribution, hosted service,
and project hub. `rheo.stream` is already registered and should become the stable
home for the project.

Likely public endpoints, when they are needed, include:

- `rheo.stream` — project and product home
- `circuit.rheo.stream` — the application shell (originally `app.`; see the recorded
  change of direction in the decision ledger)
- `auth.rheo.stream` — login and the OAuth callback
- `docs.rheo.stream` — documentation
- `api.rheo.stream` — public application API
- `mcp.rheo.stream` — MCP endpoint

Individual module subdomains are unnecessary unless the modules later become
independently deployed public services.

### rheo

The agent that operates across the stream. rheo is the conversational identity,
not an additional system of record. It can search, summarize, propose, remember,
and perform approved actions through tools exposed by the modules.

### Leads

The opportunity-development domain. It receives signals from connected sources,
preserves evidence, identifies related people and organizations, and supports
qualification, follow-up, and outcomes through workspace-selected pipelines.

“Leads” is intentionally broader than “jobs.” It should support freelance
postings, contract opportunities, direct prospects, referrals, grants, partnerships,
or other possible work without forcing them all into an employment-shaped schema.
An incoming signal need not become an opportunity, and a successful opportunity
need not create a project: it might end in a referral or an external CRM handoff.

### Current

The work-in-motion domain. It holds the tasks, commitments, projects, deadlines,
and operational state that move work toward completion, whether it begins with an
opportunity, an existing client engagement, or an independent commitment.

### Recallatron

The memory domain. It stores and retrieves durable context, relationships,
decisions, observations, and history across the rest of the system. Recallatron
should remain recognizable as the memory of rheoStream, while being capable of
serving other systems through a clean interface.

### Tuttle

An optional external back-office integration, not a name to absorb or a codebase
to silently fork. Tuttle covers adjacent business-administration concerns such as
clients, contracts, time, invoices, taxes, and cash flow. rheoStream should be
able to connect to it while respecting its local-first model and GPL-3.0 license.

## Legacy-to-new conceptual map

| Existing asset | rheoStream destination | Migration posture |
| --- | --- | --- |
| MOL opportunity intake, scoring, proposals, and answer library | Leads | Port useful behavior and tests into neutral domain contracts |
| M.O.T. work and ticket management | Current | Reconstruct as the work-in-motion module |
| Recallatron functionality currently inside M.O.T. | Recallatron | Extract behind an independent memory service boundary |
| Rheo-bot and Claude routines | rheo runtime and channels | Preserve useful behavior without preserving ad hoc coupling |
| Jobvis source adapters and patterns | Leads source integrations | Reuse or adapt selectively with MIT attribution and independent review |
| Tuttle | External integration | Connect through supported interchange or a local node; contribute useful interoperability upstream |

The old repositories remain valuable as production systems, behavioral references,
test sources, fixtures, and migration inputs. They should not dictate the final
repository boundaries or public naming.

## Who it is for

The first client is a freelance web developer and software entrepreneur.
rheoStream must improve that daily workflow while making the same foundation
available to other professions. The first configuration provides concrete needs
against which to test the framework; it does not define every user's occupation.
Related audiences include:

- an individual freelancer running the entire suite privately;
- a consultant or small studio with several collaborators in one workspace;
- an HR consultant coordinating client relationships, assessments, and engagements;
- a regulatory approval consultant coordinating evidence, dossiers, and submissions;
- a sales representative developing accounts, opportunities, and follow-ups;
- a business developing inquiries from its own sites, forms, assessments, events,
  product signups, email, or referral partners, with no job-search requirement;
- a technical user who wants only selected modules and connects other tools;
- a hosted customer who does not want to operate infrastructure;
- a contributor building a source adapter, channel, memory strategy, or business
  integration without learning the entire system.

The system need not serve large enterprises initially. It should avoid assumptions
that make organization-level collaboration, isolation, or managed hosting
prohibitively expensive to add.

## Conceptual system shape

```text
  Claude · Telegram · Voice        Web UI       User-owned funnels / tools
              │                      │                     │
      rheo session/runtime     Application API      Connectors / intake API
              │                      │                     │
         MCP façade                 │                     │
              └──────────────────────┴─────────────────────┘
                                     │
                        Shared framework core
               workspace · permissions · module lifecycle
                                     │
                        Optional domain modules
                 Leads · Current · Recallatron · extensions
                                     │
                    Durable operations and domain events
                                     │
                      External destination adapters
                        CRM · Tuttle · referrals
```

This is a logical view, not a requirement that every box become a separate network
service. A small self-hosted installation might run them in one process. The
boundaries matter because they establish ownership and contracts, not because the
project needs premature microservices.

MCP is the agent interface, not the internal event bus or the mandatory path for
webhooks. The web UI, deterministic integrations, and MCP call the same application
services with equivalent authorization. Intake and ordinary domain operations
continue working when no model session is running.

### Framework core, modules, and domain packs

Keep the framework core small. It supplies mechanisms that installed modules use:

| Shared infrastructure | Core responsibility |
| --- | --- |
| Identity and access | Workspaces, membership, authenticated context, policy enforcement, and channel identity |
| Extension lifecycle | Module registration, dependency and compatibility checks, configuration, activation, and removal |
| Agent and application access | Runtime/channel contracts, authorized tool discovery, API routing, and registered UI contributions |
| Durable execution | Jobs, scheduling, event delivery, operation tracking, approval records, and scoped secret access |
| Data operations | Storage provisioning, per-module migration tracking, export/restore coordination, and audit infrastructure |
| Record references | Stable module/type/record references and permission-checked resolution, without owning referenced business data |

The core must not know what a job application, HR assessment, regulatory dossier,
or sales stage means. Modules own those meanings, including their validation and
state transitions. Shared storage plumbing does not imply a universal business
record table. Audit evidence is essential infrastructure even when optional
Recallatron memory is disabled.

Distinguish reusable business capabilities from infrastructure as well. People and
organizations, document/evidence management, and commitments may be useful across
professions, but their business rules still belong in modules. Add a shared module
when a concrete workflow needs it; avoid making every possible shared capability
mandatory. The separation of shared services and feature extensions has a useful
design precedent in [Backstage's backend system](https://backstage.io/docs/backend-system/).

Keep four extension concepts distinct:

| Extension | Responsibility | Example |
| --- | --- | --- |
| Connector | Authenticate to a source or destination, translate its contract, track delivery | Generic signed webhook, form adapter, CRM adapter, job-board poller |
| Workflow preset | Supply a versioned starting configuration for fields, stages, qualification, drafts, and outcomes | Job search, inbound services, referrals |
| Domain module | Own authoritative business behavior and records | Leads, Current, Recallatron; later assessments or dossiers |
| Domain pack | Compose compatible modules, connectors, presets, templates, and vocabulary for a useful starting experience | Freelance software business, HR consulting, regulatory consulting, sales |

Public domain packs are reusable, versioned configuration bundles. Workspace-specific
overrides and resolved connection details are private data; applying a pack must
not rewrite the tracked package with those values. Packs do not own duplicate records
or require a fork of the application. A workspace can combine packs, replace their
defaults, or select modules individually. Applying a pack produces an inspectable
configuration/dependency plan and preserves existing user choices; overlapping packs
cannot silently overwrite one another's settings. Removing a pack does not remove
modules that another pack or the workspace still uses.

Possible compositions, for architectural illustration rather than release scope:

| First client or later domain | Reusable modules | Domain-specific additions |
| --- | --- | --- |
| Freelance development and software business | Leads, Current, Recallatron, relationships | Optional job search, proposals, estimates, software-project connectors, product-business pipelines |
| HR consultant | Relationships, Current, optional Leads and Recallatron | Assessments, engagement templates, restricted case records, specialist evidence rules |
| Regulatory approval consultant | Relationships, Current, optional Leads and Recallatron | Dossiers, evidence revisions, submission lifecycle, specialist review rules |
| Sales representative | Relationships, Leads, optional Current and Recallatron | Sales-stage definitions, account qualification, follow-up templates, CRM connectors |

These workflows share infrastructure without sharing every domain rule. A dossier
with controlled evidence revisions may need a dedicated module; changing a few
stage labels in Current would not implement that behavior. A new field, template,
or stage sequence can usually remain preset configuration. This is the test for
choosing a new module versus extending an existing one.

Each workspace chooses modules, connections, and pipelines. A pipeline is a working
instance of a preset with its own owner, configuration version, and permitted
actions. Multiple funnels can feed one pipeline; one source can feed several
pipelines through explicit routing rules. A pipeline is not a tenant boundary.
Client data that needs separate ownership or isolation belongs in separate
workspaces, even when one operator manages both.

Start with declarative, schema-validated configuration and a small set of supported
actions. Do not accept arbitrary executable scripts as a hosted workspace setting.
Unusual mappings can run in a user-owned adapter outside rheoStream. New business
behavior can be implemented as a trusted, versioned module through the extension
contract. Arbitrary tenant-uploaded code needs a separate isolation design and is
not an initial hosted capability.

Job-specific fields, candidate profiles, matching criteria, proposal templates,
tools, and polling jobs belong to the job-search capability. Business users should
not need to configure or see them. Disabling a capability stops its workers and
new actions while retaining existing records for inspection and export.

The capability registry reports enabled modules, available actions, required
permissions, configuration versions, and connection health. rheo and the UI use it
to present relevant operations. Discovery does not grant permission: every call
is checked again by the service, including a cached tool call after disablement.

### The module contract

A module should be installable through a documented registration contract. Its
manifest and implementation declare:

- stable module identity, package version, compatible core contract versions, and
  required versus optional dependencies with compatible capability versions;
- owned record types, private storage destinations, data sensitivity/retention,
  migrations, configuration schemas, and versioned
  export/import formats, including a readable export without its runtime installed;
- provided services, tools, events, and subscriptions, with typed inputs/results,
  permissions, action classes, and idempotency semantics;
- navigation, record views, forms, search providers, and agent guidance registered
  through defined extension points and filtered by current authorization;
- workers, schedules, connector bindings, secret scopes, and any external effects;
- install, upgrade, enable, disable, export, and removal behavior, plus health checks
  and contract tests.

Capabilities are versioned semantic contracts, not just tool names. A replacement
provider must satisfy the same behavior before dependents can use it. Record types,
events, routes, and tools need module namespaces to prevent collisions. Agent
instructions supplied by a module describe its use but cannot grant permissions.

Maintain a resolved module/configuration manifest per workspace and schema versions
per installed module. Check dependencies before activation and upgrade; reject
cycles and incompatible combinations with an actionable explanation. Optional
integrations need explicit unavailable states instead of hidden runtime dependencies.

For the first release, trusted modules can be ordinary packages assembled into a
modular monolith through supported registration. Code installation may require a
build or restart; easy composition does not require hot-loading executable code.
Separate host-installed package availability from per-workspace activation. A
hosted operator selects deployable code; workspace administrators enable permitted
modules and packs. In-process modules share a runtime trust boundary, so registry
permissions alone do not sandbox malicious module code.

### Adding, disabling, and removing modules

| Operation | Required behavior |
| --- | --- |
| Add / enable | Resolve versions and dependencies, prepare storage/configuration, show required permissions, and activate after initialization succeeds |
| Upgrade | Check compatibility and migrations; preserve configuration and records, with a tested restore/recovery path before destructive schema changes |
| Disable | Block new commands, pause intake/subscriptions/schedules, drain or cancel tracked jobs, and remove active UI/tool contributions while preserving data |
| Re-enable | Restore compatible configuration and resume deliberately; catch-up processing must not replay already completed external effects |
| Remove from workspace | Check required dependents, references, and unresolved operations; export/archive retained data, revoke module-owned bindings, and detach runtime capabilities |
| Purge retained data | Apply a separate explicit deletion operation with retention, reference, memory/index, and external-copy handling |

Disabling must invalidate cached tool access and fence workers from starting new
effects. Already-dispatched external work may still complete: reconcile it or leave
an explicit unresolved operation rather than pretending disablement reversed it.
Required dependents block removal until migrated or included in an explicit removal
plan. Optional dependents degrade gracefully. Never cascade-delete unrelated work,
shared party records, credentials used elsewhere, or another workspace's data.

References to detached modules should remain identifiable as unavailable or archived.
Permission-checked snapshots and self-describing exports preserve useful history
without requiring the removed module's executable code. Record reactivation rules
and event checkpoints explicitly so reinstallation cannot cause duplicate sends.
Host-level package removal is a separate operation and must account for every
workspace that still uses that package.

## rheo: agent and interaction model

rheo's AI execution must be configurable through replaceable runtime adapters.
**Claude Code in print mode (`claude -p`) and OpenRouter API execution are explicit
requirements. Codex CLI is an additional target adapter:** its documented headless
interface makes it technically feasible, with integration and workflow suitability
still to validate. No module or channel may assume one of these runtimes is present.

Runtime selection and model selection are different settings. A runtime supplies
the conversation and tool-execution machinery; a model/provider supplies inference.
The conceptual adapters are:

| Adapter | Execution path | Responsibility and intended use |
| --- | --- | --- |
| `ClaudeCliRuntime` | Spawn `claude -p` | Use Claude Code's agent loop, configured MCP tools, structured/streaming output, and scoped resumable sessions for local or personal self-hosted operation |
| `OpenRouterRuntime` | Call the OpenRouter API with a configured model and provider policy | Supply a Rheo-controlled agent loop (directly or through an SDK), translate authorized tools, execute permitted calls, and return tool results to the model; usable by local and hosted installations |
| `CodexCliRuntime` | Spawn `codex exec` | Use Codex's non-interactive agent loop, MCP integration, structured events, and explicit session IDs; validate as an alternative CLI adapter against the same contracts |
| Other API/SDK runtimes | A supported provider API or agent SDK | Add direct-provider or other model execution without changing domain modules |

[Claude Code's programmatic interface](https://code.claude.com/docs/en/headless)
supports `-p` with `--output-format json` or `--output-format stream-json`; streaming
uses `--verbose`. [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
uses `codex exec`, with `--json` for JSONL events, `--output-schema` for a structured
final response, and `codex exec resume <SESSION_ID>` to continue a specific session.
These are existing CLI capabilities, not implemented rheoStream adapters.

OpenRouter supplies model inference and tool-call requests. Its
[tool-calling flow](https://openrouter.ai/docs/guides/features/tool-calling) leaves
tool execution with the client. The proposed adapter must therefore manage the
agent loop and dispatch through rheo's authorized MCP/domain boundary. Changing
an API base URL alone is not a complete replacement for either CLI agent.

### Runtime configuration and portability

The operator configures available adapters, credentials, and policy privately.
Each application workspace may select a permitted runtime/model default, with
explicit workflow overrides where useful. Store those choices and secret references
in private configuration or the workspace database, not public packs. Hosted users
select from operator-installed adapters; they cannot provide arbitrary shell
commands, executable paths, or provider endpoints as workspace settings.

The runtime contract should cover:

- A request bound to an authenticated actor, workspace, audience, and durable
  operation, with scoped context, permitted tools, output requirements, and limits.
- Capability discovery for tool calling, structured output, streaming, continuation,
  cancellation, and usage reporting. Reject unsupported requirements explicitly;
  sharing an interface does not make every model or runtime equally capable.
- Normalized progress, tool results, final output, failure, cancellation, and approval
  states. Check terminal status and validate results; a text reply is not proof that
  a requested domain action succeeded. Report usage/cost only when available, with
  estimates identified separately, and enforce host deadlines and iteration limits.
- Rheo-owned business state and permitted conversation history. Native session IDs
  are private adapter handles bound to runtime, credential scope, actor, workspace,
  and audience. Never resume a global "latest" session. Switching adapters starts
  a new native session with selected authorized context; opaque session files are
  not portable, and completed effects must not be replayed.

For OpenRouter, model capabilities and underlying provider routing both matter.
Set an explicit model/provider allowlist, required parameters, data policy, and
fallback policy; use `require_parameters` where needed so unsupported parameters
are not silently ignored. Fallbacks must stay within the approved policy rather
than widening who receives private context. These controls are described in
[OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
Changing providers or retrying inference must not bypass action approval or repeat
an already completed external effect.

CLI adapters need a trusted executable and argument list, bounded subprocess
execution, and explicit configuration for tools, hooks, plugins, environment, and
private session storage. Pass task content as data, never interpolated shell code.
Use a scoped working directory and constrained filesystem/network access; opening
the parent development workspace must not expose every private sibling to a run.
Built-in shell/file tools must not bypass rheo's authorization or write its domain
database directly. If an adapter cannot enforce a workflow's required boundary,
that adapter is unsupported for that workflow. Headless execution must surface a
denied or approval-required action without hanging or silently granting permission.

Local CLI execution does not imply local/offline model inference. Provider data
disclosure and authentication remain explicit choices. Personal installations can
use locally provisioned CLI authentication where supported. A hosted SaaS should
use an API or supported agent SDK with service-owned credentials; it must not depend
on customers sharing consumer CLI subscription logins. The first production default
and rollout order remain open; the architecture must accommodate these choices.

The runtime owns model conversation mechanics. rheoStream owns identity,
authorization, tool policy, domain data, audit history, and durable work state.

Channels share identity and policy services, not automatically the same transcript
or audience. A private conversation and a group Telegram conversation have distinct
visibility. Bind each session and resumable operation to its authenticated actor,
workspace, and audience; linking a channel requires authenticated enrollment.
Revocation must also apply to queued work and resumed sessions.

### Interaction surfaces

**Interactive Claude** is the shortest path to a useful first agent. rheo's MCP
server supplies the tools while Claude supplies the conversational environment.

**Telegram** provides asynchronous, mobile access. It should be another client of
the same rheo session layer, not a second implementation of business rules.

**Direct voice** adds speech recognition and synthesis around that same session:

```text
speech → transcription → rheo session → MCP/domain action → response → speech
```

The voice provider should not become an independent reasoning agent with its own
tool behavior. Text and voice may render responses differently, but they should
share permissions, memory, confirmations, and action history.

## MCP as the stable agent boundary

rheoStream should initially present one coherent MCP façade with namespaced,
goal-level tools. Domain services sit behind it. The MCP layer must not expose raw
database access or merely reproduce every CRUD endpoint.

Illustrative tools include:

```text
leads_search
leads_list
leads_get
leads_ingest
leads_enrich
leads_qualify
leads_prepare_followup
leads_transition
leads_handoff

# Only when the job-search capability is enabled:
leads_prepare_application

current_list_work
current_create_work
current_complete_work

operations_get

recallatron_recall
recallatron_remember

tuttle_list_clients
tuttle_export_project
```

These names are examples for the future contract, not a frozen tool specification.

The authenticated request or session supplies the workspace and user context. A
model must never be trusted to choose an arbitrary `workspace_id`, database path,
or organization identity in its tool arguments. Likewise, database administrator
credentials or a Supabase service-role key must never be available to the model.

Tools should belong to clear safety classes:

- **Read:** inspect and retrieve information.
- **Draft:** prepare a proposal, message, plan, or possible state change without
  causing an external effect.
- **Mutate:** make a reversible internal state change.
- **External side effect:** send, submit, publish, pay, delete, or otherwise affect
  another person or system.

External side effects require explicit policy and usually user confirmation.
Mutating operations should be idempotent where practical and produce an audit
record. Long-running work should return an operation identifier and expose progress
rather than holding a fragile request open indefinitely.

An approved external action should identify the exact recipients or destination,
payload version, purpose, and permitted execution window. Changing those inputs
invalidates the approval. Recheck current permissions and contact restrictions
before execution; a conversation's earlier approval cannot override revocation.
Deletion is destructive even when it affects only internal data, and should not be
classified as an ordinary reversible mutation.

Treat imported text, URLs, attachments, and retrieved memories as untrusted
evidence. They cannot grant tools, change policy, or authorize sending data.
Connector credentials remain inside their adapters. Remote MCP authentication must
validate token audience and must not pass a client's token through to unrelated
downstream services. These boundaries follow the
[MCP security guidance](https://modelcontextprotocol.io/docs/draft/tutorials/security/security_best_practices).

## Leads

Leads is an evidence-preserving opportunity system with configurable pipelines.
The common core should be small, but it needs stronger distinctions than a `jobs`
table renamed to `leads`.

### Separate signals, parties, and opportunities

| Concept | Meaning and ownership |
| --- | --- |
| Source connection | A workspace's configured source account or endpoint, with permissions, mapping version, cursor, and health |
| Funnel | The acquisition context: a website, campaign, assessment, referral program, or other user-defined path; distinct from transport |
| Signal / observation | Evidence that something happened or was seen; a submission, referral, download, posting, or update |
| Party | A person or organization linked by explicit roles to opportunities; a person can represent different organizations over time |
| Opportunity | A possible outcome worth pursuing in a particular pipeline, with an owner, stage, evidence, and history |
| Qualification | A versioned assessment of an opportunity against a particular objective and rubric |
| Handoff | A tracked request to create related work or transfer an approved subset of information to a destination |

“Lead” remains the product term. Internally, distinguish an intake signal from an
opportunity being worked. A download can stay in the intake history; a consultation
request can create an opportunity under an explicit rule. Observations can be
unlinked, linked later, or support several opportunities through recorded decisions.

The same person completing two funnel steps produces two observations, not two
contacts by default and not necessarily two deals. The same organization can have
several unrelated opportunities, and several contacts can participate in one deal.
An anonymous job posting can exist without a known party. An assessment's domain
score does not establish buying intent or permission to make contact.

Shared party identity should sit behind a small reusable relationships module,
independent of Leads. This is a refinement required by the broader framework goal:
other domains need people and organizations even when opportunity management is
absent. It is a business module, not the framework's login-identity service or a
full CRM. Modules that need it declare that dependency explicitly.

Relationships owns the agreed person/organization identifiers and shared contact
fields. Leads owns opportunity participation; an HR case module owns confidential
case facts; a dossier module owns its applicant/evidence relationships. Each module
references the party without turning the shared contact record into an unrestricted
container for specialist data. Referencing a party does not grant access to all
records about it. Current can retain labeled handoff snapshots; Recallatron links
to authoritative records under their current permissions.

An external CRM connection must declare which system owns which fields and how
identifiers and permitted updates map. Replacing that provider requires compatible
contracts and explicit identity migration, rather than allowing two competing
authorities or silently changing stable references.

### Any user can connect their own funnel

The baseline integration should be a documented, authenticated ingestion API with
a generic webhook receiver and declarative field mapping. Add CSV/JSON import,
manual capture, email ingestion, and incremental API polling as adapters. Users
can connect a custom site or automation tool without contributing a provider to
the core repository or adopting rheoStream as their form builder.

A connection maps source events to normalized observations and explicit routing
rules. It declares supported event types, source identity, field mappings, schema
version, contact-permission evidence, and allowed destination pipelines. Preserve
acquisition attribution separately from delivery transport: a form delivered by
an automation service still came from that form and campaign.

Two unrelated custom funnels and one job-source fixture should exercise the same
intake contract before it is frozen. Individual products are consumers of that
contract, not required adapters or core types. Public examples use synthetic data
and neutral project names.

### Intake contract and delivery guarantees

The receiving boundary authenticates the connection and resolves its workspace.
An event's claimed tenant, destination, or permissions never override that binding.
External credentials belong on a server or connector, never in a public form's
browser code. A future hosted public-form endpoint needs a separate, limited
capture contract with abuse controls, rather than exposing privileged ingestion.

The normalized observation envelope should include:

- stable source-event identity, event type, and payload schema version;
- external subject identity when available, separately from the event ID;
- source occurrence time, server receipt time, and source revision when available;
- connection, funnel, and attribution references resolved or validated by the server;
- normalized facts, per-field provenance, completeness, and bounded source payload;
- source references and purpose-specific contact/sharing permission evidence.

Use a CloudEvents-compatible envelope where useful. Its distinction between event
identity (`source` plus `id`) and subject identity is relevant here; the envelope
does not supply authorization or delivery guarantees.
[CloudEvents 1.0.2 specification](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md).

Validate signatures or scoped credentials, payload size, and schema before durable
acceptance. For signed webhooks, validate a delivery timestamp and replay window
where the sender supports them; retain stable event identity across legitimate
retries. Acknowledge only after storing a receipt and pending processing work
durably. Pollers advance checkpoints only after durable acceptance. An accepted
receipt means processing is pending, not that a qualified opportunity exists.
These recommendations build on the authenticated delivery and asynchronous
processing patterns in [GitHub's webhook guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks).

Assume deliveries may repeat, arrive late, or be missed. Enforce receipt uniqueness
within the trusted workspace and source namespace. Reusing an event ID with changed
content is a conflict, not a silent update. Track processing attempts, bounded
backoff, failed items, and deliberate replay. Provide reconciliation or backfill
where the source permits it; report sources whose missed events cannot be recovered.
Expose connection health, last successful intake, lag, and unresolved failures.

### Matching and evidence reconciliation

Keep three separate decisions:

1. **Delivery deduplication:** has this source event already been accepted?
2. **Party resolution:** does this person or organization match an existing party?
3. **Opportunity matching:** does this evidence concern the same possible outcome?

Within a connection's source namespace, external subject identifiers are strongest.
A provider-specific canonical URL can identify a job posting; a website homepage,
landing-page URL, email address, or company domain cannot identify a business deal.
Email and domain matches are party hints, not global merge keys. Avoid cross-workspace
matching; ambiguous matches need review, and merges need an auditable reversal path.

Preserve observations under their retention policy and derive current fields using
explicit source priority, revision/freshness, and field ownership rules. Distinguish
missing fields from explicit clears. A late event cannot blindly overwrite newer
evidence; a richer observation cannot overwrite a user-owned stage or note.
Retractions and corrections can invalidate earlier evidence even when their payload
is thinner. Preserve conflicts visibly when the source has no reliable ordering.

Public identifiers should include `source_connection_id`, `external_subject_id`,
`canonical_url`, `ingestion_method`, `source_observations`, and `content_quality`.
Job-specific IDs belong in the job adapter's mapping. Do not replace `upwork_url`
with another supposedly neutral field such as `external_job_id` in the common core.

### Configurable pipelines with bounded extensions

Keep common opportunity fields explicit: identity, title, pipeline and version,
stage, owner, linked parties, provenance, timestamps, and disposition. Optional
value estimates carry currency and basis; a project fee, annual contract value,
hourly rate, and referral fee are different quantities.

Add workflow-specific fields through versioned, namespaced, schema-validated
extensions. Job search can carry application questions and candidate-fit details;
a business pipeline can carry service interest, organization size, or buying
timeframe. Define types, required fields, sensitivity, and query behavior. Avoid
both a core table full of nullable job columns and an unvalidated JSON record that
no service can reliably query or migrate.

Examples of independently selectable pipeline configurations:

| Workflow | Example stages | Possible successful outcome |
| --- | --- | --- |
| Job search | Review → qualified → applied → interviewing | Accepted role or contract |
| Inbound services | Triage → discovery → qualified → proposal → decision | Service engagement |
| Referrals | Review → eligible → introduction prepared → accepted | Confirmed introduction |

Presets supply allowed transitions, required evidence, qualification rules, draft
templates, and optional handoffs. Keep stable stage IDs separate from editable
labels, and map terminal dispositions to comparable outcomes such as succeeded,
lost, or disqualified. Sending an introduction does not imply that it was accepted.
Nurture can remain an open stage. Each opportunity starts in one primary pipeline;
moving it is a validated operation with explicit field/stage mapping.

Pin configuration versions on opportunities and qualification results. Editing a
preset must not silently reinterpret existing records. Begin with a small set of
state transitions and actions, rather than a general workflow programming language
or visual automation builder.

### Qualification and feedback

An assessment records its objective, rubric version, evidence references, input
revision, explanation, and uncertainty. If a model participates, retain the model
and prompt version. Separate fit, intent, evidence completeness, and urgency;
avoid presenting one universal score across unrelated pipelines.

For job search, fit may mean suitability for the workspace's candidate profile.
For a business, it may mean suitability for its services and operating region.
Missing evidence should produce "needs information" rather than invented certainty.
Reassess when relevant evidence or the rubric changes, without erasing prior
assessments or user decisions. A late assessment cannot replace one based on newer
inputs. Use accepted/lost outcomes and explicit feedback to evaluate a rubric;
do not silently retrain rules or compare conversion rates with different definitions.

### Contact purpose, access, and retention

Keep observed interest separate from permission to contact or share. Preserve the
declared purpose, source disclosure/version, timestamp, channel, permitted recipient
scope, and later withdrawal or suppression. A content download does not authorize
unrelated outreach or onward sale of contact details. These are product-policy
requirements; applicable legal rules still belong in the later data-policy review.

Collect and export only the fields needed for an authorized purpose. Deletion and
correction must reach derived summaries, search indexes, queued actions, and local
exports under the retention policy. Retain only the minimum permitted suppression
and audit metadata needed to prevent re-import or repeated contact. External copies
need a tracked correction/deletion request where supported; rheo must not claim it
can erase another system's copy automatically.

### Optional job-search source landscape

For a workspace that enables job search, candidate sources include:

- **Upwork** — email alerts, copied search results, copied full postings, and an
  official API adapter if access becomes available;
- **Braintrust** — existing API polling behavior;
- **JSearch / OpenWeb Ninja** — a broad job-search API used by Jobvis;
- **Adzuna** — an additional job listings API used by Jobvis;
- **Remotive** — remote-work listings, subject to its attribution and hosted-use
  terms;
- an **offline cache or fixture source** for demos, development, and deterministic
  tests rather than as a live provider.

Additional boards should be integrations, not new branches in the core domain
model. Provider licensing and attribution requirements must be checked before a
source is enabled in a public hosted service.

### Upwork ingestion and enrichment

Upwork currently requires several imperfect observations of the same opportunity:

1. An email alert provides thin discovery data.
2. A copied search result provides more metadata and fresher competition signals.
3. A copied full posting provides the text needed for meaningful scoring and
   application preparation.
4. A future Upwork API response could become the authoritative observation.

The email transport should become a deterministic client-side poller using Gmail
API or IMAP, with a cursor or UID checkpoint, rather than a Claude routine. On a
desktop this means an operating-system service or scheduled process, not a timer
that only works while a browser tab is open. Claude is not needed to move a known
email format into an ingestion endpoint.

Changing the transport does not make the email content richer. Email-only leads
should remain visibly provisional and should not receive a confident final score
until a sufficiently complete observation is available.

### Outcomes and optional handoffs

Leads owns qualification, opportunity stage, correspondence history, and the next
follow-up reminder needed to operate it independently. Current owns delivery work
and any explicitly created tasks; when it owns a linked task, Leads displays that
task's state rather than maintaining a second editable copy.

A qualified opportunity may become work in Current, an external CRM record, a
referral, a future nurture item, or a closed outcome. Current can also hold a
discovery task before an opportunity is won. There is no mandatory sequence in
which every signal becomes a lead and every lead becomes a project.

Use an explicit handoff operation with a durable ID, source opportunity, purpose,
destination, approved payload snapshot, and result reference. The destination must
deduplicate retries of the same handoff. A lost response after successful creation
must not create a second project on retry. Multiple intentional handoffs use distinct
IDs and purposes; avoid a single `converted` flag that assumes one project forever.

The destination owns its resulting work state; Leads retains opportunity and handoff
history. Track pending, succeeded, failed, and uncertain outcomes, and reconcile
them without direct cross-module table writes. Mark an opportunity's business
outcome only under its pipeline rules, not merely because a delivery was queued.

Referral or cross-workspace handoffs are explicit disclosures to a permitted
recipient, not access to the source workspace. Begin with one chosen destination
per handoff; multi-buyer distribution and marketplace features remain out of scope.

## Current

Current represents commitments and work in motion. Its exact product model still
needs discovery, but its boundary should be broader and cleaner than a renamed
ticket tracker.

Likely concerns include:

- projects, tasks, and next actions;
- status, priority, due dates, and dependencies;
- commitments to clients or collaborators;
- work created from a lead or created independently;
- notes and relevant Recallatron context;
- events that external systems such as Tuttle can observe.

Current should not become an invoicing or accounting product merely because work
eventually generates money. Those concerns can be delegated to Tuttle or another
integration.

Current can coordinate tasks linked to a specialist module's records. For example,
it can track preparation work for a dossier while the dossier module owns evidence
versions and submission state. Completing a task cannot itself assert that a
submission was accepted. The same separation applies to assessment completion,
client commitments, and other domain outcomes.

## Durable execution across modules

Prefer a modular monolith with a durable worker initially. Synchronous commands can
call a module's public service in process; background effects use persisted work
records. A module commits its state change and outgoing event in one transaction
through an outbox, then a worker delivers the event. Consumers record processed
event IDs with their own state changes. This addresses the gap between saving data
and publishing its event, while requiring duplicate-safe consumers.
[Transactional outbox pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).

Events describe facts such as `observation.accepted`, `opportunity.qualified`,
`handoff.requested`, and `work.created`. Version their schemas and include event,
subject, correlation, causation, and subject-revision identifiers. Pass authenticated
workspace context separately from untrusted external event claims. Prefer small
payloads with permission-checked references over copies of all lead data.

Workers need leases, bounded retries, failure inspection, cancellation, and restart
recovery. Use record versions for concurrent edits and late model results; protect
the same handoff or action against concurrent execution. Replay that rebuilds a
read model must not resend messages or repeat other external effects.

An external timeout can leave the outcome unknown. Record the provider request ID
and reconcile status before retrying; if the provider offers neither idempotency
nor status lookup, require resolution rather than claiming exactly-once delivery.
Keep an operation visible until its result is known or explicitly resolved.

This calls for ordinary domain tables, a durable job/outbox mechanism, and an audit
trail. It does not require Kafka, a distributed workflow platform, or event sourcing
as the authoritative data model in the first release.

## Recallatron

Recallatron is durable memory rather than an unbounded transcript archive. It
should help rheo retrieve the smallest useful context for the present task and
retain knowledge that remains valuable across sessions.

Its concerns can include:

- memories and their provenance;
- entities and relationships;
- decisions and why they were made;
- semantic and lexical retrieval;
- time, confidence, correction, and supersession;
- links back to authoritative Leads and Current records.

Recallatron should not silently become the authoritative database for domain
records. A remembered summary of a lead is not the lead itself; a remembered task
is not Current's task record. This distinction permits memory strategies to evolve
without corrupting operational state.

Retrieval must enforce the caller's current workspace and record permissions before
content reaches a model. Derived memories inherit their sources' access and purpose
restrictions; combining sources must not broaden their audience. Supersession,
correction, and deletion need to invalidate summaries and embeddings as well as
the source record. Memory retention must be explicit, not automatically permanent.

## Tuttle and the back-office boundary

Tuttle is a promising adjacent component because it already addresses much of the
administrative lifecycle that follows winning work. The preferred strategy is to
connect to it rather than transplant its GPL code into rheoStream.

Useful integration paths, from least to most coupled, include:

1. structured export and import;
2. a documented local API or command interface;
3. a small local `rheo-node` that makes authenticated outbound connections and
   exposes approved Tuttle operations to a remote rheo session.

A first upstream contribution to Tuttle could improve structured CSV/JSON or
accounting export. That is useful to Tuttle independently and creates a legitimate
interoperability boundary for rheoStream.

Tuttle currently uses a local SQLite database per user through SQLModel/SQLAlchemy,
with Alembic migrations. SQLite itself does not prevent a future hosted or
multitenant arrangement. The more significant adaptation issues are Tuttle's
desktop lifecycle, filesystem assumptions, local Python process, and present
single-user interaction model.

## Workspaces, tenancy, and storage

### The invariant is workspace context

Every operation should execute inside an authenticated **workspace context**.
That is the architectural invariant. A workspace is the isolation, ownership,
export, retention, and collaboration boundary. A one-person installation starts
with one single-user workspace; an organization may grant several users access to
one workspace.

This does not require every deployment to store a `workspace_id` column in every
table. A physically separate per-workspace database already supplies that boundary.
It does require the service layer to receive a trusted workspace context rather
than assuming one global user or accepting tenant identity from model-generated
arguments.

### Private data placement in each deployment mode

Local operation is the expected default for many users. Store runtime data in an
OS-appropriate application-data directory outside the source checkout, with a
documented operator-configured data root when needed. The core resolves private
workspace locations; a module requests scoped storage and must not choose arbitrary
paths or write user records into its installed package. Private files and directories
use restrictive OS permissions appropriate to the owner or service account.

For development, use an unversioned parent folder as the editor workspace, with
the public `rheo-stream/` checkout beside private `private/`, `workspaces/`, and
`backups/` directories. The parent has no Git repository. An operator-controlled
path map outside the checkout connects these locations; do not expose private
siblings through symlinks in source. Git, build contexts, and publication operate
only on the child checkout. See the [workspace layout](../workspace-layout.md).

This filesystem workspace is distinct from an authenticated application workspace:
one local parent may hold data for several application workspaces, and opening the
parent in an editor grants no application permissions. Runtime initialization must
validate operator-configured roots and resolve each workspace's storage within
them. Modules and incoming payloads cannot supply arbitrary paths. The scaffold's
local path map records locations only; it does not yet implement runtime loading,
module registration, or access control.

Local and hosted workspaces use the same ownership model. Profiles, rates, service
offerings, private agent guidance, configured pipelines, client records, and module
settings live in workspace storage; personal preferences and credentials may also
need member-level access within that workspace. A shared workspace does not make
every member's private information readable by all collaborators.

| Data | Local installation | Hosted installation |
| --- | --- | --- |
| Structured records and private configuration | Workspace database in the private data root | Isolated workspace database or authorized tenant-scoped rows |
| Uploads, evidence, attachments, and generated documents | Private workspace files with metadata/access records in the database | Private workspace file/object storage with authenticated access |
| Memory, transcripts, search indexes, and operation history | Private workspace storage with explicit retention | Workspace-scoped databases/indexes and protected object storage |
| Credentials, tokens, signing/encryption keys | OS credential store or permission-restricted secret storage | Secret manager or dedicated encrypted secret storage; keep key material separate from ordinary data and backups |
| Exports, logs, caches, and backups | Private paths outside the checkout, including temporary generation files | Protected operational storage with workspace scope, retention, and restore controls |

Files may be referenced from the database without putting every attachment inside
it. Those references must not expose public object URLs or bypass access checks.
Encryption does not make a database, backup, or export suitable for a public repo.

Keep configuration sources distinct: public package defaults, private deployment
settings, and private workspace/member overrides. Validate their precedence;
workspace overrides cannot relax operator security policy. Store secret references
in configuration rather than credentials in committed manifests. Exporting a
reusable pack is a separate allowlisted operation that strips private overrides,
identifiers, connection details, and secret references and produces a reviewable
preview. A workspace backup is private and must never double as a public example.

A fresh clone must build and run a synthetic demo without the first client's
database, profile, infrastructure, or credentials. Initialization creates private
state separately and must leave tracked source and public defaults unchanged.

### Per-workspace SQLite is a serious option

A hosted rheoStream could give each workspace its own SQLite database. This is a
form of multitenancy with physical data separation rather than row-level security.
It offers attractive properties:

- straightforward tenant isolation;
- workspace-level backup, restore, export, and deletion;
- portability between hosted and self-hosted operation;
- reduced risk that an incorrect query exposes rows belonging to another tenant;
- compatibility with local-first components and offline-capable installations.

The natural unit should be a workspace rather than an individual user so that
collaboration does not require merging personal databases.

Encryption has distinct layers. Storage-volume encryption protects disks and
snapshots. Application-level database encryption, such as a SQLCipher-compatible
approach with a different key for each workspace, can provide a stronger boundary.
Keys should be held separately in an appropriate key-management service. Backups,
write-ahead logs, temporary files, exports, and key rotation must be included in
the threat model; encrypting only the main file is not a complete design.

### Operational implications

Database-per-workspace moves complexity out of SQL policy and into orchestration.
A hosted implementation needs:

- a trusted registry mapping workspace identities to database locations and keys;
- safe connection routing that never accepts an arbitrary path from a client;
- migration orchestration and schema-version tracking across many databases;
- encrypted backup, restore, export, and deletion workflows;
- coordination or compute affinity when several application replicas may access
  one database;
- deliberate handling of concurrent writers and long-running jobs;
- observability that does not leak tenant data;
- a separate strategy for cross-tenant operational metrics.

These are solvable constraints, but they must be acknowledged before choosing the
hosted runtime.

### Shared Postgres with RLS remains possible

PostgreSQL/Supabase with row-level security remains a valid implementation for
modules or deployments that benefit from shared relational storage, conventional
horizontal scaling, and centralized operations. It is not the only acceptable
definition of multitenancy and should not be allowed to become an irreversible
assumption inside the domain layer.

A hybrid is also plausible: a small shared control plane for accounts, workspace
registry, billing, and service metadata, with encrypted SQLite data planes per
workspace. Alternatively, the hosted edition could use Postgres while the
self-hosted edition uses SQLite, provided both are exercised through the same
behavioral contract and the cost of supporting two implementations remains
justified.

The storage decision belongs in the later specification. The idea-stage commitment
is to avoid domain and API choices that make either isolated SQLite or secure
shared storage needlessly difficult.

Choose and operate one reference storage implementation first. Preserve the domain
boundary, export format, and behavioral tests without promising two production
backends before there is a demonstrated need. Keeping SQL in adapters alone does
not prove parity: transactions, locks, search, migrations, and recovery must be
validated for each backend before claiming support.

Likewise, local ownership and a hybrid node do not imply bidirectional offline
synchronization. Start with one authoritative writer location per workspace/domain.
A disconnected node exposes stale or unavailable state and queued operations with
explicit status. Multi-device concurrent offline writes require a separate conflict
and identity design; they are not part of the initial portability promise.

## Deployment modes

rheoStream should be able to grow toward three related modes.

### Self-hosted

The user installs the harness and selected modules, owns the data and credentials,
and selects a configured runtime, such as Claude CLI, OpenRouter, or the proposed
Codex CLI adapter. Application upgrades replace code
without replacing private workspace data. A friendly distribution might
eventually support commands such as:

```text
rheo init
rheo add telegram
rheo connect tuttle
rheo doctor
rheo start
```

These are product-direction examples, not a committed CLI.

### Hybrid

Some modules or agent sessions run as a hosted service while sensitive or
local-first components remain on the user's machine. A local node makes an
authenticated outbound connection and exposes only explicitly permitted
capabilities. This is likely the most natural hosted relationship with Tuttle.

### Fully hosted

rheoStream operates the application, agent runtime, sources, and storage for the
customer. The hosted service uses service-grade model credentials and strong
workspace isolation. It should use the same domain contracts and MCP semantics as
the self-hosted edition rather than becoming a separate product.

Not every integration must be available in every mode. Capability discovery
should make absence explicit to rheo and to the user.

## Open-source and SaaS posture

The public repository should be a new, clean canonical repository rather than a
fork of MOL or M.O.T. and rather than a thin permanent harness around both legacy
applications. The desired approach is a structured reconstruction:

1. define the new neutral contracts and boundaries;
2. bring over behavioral tests and sanitized fixtures where appropriate;
3. port the smallest working implementation of each capability;
4. compare it against the existing production behavior;
5. switch traffic or workflow incrementally;
6. freeze and eventually archive superseded legacy code.

Temporary adapters to the existing private applications are useful migration
tools. Each should have a clear removal condition so that a shortcut does not
become a public compatibility promise.

The new repository must be sanitized from its beginning. Existing history can
contain secrets, deployment topology, personal lead data, CV material, proposal
content, client information, or generated logs. The old repository should never
simply be made public. Reused code, source adapters, and assets need appropriate
license and attribution review.

### Public repository contents and private workspace contents

| Public repository | Private data, never committed as user content |
| --- | --- |
| Framework/module code, migrations, and schemas | Live databases, database journals, dumps, backups, and exports |
| Reusable domain packs, blank templates, and generic agent instructions | CVs, portfolios containing private material, rates, client lists, personalized prompts, and workspace overrides |
| Synthetic fixtures, demo accounts, and invented example documents | Real leads, applications, proposals, assessments, case records, dossiers, attachments, and correspondence |
| Placeholder environment examples and generic deployment templates | Credentials, OAuth sessions/tokens, private keys, account IDs, and private deployment details |
| Sanitized architecture and contributor documentation | Runtime logs, conversations, memory, embeddings, generated reports, and screenshots containing user data |

This applies equally to the first client, contributors, local installations, and
hosted tenants. Reusable behavior belongs in source; the values supplied by a
particular person or business belong in their private workspace. Public documentation
and examples must not embed real customer identities or depend on private projects.

Project `AGENTS.md` and `CLAUDE.md` files may contain internal build procedures and
are local, ignored files. Keep their detailed guidance and fresh-session handoffs
in the private sibling agent-context directory, with small local entry files where
each coding tool discovers them. Publish general contribution guidance separately
in `CONTRIBUTING.md`. A new clone must not depend on the maintainer's private
instructions to understand or build the public project; local continuity must be
provisioned separately. See [private instructions and new sessions](../workspace-layout.md#private-instructions-and-new-sessions).

### Ignore rules are a second boundary

The default data location is outside Git. For developers who deliberately use
checkout-local state, reserve a private directory such as `/.rheo-local/` and keep
all its profiles, uploads, transcripts, indexes, logs, exports, and backups inside
it. Publish and test the corresponding root `.gitignore` as part of the new
repository's initial scaffold. Modules must use the core's storage API so installing
a module cannot introduce an unignored private output directory.

An illustrative privacy baseline, to combine with normal build/tool ignores:

```gitignore
# Optional checkout-local runtime state; default storage is outside the repo.
/.rheo-local/
/.secrets/

# Local coding-agent instructions/configuration; internal build context is private.
AGENTS.md
AGENTS.override.md
CLAUDE.md
CLAUDE.local.md
.codex/
.claude/
.mcp.json
/rheo.local.*

# Local/deployment environment values; examples contain placeholders only.
.env
.env.*
!.env.example
!.env.*.example

# Database files and their SQLite sidecars, wherever accidentally generated.
*.db
*.db-*
*.sqlite
*.sqlite-*
*.sqlite3
*.sqlite3-*

# Runtime logs. Transcripts and other artifacts belong in the private data root.
*.log
```

Use narrow, explicit paths for runtime artifacts. Do not blanket-ignore all JSON,
CSV, Markdown, PDFs, or a generic `workspaces/` source directory: those can contain
legitimate public code, schemas, and synthetic fixtures. Private dumps and exports
of any format remain inside the private data root. Any new output destination must
pass the storage-boundary check before release.

`.gitignore` affects untracked files; it does not remove already tracked content,
erase Git history, or provide access control. Publication therefore requires a
review of selected files and the new repository's history as well as tested ignore
patterns. [Git's ignore documentation](https://git-scm.com/docs/gitignore).

### Publication and packaging checks

Start the public repository from a reviewed source selection. Do not copy old Git
history, private run directories, or whole production trees. Review documentation,
tests, fixtures, comments, and examples for personal or client content as well as
credentials; secret scanning alone cannot recognize all private business data.

Use local checks before upload and CI as an additional release check. Verify that
representative private paths are ignored and no prohibited runtime paths are
tracked. Build release packages and container images from explicit public source
inputs; Git ignore rules do not automatically control Docker build contexts, package
contents, CI artifacts, or static frontend bundles. Tests use synthetic data and
must not load a developer's real workspace or production database.

Exercise initialization, ingestion, drafting, memory, export, and module removal
against a synthetic workspace, then inspect source changes and packaged artifacts.
Private values must stay in the selected private store and out of browser build
assets, diagnostics, committed examples, and published artifacts. A deliberately
shared report or exported configuration needs its own disclosure choice.

If credentials are accidentally exposed, revoke/rotate them and handle repository
cleanup as a separate incident; adding an ignore rule afterward is insufficient.
[GitHub's sensitive-data removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

These are requirements for the new public rheoStream repository. This idea document
does not establish that the current private legacy repository or its history is
safe to publish.

### License choice

Settled 2026-09-17: **AGPL-3.0**. The `LICENSE` file is the GNU Affero GPL v3
text. SPDX: `AGPL-3.0-only` (this version only, not or-later). Operators who
convey a modified version must meet the applicable Corresponding Source
requirements. If that version supports remote network interaction, they must
offer Corresponding Source to those remote users at no charge.

Considered and not chosen:

- **Apache-2.0**, which would maximize permissive adoption including proprietary
  hosted use of a modified kernel;
- a **dual-license model** pairing a reciprocal community edition with
  commercial licensing — premature; the commercial model remains open.

Tuttle's GPL-3.0 license still reinforces the value of process/API integration
rather than copying its implementation into this core.

## Architectural guardrails

The following constraints should guide work before the final specification. Their
purpose is to preserve future options.

1. **No legacy names in new public contracts.** Do not introduce new `mol`, `mot`,
   `ministry`, or generalized `upwork_*` routes, packages, tables, MCP tools,
   services, environment variables, or user-facing documentation. Historical
   migration notes may identify their sources.
2. **Workspace context enters at the boundary.** HTTP, MCP, jobs, and channel
   sessions resolve a trusted workspace before calling domain services.
3. **No user-selected database paths or tenant identifiers.** Storage routing is
   server-controlled and derived from authenticated context.
4. **Domain services do not depend directly on Claude.** They accept structured
   requests and return structured results. Agent runtimes orchestrate them.
5. **The MCP server calls services, not databases.** MCP is an interface over
   application behavior, not a privileged SQL gateway.
6. **Modules do not write one another's storage.** Cross-domain changes use public
   operations and events.
7. **Storage-specific SQL stays behind repositories or storage adapters.** Where
   behavior differs between SQLite and Postgres, tests define the intended
   contract and the difference is explicit.
8. **Every workspace tracks its composition and schema versions.** Core and module
   versions, configuration, and export/restore paths are product features, not
   emergency scripts.
9. **Identifiers are globally safe.** Durable IDs and event IDs should remain
   unambiguous when data is exported, imported, or synchronized.
10. **Source provenance is first-class.** Never overwrite richer evidence with a
    thinner observation or hide why a field changed.
11. **Side effects are confirmable and auditable.** External actions use explicit
    permissions, idempotency, and durable outcome records.
12. **Long-running work is resumable or observable.** Do not make correctness
    depend on one open HTTP request or one uninterrupted chat turn.
13. **Channels share one authorization model.** Telegram and voice cannot become
    privileged back doors around web or interactive-session policy.
14. **Secrets are scoped to the narrowest component.** Job-source credentials,
    email access, model credentials, and encryption keys do not flow into prompts
    or general tool arguments.
15. **Self-hosted is continuously exercised.** Hosted-only assumptions should not
    accumulate unnoticed while the project still claims portability.
16. **Tuttle remains independently replaceable.** rheoStream depends on an
    integration contract, not on Tuttle's private schema or process internals.
17. **Funnels do not define the core schema.** Source mappings, pipeline presets,
    and domain modules are separate extension points. Job search is optional.
18. **Identity is resolved at the right level.** Delivery deduplication, party
    matching, and opportunity matching use separate rules and workspace scopes.
19. **Configuration changes are versioned.** Pipeline edits and scoring changes
    do not silently rewrite existing records or their interpretation.
20. **Accepted intake and completed outcomes are different.** Durable receipts,
    handoffs, and external actions expose their real processing state.
21. **Derived context inherits access and retention.** Search, memory, exports,
    and queued actions cannot escape source restrictions or revocation.
22. **The core has no profession-specific rules.** The first configuration and
    later domain packs use the same public extension contracts.
23. **Module removal is a supported workflow.** Dependencies, in-flight effects,
    retained records, references, and shared resources have explicit handling.
24. **Shared business records do not require Leads.** Relationship identity and
    specialist facts have defined owners, with access checked at the record level.
25. **Personalization is private data.** Public packs and templates contain reusable
    defaults; individual profiles, overrides, connection details, and runtime state
    stay in authorized local or hosted workspace storage.
26. **Runtime output stays outside the source tree by default.** Ignore rules,
    tracked-file checks, and release-content review also protect the explicit
    checkout-local fallback and packaging paths.

## What rheoStream is not trying to be

At this stage, rheoStream is not:

- an uncontrolled agent that automatically applies for jobs or contacts people;
- a replacement for a complete accounting or tax product;
- a mandate to move every component into one shared database;
- a mandate to split everything into network microservices;
- a blank-sheet rewrite that ignores proven behavior in the current tools;
- a cosmetic rebrand that leaves the old conceptual architecture untouched;
- a permanent compatibility shell around MOL and M.O.T.;
- a form builder, full marketing-automation suite, or multi-buyer lead marketplace;
- a promise of arbitrary hosted code plugins or concurrent offline synchronization;
- an unbounded general assistant without explicit work records, ownership, or
  action policy.

## Principal risks

### Rebuilding too much at once

A clean repository can turn into an endless rewrite if behavior is not migrated in
vertical slices. Existing tests and production comparisons must anchor the work.

### Preserving too much legacy structure

Putting a new harness in front of old APIs is fast, but it can preserve old names,
data assumptions, authentication, and direct coupling indefinitely. Temporary
adapters need explicit retirement criteria.

### Generalizing only the labels

Renaming jobs to leads while preserving candidate profiles, application stages,
and job-shaped IDs excludes business users. Validate two materially different
custom funnels alongside job search before freezing the common contract.

### Building an automation platform before a useful workflow

Every custom requirement can tempt a new abstraction. Begin with versioned fields,
stages, qualification rules, and a few handoff actions. Defer arbitrary code,
complex workflow graphs, and broad CRM features until concrete use requires them.

### Modularity that ends at installation

Optional menu items do not establish removable modules. Hidden data dependencies,
queued jobs, schema coupling, or hard-coded tool registration can make a supposedly
optional module permanent. Prove enable, disable, removal, and restoration as part
of the extension contract, including another workspace that still uses the module.

### Flattening specialist work into generic tasks

Similar infrastructure needs do not imply interchangeable professional procedures.
Keep specialist evidence, rules, and outcomes in their domain modules. Validate
the framework with a small example module while building the first useful workflow;
do not attempt complete HR, regulatory, and sales products in the first release.

### Premature storage commitment

Hard-coding either one global SQLite file or shared Postgres assumptions into
domain services could make the opposite deployment costly. Workspace context and
storage boundaries need to exist before the final database choice.

### Agent overreach

A useful conversational interface can obscure where authority resides. Without
structured tools, confirmation classes, and audit records, convenience can become
unsafe automation.

### Source fragility and licensing

Job boards change schemas, impose quotas, block automation, and apply distinct
redistribution terms. Source adapters must fail independently, preserve provider
identity, and be enableable by deployment and license context.

### Dual-mode complexity

Supporting both self-hosted and SaaS editions can double operational paths if their
contracts diverge. The system should share domain behavior while allowing storage,
credentials, and runtime implementations to differ.

### Encrypted database operations

Per-workspace encrypted SQLite improves isolation but introduces key lifecycle,
backup, migration, and replica-coordination risks. “Encrypted” must be defined by
a threat model and recovery procedure, not just by enabling one library option.

## Open questions for the eventual specification

These questions are intentionally not resolved by this idea document:

1. What is the exact workspace, membership, role, and invitation model?
2. Which official modules use per-workspace SQLite, shared Postgres/RLS, or a
   hybrid control-plane/data-plane design in the first release?
3. What encryption threat model and key-management system are required for the
   hosted edition?
4. Which first release slice proves custom-funnel intake and optional job search,
   and how much Current and Recallatron support belongs in it?
5. Which current proposal-generation capabilities belong in the optional
   job-search workflow's initial release?
6. What are the exact event schemas, ordering rules, retry limits, and replay
   contracts for the proposed outbox and durable operations model?
7. What does the local node protocol permit, and how are its capabilities revoked?
8. How much Tuttle integration can be built against supported upstream interfaces
   rather than database internals?
9. Which Jobvis-inspired providers can legally and economically be enabled in a
   hosted SaaS?
10. Which of Claude CLI, OpenRouter, and the proposed Codex CLI adapter ships first,
    which API/SDK runtime is the supported hosted default, and what capability,
    isolation, failure, and continuation checks must each adapter pass?
11. What is the confirmation policy for applications, messages, submissions,
    destructive changes, and financial actions?
12. What data is sent to model providers, and what redaction, retention, and
    user-control options are required?
13. How will workspace export/import, schema migration, and disaster recovery be
    tested?
14. Which open-source license and contributor model best support both community
    trust and a sustainable hosted service?
15. What is the minimum useful direct-voice experience, and should voice wait
    until the text interaction and permission model are stable?
16. Which generic intake transports and two unrelated custom-funnel examples will
    validate the first contract without requiring product-specific adapters?
17. What can users configure in pipeline presets, and how are existing records
    migrated when fields, stages, or qualification rules change?
18. What is the minimum relationships-module contract, which fields can an external
    CRM own, and what evidence permits an automatic match or reversible merge?
19. Which permission purposes, recipient scopes, suppression records, retention
    limits, and external correction/deletion flows must the first release support?
20. What belongs in the minimum core, which capabilities become shared business
    modules, and which dependencies does the first configuration require?
21. What are the module manifest, capability compatibility, registration, package
    provenance, and contract-test requirements for first-party and contributed code?
22. How do domain packs compose and upgrade without overwriting workspace choices,
    and how are required dependencies explained during enablement and removal?
23. How are detached records read and restored, migrations recovered, and jobs
    reconciled when a module is removed from one workspace or from the host?
24. What private data/configuration paths, member-level scopes, secret stores,
    packaging allowlists, and publication checks are required in the first release?

## Architecture acceptance scenarios

These are design checks for the later specification and behavioral tests, not a
claim that the current applications implement the architecture:

| Scenario | Required behavior |
| --- | --- |
| Fresh public clone | Build and synthetic demo work without any real user's files, credentials, infrastructure, or workspace database |
| Local private workspace | Initialization and normal workflows write user content only to approved private storage and leave tracked source/templates unchanged |
| Checkout-local fallback | Private configuration, uploads, memory, exports, databases and sidecars are ignored; public schemas and synthetic fixtures remain trackable |
| Public pack export | A reviewable reusable pack omits personal overrides, real identities, connector details, and secret references |
| Hosted workspace data | Runtime configuration and attachments are access-controlled by workspace/member permissions and absent from source, images, and frontend build assets |
| Publication review | Selected files and new Git history are reviewed; private paths, package contents, and synthetic-fixture checks pass before publication |
| Runtime choice | The same permitted workflow can select Claude CLI or OpenRouter through private configuration without changing a domain module; Codex CLI must pass the same contract checks before being declared supported |
| Runtime capability mismatch | A model without required tool/schema support, or a CLI without enforceable isolation, is rejected before the workflow starts |
| Runtime switch or retry | Authorized context carries forward, native sessions remain isolated, and completed actions are not replayed; provider fallback stays within the approved policy |
| Headless approval or failure | An unavailable CLI, expired credential, denied tool, timeout, or incomplete stream yields a clear non-success state instead of a hung job or a false completion |
| Business-only installation | A user connects a custom form and works a pipeline with no job boards, candidate profile, or application tools configured |
| Client work without Leads | A consultant uses relationships, Current, and a specialist module without opportunity management or job-search dependencies |
| A new specialist module | A small assessment example registers records, tools, UI contributions, jobs, migrations, and exports through public extension points; no profession switch or private-table access is added to the core |
| Combined domain packs | A workspace combines freelance and sales defaults without duplicate records or silent configuration replacement |
| Module disabled during execution | New effects are blocked, running work is reconciled, cached tools are denied, and retained records remain exportable |
| Module removal with dependencies | Required dependents block or join an explicit removal plan; optional references show archived/unavailable state and unrelated work survives |
| Removal from one workspace | Other workspaces keep their module and jobs; shared host code is retained while still needed |
| Module reinstalled or upgraded | Versioned data/configuration restore under compatible contracts without repeating completed external actions |
| Two different custom funnels | An assessment and an event referral use independent mappings and the same core; neither requires product names or provider-specific columns in it |
| Intake without a model session | A valid webhook is durably accepted and processed while rheo is offline |
| Same event retried concurrently | One observation and one processing effect; conflicting content under the same source event ID is surfaced |
| One person, several interactions | A download and consultation request remain distinct observations; a party can be linked without automatically merging or duplicating opportunities |
| Late evidence or scoring result | Newer facts, explicit corrections, and user-owned state survive; stale derived results are identified |
| Job-search regression | Email, search result, and full posting enrich one opportunity while preserving proposals and user decisions |
| Preset edited during active work | Existing opportunities retain their declared schema and stage meaning until an explicit migration |
| Crash during handoff | Retry recovers the same created work; an uncertain external outcome stays visible and is reconciled |
| Permissions or contact purpose withdrawn | Queued effects recheck policy; retained memory does not bypass the restriction |
| Source text requests a tool action | The text remains evidence and cannot authorize an external action |
| Wrong workspace or channel audience | Intake, tools, jobs, and retrieval reject access even when a caller knows a valid record or operation ID |
| Export and restore | Selected modules, configuration versions, relationships, and evidence remain interpretable; pending external actions stay paused until reauthorized |

Build the first client's reference configuration and validate its extension boundaries
with a small synthetic specialist module. Prove generic business-funnel intake and
the existing job-search enrichment behavior, then add a second unrelated funnel
without changing the core schema. Exercise one optional handoff, a restricted memory
lookup, and the specialist module's disable/remove/restore lifecycle with Leads
absent. This validates the framework without committing to full products for other
professions or every listed connector and deployment mode.

## Idea-stage success criterion

The direction is working when it is credible that one person can install rheo
Stream, connect their own funnels or selected opportunity sources, choose a useful
pipeline, speak or type to rheo, and develop opportunities while retaining ownership
of their data. They can optionally move chosen work into Current, retrieve permitted
context from Recallatron, or connect an external CRM or Tuttle.

A business user must get that experience without job search. A job-search user
must retain the depth of enrichment and application support that made MOL useful.
An unrelated user's custom funnel should fit through a documented intake and
configuration contract without a fork of the core.

The broader framework succeeds when a second profession can add its own module or
pack through those same contracts, operate without irrelevant modules, and remove
a module without losing unrelated records or breaking the rest of its workspace.
Adding specialist behavior may require new module code; it should not require
rewriting the shared infrastructure or changing the first client's configuration.

That first configuration's public version must be a reusable, synthetic example.
The real client's profiles, credentials, and work remain private, under the same
storage and publication rules that apply to every later user.

The same contracts should allow a managed rheoStream service to provide that
experience to isolated workspaces without rewriting the domains or depending on
consumer CLI subscriptions. Contributors should be able to understand where a new
source, channel, tool, or integration belongs without learning legacy product
history.

## Current decision ledger

### Settled direction

- The umbrella name is **rheoStream** and the project domain is `rheo.stream`.
- The agent is **rheo**.
- Opportunity discovery and qualification is **Leads**.
- Work in motion is **Current**.
- Durable memory is **Recallatron**.
- The goal is both a public open-source release and a possible hosted SaaS.
- New public contracts should not retain MOL/M.O.T. naming.
- Jobvis's useful source coverage should inform the next Leads version.
- Upwork email polling should become deterministic client-side automation rather
  than a Claude routine.
- Copied Upwork search results and full postings should enrich matching thin email
  leads rather than create disconnected duplicates.
- Tuttle is best approached as an independently installed integration and a
  potential upstream contribution target.
- A new clean canonical repository is preferable to making either current repo the
  permanent center of the new architecture.
- Existing code should be ported selectively and incrementally, not discarded and
  not preserved wholesale.
- Leads includes users' own custom business funnels, not only job search or
  integrations with the first client's existing projects.
- Job search is optional; business users must not need candidate or application
  configuration to use Leads.
- Freelance-development and software-business needs define the first
  client configuration; the framework must support other professional domains.
- Adding and removing modules is a product requirement, including their data and
  operational lifecycle, rather than only hiding features in the interface.
- Leads, Current, and Recallatron are the first official modules, not a permanently
  fixed set or a mandatory sequence for every user.
- The repository is public code and reusable definitions; every user's personal
  data, credentials, private configuration, and operational records stay private.
- The public repository is licensed **AGPL-3.0**. The commercial model is still
  open.
- Local use is a primary deployment mode. Hosted users keep private data in their
  authorized databases and workspace storage, with secrets managed separately.
- AI runtime selection is configurable. Claude CLI (`claude -p`) and OpenRouter
  are required execution options; Codex CLI (`codex exec`) is an additional target
  whose integration must be validated. Domain modules and channels stay independent
  of the selected runtime and model. Their rollout order is settled below.
- Ignore rules accompany a clean-publication process; they do not make existing
  tracked data, Git history, or packaged artifacts safe to publish by themselves.

Settled by the [requirements and scope document](../requirements/requirements-and-scope.md),
which records the rationale for each:

- Postgres is the sole reference storage backend for the first release, with one
  database or schema per workspace and a small shared control plane for accounts
  and the workspace registry. Billing belongs to the hosted edition and joins the
  control plane in that phase, not in the first release. Per-workspace SQLite
  becomes a contract goal for a possible later local-first edition, not
  first-release code.
- The implementation stack is a Python core (FastAPI domain services) plus a
  Next.js web interface, chosen after an explicit comparison with a single
  TypeScript monolith.
- Recallatron is the first real module after the walking skeleton, then the
  opportunity core, then the optional job-search workflow.
- `ClaudeCliRuntime` ships first; the OpenRouter adapter is the required second
  implementation for v1.0; Codex CLI remains a validation target. See the recorded
  change of direction below.
- The full module manifest and lifecycle contract are specified, while the first
  release implements install and enable only. Disable, remove, purge, and restore
  are a defined later milestone.
- GitHub OAuth is the first login provider, behind a pluggable identity-provider
  boundary. Command-line and MCP access uses local tokens.
- URL topology is configuration. Single-host path mode is the default for
  self-hosters; subdomain-per-module is supported and is what the reference
  deployment uses. See the recorded change of direction below.
- The web interface is ported from the two predecessor applications rather than
  rewritten, under a bounded port inventory and the publication rules.
- The memory port includes a real retrieval-layer rework, since the predecessor's
  memory layer uses `sqlite-vec` and FTS5 rather than pgvector. It is contained
  behind the retrieval adapter.
- The reference instance is a single-server container deployment behind a reverse
  proxy, so the web interface must not depend on hosting-platform-only features.
- The workspace, membership, role, first-release-slice, confirmation-policy, and
  relationships-contract questions are answered there, and a record-level deletion
  path for observations, parties, and opportunities is proposed there as the
  deletion half of question 19. These five (R1 to R5 in that document) were
  proposals taken on the maintainer's behalf and were ratified by the maintainer's
  merge of the pull request that carried them (PR #5, 2026-09-09). Every other open
  question below carries a recorded disposition.

Settled by the [architecture specification](../architecture/README.md), as decisions
A1 to A17 with rationale in the document each names; accepted by the maintainer's
review and merge of the pull request that carried them (PR #7, 2026-09-09):

- The per-workspace storage unit is a database, not a schema; one Postgres schema
  per module inside it ([storage](../architecture/storage-and-workspaces.md)).
- Identifiers are UUID version 7 with typed record references
  ([identifiers](../architecture/identifiers.md)); this closes guardrail 9.
- Configuration precedence and the operator policy floor, and the secret store (a
  file-backed store with an environment backend, behind one protocol) are chosen
  ([storage](../architecture/storage-and-workspaces.md)); this closes the parts of
  question 24 the requirements left to the specification.
- Module manifest and registration contract version 1, with the full lifecycle on
  paper ([module contract](../architecture/module-contract.md)); this closes
  question 21 for the first release.
- Event schemas, ordering, retry limits, replay, and the intake envelope
  ([intake and events](../architecture/intake-and-events.md)); this closes question 6.
- Runtime contract version 1, the MCP facade conventions, and the redaction contract
  ([runtime and MCP](../architecture/runtime-and-mcp.md)); this closes question 12
  and the capability-check half of question 10.
- The final confirmation rules and the pipeline preset limits and migration operation
  ([confirmation and safety](../architecture/confirmation-and-safety.md)); this closes
  question 11's remainder and question 17.
- Identity, sessions, tokens, and the URL topology as configuration
  ([identity and topology](../architecture/identity-and-topology.md)).
- The relationships module's data model, with a merged party kept as an alias so
  that references are never rewritten and unmerge restores from the merge record
  ([relationships](../architecture/relationships.md)); this gives R4's reversibility
  its representation.
- The memory module's data model, with an explicit audience and purpose set on every
  memory, intersection on derivation, and one invalidation rule for deletion,
  correction, and supersession ([memory](../architecture/memory.md)).

### Recorded changes of direction

Later documents should not reverse settled direction without recording the reason.
The following changes are recorded here.

1. **Runtime rollout order.** This document states that Claude CLI and OpenRouter
   execution "are explicit requirements." That is softened to a rollout order:
   `ClaudeCliRuntime` ships first and the OpenRouter adapter is required before
   v1.0. Reason: the requirement existed to stop the architecture assuming one
   runtime, and requiring the structurally different second adapter before v1.0
   serves that purpose completely. Read as a first-slice requirement it would delay
   every domain decision behind two runtime integrations for no design benefit.
   Guardrail 4 is unchanged and is enforced by the runtime contract from the first
   adapter.
2. **Module subdomains.** This document states that "individual module subdomains
   are unnecessary unless the modules later become independently deployed public
   services." That is overridden. Subdomain-per-module is supported and is what the
   reference deployment uses (`leads.`, `current.`, and so on, alongside `api.`,
   `mcp.`, and `docs.`, with the application shell on `circuit.`, the login and OAuth
   callback on `auth.`, and `tuttle.` reserved for the integration surface only).
   Reason: the maintainer wants module boundaries
   visible in the address bar of the instance used daily, and one application
   serving every host through host-based routing costs a routing table rather than a
   deployment per module. Identity sits on its own host so the registered callback
   URL does not move when the shell changes and a later second front end can share
   one identity endpoint. Constraints: neither modules nor identity are separately
   deployed, `auth.` being a route boundary rather than a second service; in
   subdomain mode one session covers the shell host, the identity host and the module
   hosts the application serves, and hosts it does not serve must not receive the
   session cookie; in single-host path mode there is no identity host and the callback
   is a path on the single origin; single-host path mode remains the default and stays
   continuously exercised under guardrail 15; and a hosted multi-tenant edition must
   choose deliberately between module subdomains and per-tenant subdomains, because
   they compete for the same namespace level and a wildcard certificate covers one
   level only.
3. **The shell host is `circuit.`, not `app.`** This document's endpoint list named
   `app.rheo.stream` as the hosted application. Renamed. Reason: under the module
   subdomains above, `app.` is ambiguous, because `leads.` and `current.` are equally
   "the application"; `circuit.` names the surface that carries the shell and the
   workspace switcher specifically. An electrical circuit and a water circuit are both
   ordinary usage, so the name reads in the same register as `rheo.stream` and
   `current.`, and a circuit is also a route made in rounds, which is what the shell is
   for. Nothing else changes: the shell host is still one host among the several a
   single application serves through host-based routing, and in single-host path mode
   it does not exist at all. The name is configuration like every other host here.
4. **Ambient automatic memory, and what "entities and relationships" means.**
   Release-one Recallatron includes both manual memory and ambient automatic creation
   from explicitly stated eligible transcript evidence, without a remember command or
   per-note administered review. The previous manual-only restriction is superseded to
   preserve the intended across-session memory behavior. "Entities and relationships"
   means global UUID entity identity plus memory-to-entity mentions and
   memory-to-authoritative-record provenance/about links. It does not ratify generic
   entity attributes, entity-to-entity edges/traversal, confirmation or merge. Readable
   retained non-erasure history remains part of A17; erased content remains absent.
5. **Entity identity and creation.** Entity creation is independent and non-probing.
   Same-name entities are allowed; no global normalized-name uniqueness constraint
   exists. Existing entity selection requires an authorized canonical reference. A
   no-backing-reference entity is removed when its last mention is deleted; referenced
   domain entities are retained under their own access rules.
6. **Erasure, scheduled expiry, and marked ancestry.** User erasure follows dependent
   lineage. Scheduled expiry of a superseded predecessor preserves a fresh replacement
   reached only by marked ancestry, with its own retention clock. Server-owned immutable
   ancestry and separately copied actual access restrictions preserve this behavior
   after physical removal and export/restore. The core deletion record adds cause and
   retained_successor_ref as content-free validation metadata; its outcome still has
   only the four counters, and caller output does not disclose cross-audience counts. No
   successor removal revives a predecessor.
7. **The retention baseline, and what stays unapproved.** Criterion 30's explicit
   bounded positive memory retention and scheduled sweep are release-one baseline
   behavior; no keep-forever sentinel is authorized. Advanced retention strategies
   remain unapproved. Migration preserves original clocks and forecasts expiry rather
   than refreshing dates. (Narrowed the same day by entry 13 below: the sweep and the
   bounded window stay as baseline mechanism, and whether a workspace expires by age at
   all became its own explicit off-by-default setting. "No keep-forever sentinel" is
   unchanged — unboundedness is that gate's state, never a magic number on the window.)
8. **Bound purpose, compare-and-set, and trusted source identity.** Bound runtime/job
   purpose is authoritative and cannot be overridden or written wider. Unbound
   authenticated humans/services have no purpose gate. Correct and supersede use
   expected_revision compare-and-set. Namespaced opaque external source identity and
   payload-free receipts provide idempotent ingest without name probing, canonical-ref
   misuse or replay resurrection.
9. **The automatic-memory carrier, and what it does not activate.** A dedicated
   automatic-memory carrier follows retrieval and precedes migration/cutover. It
   delivers both a Rheo runtime typed evidence handoff and a
   machine-and-project-enrolled local Claude Code CLI/Desktop Code bridge, with bounded
   Stop capture and SessionEnd finalization, durable sanitation/digestion/extraction,
   immutable authority/provenance and replay suppression. M.O.T. is a behavior
   reference, not schema or threshold authority. Internal digestion does not activate a
   conversation ledger/archive, generic sessionization, profiles,
   candidate-confirmation UI, no-query bundles or proactive memory surfacing.
   Topic-thread and procedural-note handling keep their original explicit deferrals; the
   proposed discovery carriers remain unapproved.
10. **None of this waives migration criterion 32.** These boundaries do not waive
    migration criterion 32. Unsupported or lossy source mappings hold cutover until
    every source record has a valid destination, an exact public criterion change is
    ratified, or the maintainer declines cutover. The supported-client manifest is a
    separate operational gate and never a data-parity waiver.
11. **The owned-delete pair is a field of `RecordType`.** Module contract version 1, as
    PR #7 settled it, gave the deletion coordinator no typed way to reach a module's own
    delete: until a real owner existed, only a test harness registered a declaration, by
    hand. `RecordType` now carries `authorize_delete` and `delete_owned`, whole or
    absent, valid only on a `deletable` type, and the loader registers the pair on the
    process-global owned-delete registry unconditionally. Reason: "a record type appears
    in exactly one manifest" is already the invariant an owned-delete registry needs, so
    hanging the pair off the type makes "exactly one declaration owns a record type"
    structural rather than checked. Ratified by the maintainer on 2026-09-21, reviewed
    at run 1a1's checkpoint 13, and written into the
    [module contract](../architecture/module-contract.md#owned-record-types) rather than
    left to a build-stage commit message. Constraint: this is the narrow owned delete
    and its evidence, not the full A14 cascade, whose job, action, transcript and
    held-export legs remain phase three's along with criterion 65.
12. **`Disposition` is a required keyword on the delete authorizer and the participant
    handler.** Both callables now take a keyword-only `disposition` with no default,
    valued `user_erasure` or `retention_expiry` — the same vocabulary the deletion
    ledger's `cause` column records. Reason: the two dispositions traverse different
    edges and carry different authority, both differences live inside the owner's own
    callables, and neither can read the sealed capability the core verified, so an owner
    cannot infer which one is running and has to be told. No default, because a caller
    that did not say has not decided. Ratified by the maintainer on 2026-09-21, reviewed
    at run 1a1's checkpoint 14. Constraint: it is one axis with two values carried on
    the existing pair, not a second registration an owner could forget half of.
13. **Memory retention is opt-in, and age-based expiry is off by default.** The
    architecture specification settled by PR #7 made `recallatron.retention.days`
    mandatory for every workspace, with a package default of 365 days and the explicit
    statement that none inherits indefinite retention — so every memory was deleted by a
    daily sweep once older than the setting, with no opt-out. That is superseded for
    retention alone. A second setting, `recallatron.retention.expire_by_age`, is written
    at enable with a package default of false, and the window is read only while it is
    on. Reason: the authority being restored is FR 29's own text, "Memory retention is
    explicit per workspace. Nothing is retained permanently by default" — a requirement
    against an inherited retention posture, which making both settings explicit stored
    rows satisfies, and which does not forbid a workspace choosing to keep its memories.
    Recallatron's premise is that a memory is corrected or superseded, not expired for
    having got old, and permanent durable memory is the product's differentiator; the
    mandatory reading was an architecture-stage over-tightening that travelled through
    one pull request carrying seventeen unrelated lettered decisions. Ratified by the
    maintainer on 2026-09-21, reviewed at run 1a1's checkpoint 16. Constraints: the
    sweep mechanism stays, as opt-in infrastructure rather than default behavior; the
    window keeps its hard bounds of 1 to 3650 days and gains no indefinite sentinel;
    `member`-audience memories stay outside the age mechanism in either gate state; and
    category-scoped retention — expiring one class of subject matter and not another —
    is explicitly deferred, because the schema carries no taxonomy to scope such a
    policy against. Criterion 30 and its FR 29 trace row are amended to match, and the
    recorded change of direction is repeated in
    [memory](../architecture/memory.md#retention-fr-29-criterion-30).
14. **The theme is a themable contract, not one theme.** Requirements decision D8, the
    phase-two scope in the build plan, and criteria 34 and 37 specified the unified interface's
    theme as singular: "one token set, one theme." That is superseded. A theme is now a small
    declarative data file of values for a versioned token contract that sets color, type and
    shape and nothing else; it is not arbitrary CSS. The approval and confirmation surface draws
    from a reserved token subset that only built-in themes may set, so a third-party theme
    cannot change how a destructive or financial confirmation reads. Reason: module screens are
    written against whatever tokens the shell promises, so the token set is an interface with
    the same standing as the module and runtime contracts and has to be versioned like them, and
    a user-authored, shareable theme is only safe to allow if the confirmation chrome sits
    outside its reach. Directed by the maintainer on 2026-09-12 and recorded in issue #40, which
    also set the timing (phase two as planned, not pulled forward). Constraints: the contract
    stays small, because every token added is one every shared theme must supply; phase two
    ships the contract and one seed built-in theme, which a later UI phase replaces or extends
    with its own built-ins: two to start, both working names, the current rheo.stream site look
    (dark only, seeded by this phase's theme) and a Novadiem theme with a light and a dark mode,
    each mode its own theme file on contract v1, grouped behind a user-facing switch by later
    registry work rather than a contract change; theme selection and user-theme import are that
    later phase's work. Specified in
    [theme contract](../architecture/theme-contract.md) as architecture decision A18; D8, the
    phase-two scope bullet and criteria 34 and 37 carry an amendment note pointing to the theme
    contract, and D8's note also points here.

### Preferred but still to validate

- One goal-oriented rheo MCP façade is the initial agent boundary.
- `claude -p` is a useful first local adapter. An OpenRouter adapter supplies its
  own agent loop; an API/agent SDK is appropriate for hosted operation. Validate
  Codex's headless adapter through the common runtime contract. The rollout order
  is no longer open; it is settled above and recorded as a change of direction.
- Workspace is the tenancy and portability boundary.
- Per-workspace encrypted SQLite remains a credible option for the hosted edition
  and for a local-first edition, especially for local-first continuity and Tuttle
  compatibility. It is not a first-release option: the first release settled on
  Postgres and made per-workspace SQLite a contract goal, with no implementation
  shipping.
- A hybrid local-node design is the likely bridge between hosted rheo sessions and
  local Tuttle data.
- A clean modular monolith is a better beginning than premature microservices.
- A small framework core supports connectors, versioned workflow presets, domain
  modules, and composable domain packs through documented extension contracts.
- Signals, parties, opportunities, and qualifications are separate concepts. A
  reusable relationships module owns shared party identity independently of Leads;
  specialist modules own their own participation, facts, and record permissions.
- Trusted modules begin as registered packages in a modular monolith, with host
  code availability separate from per-workspace activation; arbitrary hosted code
  uploads and runtime hot-loading remain deferred.
- Module lifecycle contracts cover dependency checks, version compatibility,
  configuration, migrations, removal, retained exports, and restoration.
- A private data root outside the source checkout is the default; a reserved,
  ignored checkout-local directory supports development when explicitly selected.
- The development workspace is an unversioned parent containing the public Git
  checkout and private siblings. Explicit local configuration connects them; build
  and publication roots remain inside the public checkout.
- Generic authenticated intake, durable receipts, and explicit routing allow users
  to connect their own funnels without a core adapter for each product.
- Transactional outbox delivery, idempotent consumers, and tracked handoffs provide
  crash recovery without requiring event sourcing or a distributed broker.
- Current is an optional destination; a referral or external CRM handoff can also
  complete an opportunity workflow.
- One reference storage implementation and one authoritative writer location per
  workspace/domain should precede dual-backend support or offline synchronization.

### Open decisions

- Storage topology for the hosted edition. The first release is settled above.
- Encryption implementation and key management.
- Commercial model (the license itself is AGPL-3.0 as of 2026-09-17).
- Detailed domain schemas, events, API, and MCP contracts beyond the first release.
  The first release's are settled by the architecture specification above.
- Domain-pack composition and module lifecycle acceptance criteria beyond install
  and enable. The module manifest, the core/module boundary for the first release,
  and the lifecycle contract on paper are settled by the architecture specification
  above; the relationships-module contract is settled by the requirements.
- The contact-purpose and retention contracts beyond the first release, and the
  external correction flow. Pipeline configuration limits and version migration
  are settled by the architecture specification above; identity resolution rules
  and data ownership between rheoStream and an external CRM are settled by the
  requirements.
- Reusable-pack export rules and the packaging-allowlist detail. Private storage
  paths, configuration precedence, and the secret store are settled by the
  architecture specification above; the publication checks are first-release
  requirements.
- Voice and Telegram providers and operational details.
- Depth and timing of the Tuttle integration.

## Next documents

This idea document should be reviewed and corrected first. Once it reflects the
intended product, it can become the input to:

1. a requirements and scope document;
2. a domain and systems architecture specification;
3. security, tenancy, and data-lifecycle specifications;
4. migration inventories for MOL, M.O.T., Recallatron, and Rheo-bot;
5. a phased build and release plan with explicit acceptance criteria.

Those later documents should make decisions that this one deliberately leaves
open. They should not quietly reverse the settled direction without recording the
reason.

## Reference inputs

These materials informed the idea and should be revisited during requirements and
specification work:

- [Build Your Own Voice Agent](https://jamwithai.substack.com/p/build-your-own-voice-agent)
  — the original voice-agent exploration that prompted this direction.
- [Tuttle](https://github.com/tuttle-dev/tuttle) — local-first freelance business
  administration and a possible external integration/contribution target.
- [Observable Job Agent (Jobvis)](https://github.com/jamwithai/observable-job-agent)
  — a reference for observable job search and source adapters.
- [Claude Code programmatic usage](https://code.claude.com/docs/en/headless)
  — print mode, structured output, streaming, and explicit session resumption.
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
  — `codex exec`, JSONL events, structured final output, and session resumption.
- [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling)
  and [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
  — client-owned tool execution and configurable model/provider constraints.
- [OpenWeb Ninja JSearch](https://www.openwebninja.com/api/jsearch),
  [Adzuna developer API](https://developer.adzuna.com/), and
  [Remotive Jobs API](https://remotive.com/remote-jobs/api) — candidate Leads
  providers whose current plans, attribution rules, and hosted-use terms require
  review before public release.
