# System overview

**Part of:** the [architecture specification](README.md). This document gives the shape of the
whole and the decision records for the stack (D3) and the modular monolith. The other documents
carry the contracts.
**Decision:** A12 (process topology). See the [decision list](README.md#architecture-decisions).

## What the system is

rheoStream is a **modular monolith with a durable worker**, as the idea document prefers. One
Python application holds the framework core and every installed module in one process image.
Three surfaces call the same application services: the web interface, the MCP facade, and intake.
Background effects run in a worker that reads persisted work records from each workspace's
database. There is no message broker, no distributed workflow engine, and no event-sourced
authoritative model. Ordinary tables, an outbox, a job table, and an audit table are enough for
the requirements as written, and the [intake and events](intake-and-events.md) document names the
requirement that forces each of them.

```text
  web browser            claude / MCP client         external funnel / CSV / person
       │                        │                              │
  apps/web (Next.js)      apps/mcp (facade)             connectors (transports)
       │  internal API           │  service calls              │  leads.intake service
       └────────────────────────┼─────────────────────────────┘
                                 │
                    packages/core: boundary → WorkspaceContext
                                 │
                    service registry (operations, classes, audit, outbox)
                                 │
              modules: recallatron · relationships · leads · (current, later)
                                 │
                 one Postgres database per workspace  +  control plane
                                 │
                        apps/worker: jobs, event delivery, schedules
```

## Processes

Release one deploys four containers plus the operator's reverse proxy. The core and the worker
share one image.

| Process | Directory | Serves | Notes |
| --- | --- | --- | --- |
| `core` | `apps/core` | HTTP API on the `api` surface, MCP on the `mcp` surface, identity endpoints under `/auth/*` on every application host, the internal API for the web tier | FastAPI under uvicorn. Holds every module's Python package. |
| `worker` | `apps/worker` | Nothing over HTTP | Same image as `core`, worker entry point. Visits only the workspaces the control plane's due-work index (`control.workspace_work_due`) reports as due, never every active workspace in turn, and serves their `core.job` and `core.event_delivery` tables ([jobs and the worker](intake-and-events.md#jobs-and-the-worker-fr-16)). May run inside the `core` process in development (`--with-worker`). |
| `web` | `apps/web` | The application shell, the workspace switcher, and every module's screens | Next.js under `next start`. No database access; every read and write goes to `core`'s internal API. No platform-only feature (FR 48, criterion 23). |
| `postgres` | `deploy/` | The control-plane database and every workspace database | A standard image with pgvector and `pg_trgm` available (requirements assumption). |
| reverse proxy | operator-provided | TLS, host routing | Generic: any proxy that can route by host and path and terminate a wildcard certificate. `deploy/` ships a placeholder configuration, never a product name (D10). |

The proxy's routing table is the same in both topologies ([identity and topology](identity-and-topology.md#the-routing-table)):
`/auth/*`, `/api/*`, and `/mcp` go to `core`; everything else on an application host goes to
`web`. The web tier reaches `core` over the container network at `RHEO_CORE_INTERNAL_URL`, a
second listener the proxy never exposes, forwarding the session secret in a header the public
listener ignores ([the internal API's trust boundary](identity-and-topology.md#sessions-and-cookies));
the browser never calls the `api` host for the first-party interface, which is why release one
needs no CORS allowlist.

`apps/cli` is the `rheo` operator command (workspace creation and repair, member addition, token
issuance on a headless install, migrate, export, restore, doctor). It runs in the `core` image and
talks to the control plane and the workspace databases directly through the same core packages;
it is the only surface with the `operator` actor kind.

## The workspace context

Every entry point resolves a `WorkspaceContext` before any service is called (FR 1, guardrail 2),
and no service accepts a workspace or actor any other way (FR 2, FR 23).

| Field | Type | Source |
| --- | --- | --- |
| `workspace_id` | uuid | web: the session's `active_workspace_id`; MCP and CLI: the token's workspace; job: the job row's database; intake: the connection's workspace |
| `actor` | `Actor(kind, id)` | `kind` in `account`, `token`, `operator`, `system`, `connection`; `id` is the account, token, or connection id, or null for `system` |
| `role` | `owner`, `member`, `operator`, or `service` | from `control.membership` for an account or a token's account; `operator` for the CLI; `service` for a `connection` actor and a `system` actor |
| `entry` | `web`, `api`, `mcp`, `cli`, `job`, `intake`, `channel` | which boundary built the context |
| `audience` | `Audience(kind, id)` | who may see outputs: `session` (id: the account), `token` (id: the token; its account is the person behind it), `job` (the originating operation's audience, copied onto the job when an operation enqueues it; a scheduled job has none), or `channel:<conversation>`. The memory module maps this to a memory's audience ([memory](memory.md#audience-and-purposes-fr-27-criteria-27-and-28)). |
| `operation_set` | frozen set of operation names, or `all` | from the token's snapshot rows; `all` for a web session and the operator; for a `connection` actor the operations its module's `connector_bindings` name for that transport; for a `system` actor at `entry = job` the operations whose declared roles include `service` |
| `enabled_modules` | frozen set of module ids | from `core.module_state` for this workspace |
| `request_id` | uuid | for logs and the audit record |
| `principal` | `AuthenticatedPrincipal(account_id, bound_purpose)` | server-resolved, never from a request: the account the boundary verified (null for the operator), and the purpose it is bound to (from `access_token.purpose` for a token, from the stored job or approval purpose on a rebuild, null when unbound) |

WorkspaceContext carries a server-resolved frozen principal containing the authenticated account,
when one exists, and the bound purpose, when one exists. Only boundary factories construct it.
Session/human/service browsing without a bound purpose has no memory-purpose gate; runtime tokens
and purpose-bound jobs must carry a valid purpose and cannot override it through operation
arguments.

The class lives in `packages/core/boundary/` and only that package's factory functions construct
it; a test asserts no module or app package calls the constructor. Boundary adapters are the four
places FR 1 names: `apps/core` HTTP middleware, the MCP facade's session resolver, the worker's
job loader, and the intake receiver. Channels (phase six) add a fifth.

A context whose workspace is not `active` in the registry, whose membership no longer exists, or
whose token is revoked or expired, is refused at the boundary with a distinct non-success state
(criterion 9) and nothing downstream runs.

**Actors that are not people.** A `connection` actor (the intake receiver, after the signature
verified) carries `role = service` and an operation set of exactly the operations the Leads
manifest binds to that transport (`leads.intake.accept_delivery` for the webhook), so the
registry's set check and role check pass for that call and fail for anything else. A `system`
actor (the worker running a job or delivering an event) carries `role = service` and the set of
operations whose declaration lists `service` among its roles; every operation a consumer or job
handler calls (`relationships.party.resolve_or_create`, `leads.opportunity.create_from_observation`,
`recallatron.memory.remember`) lists it, and every configuration operation does not, so a job
cannot install a module or rotate a secret however it is written. Neither actor can approve
anything: `core.approval.*` lists `owner` and `member` only, and is non-token-issuable besides
([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)),
so in release one the approval operation exists in a web session's context and nowhere else.

## Request lifecycle

Every operation, whatever surface called it, runs the same way:

1. The boundary builds the `WorkspaceContext`.
2. The service registry looks up the operation by name, checks the context's `operation_set`
   contains it, checks the module owning it is in `enabled_modules`, and checks the role.
3. The registry applies the operation's safety class: read and draft run; mutate runs with an
   audit record; destructive, external, and financial require an approval in state `approved`
   for exactly this call ([confirmation](confirmation-and-safety.md)) or return
   `approval_required`.
4. The registry opens one `UnitOfWork` against the workspace database, runs the handler with the
   context and the validated input model, writes the audit record and any outbox events the
   handler queued, and commits. A handler cannot commit on its own.
5. Long-running work returns an `operation` record id immediately; the work itself is a job.

The MCP facade, the HTTP API, and the web tier's internal API are three thin encodings of step 1
over the same registry. None of them contains a branch on domain meaning.

**The HTTP `api` surface** is one route shape over the registry, so that no builder invents a
resource layout per module: `POST /api/v1/operations/<operation_name>` with the operation's
input model as the JSON body, returning `{ "state", "operation_id", "result" | "error" }` where
`state` is the operation record's state (`succeeded` with the output model in `result`;
`approval_required` with the approval id in `result`; `failed` or `refused` with `error_code` and
`error_text` in `error`; `pending` for a long-running operation, whose caller then polls
`GET /api/v1/operations/<operation_id>`, the same shape). The webhook receiver's route
([intake](intake-and-events.md#transports)) is the one route outside that shape, registered by a
connector binding. The surface accepts bearer tokens of kind `cli` and `mcp`, never a cookie and
never a `runtime` token ([presentation](identity-and-topology.md#tokens-for-cli-and-mcp-fr-4)),
and its OpenAPI document is generated from the registry's input and output models at startup;
the web tier's client is generated from that document and is never hand-edited. The internal
listener serves the same routes to the web tier with the session header in place of the token.

## Directory and package mapping

The scaffold's tree is the package layout; nothing new is invented.

| Directory | Python distribution or app | Holds |
| --- | --- | --- |
| `packages/contracts` | `rheo_contracts` | The manifest model, `WorkspaceContext` type, safety classes, the event envelope, the observation envelope, the runtime contract types, repository protocols shared across modules, record-reference type. Pure models, no I/O. |
| `packages/core` | `rheo_core` | Boundary adapters, service registry, storage backend and unit of work, migration orchestrator, settings, secret store, identity boundary and providers, sessions and tokens, outbox and worker runtime, operations and audit, approvals, deletion coordinator, export and restore, routing configuration, record resolver. |
| `modules/recallatron`, `modules/relationships`, `modules/leads`, `modules/current` | `rheo_recallatron`, `rheo_relationships`, `rheo_leads`, `rheo_current` | One module each: manifest, migrations, repositories, services, tools, event handlers, web contribution (`web/` subdirectory of TypeScript). |
| `connectors` | `rheo_connectors` | The three release-one transports (signed webhook, file import, manual capture) as thin adapters over the Leads intake service. |
| `runtimes` | `rheo_runtimes` | `ClaudeCliRuntime`; later the OpenRouter and Codex adapters. |
| `channels` | `rheo_channels` | Empty in release one. |
| `packs` | data only | Reusable defaults; empty of behaviour in release one. |
| `apps/core`, `apps/worker`, `apps/mcp`, `apps/cli` | entry points | Composition roots. `apps/mcp` is a package imported by `apps/core`, kept separate so criterion 20's import check has a boundary to test. |
| `apps/web` | Next.js application | The shell and the composed module screens. |
| `deploy` | container and proxy templates | Generic. |
| `tests` | the behavioural suites | Contract, integration, acceptance, one directory per public phase. |
| `examples` | synthetic fixtures | The synthetic funnel, demo workspace content. |

The core carries no import of any module: `rheo_core` and `rheo_contracts` list no module
distribution as a dependency, and modules are discovered through the `rheo.modules` packaging
entry-point group at startup ([module contract](module-contract.md#discovery-and-registration)).
Criteria 33 and 66 are proved by running the phase suites in a workspace with the memory module
never installed, and by an import-graph check that fails if `rheo_core` imports any
`rheo_<module>` package.

## What the core owns, concretely

The idea document's six core responsibilities map onto `packages/core` subpackages:

| Responsibility | Subpackage | Contract document |
| --- | --- | --- |
| Identity and access | `identity`, `sessions`, `tokens`, `boundary` | [identity and topology](identity-and-topology.md) |
| Extension lifecycle | `modules` (registry, install, enable, manifest validation) | [module contract](module-contract.md) |
| Agent and application access | `operations` (service registry), `routing`, `apps/mcp` | [runtime and MCP](runtime-and-mcp.md), [confirmation](confirmation-and-safety.md) |
| Durable execution | `work` (outbox, deliveries, jobs, schedules), `approvals`, `secrets` | [intake and events](intake-and-events.md), [confirmation](confirmation-and-safety.md) |
| Data operations | `storage`, `migrations`, `exports`, `deletion`, `audit` | [storage](storage-and-workspaces.md), [deletion, export, migration](deletion-export-migration.md) |
| Record references | `refs` | [identifiers](identifiers.md) |

The core knows no record type of its own beyond those tables. It has no `party`, no
`opportunity`, no `memory`. The three release-one modules and their ownership:

| Module | Owns | Depends on |
| --- | --- | --- |
| `relationships` | Party, contact point, affiliation, merge record and its items, review candidate (R4; [relationships](relationships.md)) | core only |
| `recallatron` | Memory and its purposes, memory entity, mention, link, embedding, migration batch ([memory](memory.md)) | core only; `leads` and `relationships` optional |
| `leads` | Intake connection, field mapping, routing rule, funnel, campaign, delivery receipt, payload, conflict, observation, import batch ([intake](intake-and-events.md)); opportunity, its party and observation links, field state, notes, pipeline preset and pipeline, qualification, draft, handoff and snapshot ([opportunity records](confirmation-and-safety.md#opportunities-parties-qualifications-and-handoffs)); contact permission and suppression records | core, `relationships` (required), `recallatron` (optional) |

`leads` requires `relationships`; `relationships` requires nothing (FR 40, criterion 54).
`recallatron` links to Leads records by reference and declares Leads an optional dependency, which
is what lets the phase-three suite run with memory absent (criterion 66) and memory run with Leads
absent.

## Stack decision record (D3)

**Decision.** A Python core (FastAPI domain services) and a Next.js web interface.

**The comparison.** One TypeScript monolith (Next.js serving both the interface and the
application services, one process, one language) was the alternative. It was viable. It would
have bought a single deployable process, one type system across the boundary with no generated
client, and a cheaper port of the work-in-motion interface, whose source is already Next.js.

Python won on four counts. The opportunity-discovery predecessor's scoring and proposal-preparation
code is Python, and it is the deepest behaviour being ported; typed contracts (pydantic models for
manifests, envelopes, and settings, with JSON Schema generated from them for the interface) fit
the contract-heavy design; the external back-office integration target is Python; and the
maintainer's familiarity is Python for services. None of these is a claim that TypeScript fails.
It is a port-cost and preference decision, recorded as such.

**Consequences.** Two runtimes to deploy and two dependency trees to keep current. One generated
boundary: the web tier consumes a TypeScript client generated from the core's OpenAPI document,
and that client is a generated artifact nobody hand-edits. Module authors who contribute screens
write TypeScript for the `web/` contribution and Python for everything else.

## The modular monolith, recorded

**Decision.** Logical boundaries (core, modules, connectors, runtimes, channels) enforced by
package dependency rules and the registry, not by network boundaries. One image for core and
worker; a separate web process only because the interface is Next.js.

**Why not services.** Nothing in the requirements needs independent deployment or scaling of a
module, and the cost of services (network contracts, distributed transactions across the outbox,
per-service auth) would be paid by a one-person release. The one-database-per-workspace choice
also makes in-process, single-transaction cross-module operations possible where the contract
allows them (the deletion cascade uses it deliberately, see
[deletion](deletion-export-migration.md#the-cascade)).

**What keeps the boundaries real without processes.** Modules are separate distributions with
declared dependencies; the core imports none of them; a module's storage is its own schema and
a static check holds it to that; cross-module change goes through registered operations and
events; and the composition root is configuration, not code that names modules. The idea
document's warning that "in-process modules share a runtime trust boundary" stands: registry
permissions constrain what a well-behaved module can do, not what malicious code can do, and
that is why host-installed package availability is an operator decision.

## Known tensions

Recorded here so a reader does not mistake them for oversights.

- The web session holds one active workspace at a time; two workspaces in two tabs of one browser
  is not a release-one capability. It follows from criterion 6's literal reading and is the
  simplest correct model; the hosted edition or a later release may add a workspace path segment
  with the same server-side check.
- `ClaudeCliRuntime` reports `isolation = advisory`, so a workflow that requires enforced
  isolation is refused on it until an operator-provided sandbox exists. That is honest rather
  than convenient, and a builder expecting the CLI to be the everything-runtime will meet it in
  criterion 15's test.
- FR 12's precedence and floor are tested by acceptance criterion 69, added to the build plan
  (append-only) when phase one was planned in detail: one key set at each source resolves in
  precedence order, a floored override looser than the deployment value is refused at write
  time naming the key, and a stored row is clamped at read time once the deployment value
  tightens, all driven end-to-end through the settings write path
  ([storage](storage-and-workspaces.md#a5-configuration-precedence-and-the-policy-floor-fr-12)).
- The purpose vocabulary is a closed enum until the framework-proof phase
  ([contact permission](intake-and-events.md#contact-permission-records-fr-46)).
- `CREATE EXTENSION` needs a permitted database role; the remedy is a template database
  ([storage](storage-and-workspaces.md#provisioning)). The migration orchestrator is the only
  entry point for Alembic ([storage](storage-and-workspaces.md#migrations)).
- The reference deployment's module subdomains give the interface several origins. The
  [session grant](identity-and-topology.md#sessions-and-cookies) handles it; single-host path mode
  never exercises it, which is why both modes are a CI gate (criterion 22).
- The transactional outbox fans out at write time to the consumers enabled in that workspace at
  that moment. A module enabled later receives no earlier events by design; the later lifecycle
  milestone's re-enable rule (no replay of completed external effects) is the same rule from the
  other side.
- Criterion 18's second check, that the production registration set carries no external-class
  or financial-class operation, is a **release-one invariant** (phases one to three), true
  because the handoff request is mutate class and the only external-class operation is the test
  sink. Phase five introduces the first production external-class operation, the handoff
  destination's execution ([later phases](later-phases.md#phase-5-the-work-in-motion-module)),
  and must change that CI assertion when it lands; until then the assertion is exact.
- Approval is a web-session act in release one: no token of any kind carries `core.approval.*`
  ([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)).
  A person working from a terminal opens the interface to approve. The requirements do not ask
  for command-line approval, and one rule with no kind branch is what makes A10 structural; a
  later release that wants a person's `cli` token to approve would relax that one rule and
  nothing else.
