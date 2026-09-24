# Architecture

**Status:** Architecture specification, designed against the accepted
[requirements and scope](../requirements/requirements-and-scope.md) (D1 to D11, R1 to R5, FR 1 to
FR 53) and the accepted [build plan](../requirements/build-plan.md). The decisions recorded here as
A1 to A17 are this specification's own calls, made where the requirements deliberately left the
design open. They are **accepted architecture**: the maintainer reviewed and merged the pull
request that carried them (PR #7, 2026-09-09). Each is written as a decision with rationale so
that a later revisit can reject one on its reasoning rather than on its vagueness. A18 was added
afterwards, with the [theme token contract](theme-contract.md), by the change that resolved
issue #40 and amended D8 from one theme to a themable contract.

**Source of truth for what the system must do:** the requirements. This specification never
reopens a settled decision; where it believes one is worth revisiting it says so under
[known tensions](overview.md#known-tensions), not by designing around it.

**Depth:** implementable for release one (public phases one to three), with every release-one
table at column level. One page per later phase.

## Reading order

| Document | What it settles |
| --- | --- |
| [Overview](overview.md) | The modular monolith with a durable worker, the four processes, the workspace context every request carries, the request lifecycle, the directory-to-package map, the core/module boundary, and the stack and monolith decision records. |
| [Identifiers](identifiers.md) | UUIDv7 everywhere durable, the record-reference form, permission-checked resolution, and every namespace in one table. |
| [Storage, workspaces, configuration, and secrets](storage-and-workspaces.md) | Database per workspace (the deferred D1 call), the control plane, server-derived routing, provisioning, schema per module, migrations, the storage and retrieval adapter seams, the data root, configuration precedence with the operator policy floor, the secret store, and the storage decision record. |
| [Module contract, version 1](module-contract.md) | The manifest, registration, install and enable in code, the full lifecycle on paper, compatibility, and how the boundary is proved by absence. |
| [Memory](memory.md) | The memory records with provenance, the explicit audience and purpose model and the intersection rule on derivation, retrieval's two stages, correction and supersession under one invalidation rule, retention under one per-workspace setting, the embedding job, and the predecessor migration. |
| [Relationships](relationships.md) | Party, contact point, affiliation, merge record, and review candidate at column level; the two classes of automatic-match evidence; `resolve_or_create`; alias-based merge and restore-from-record unmerge; how a resolved review reaches Leads. |
| [Intake, events, durable work, and audit](intake-and-events.md) | The observation envelope, receipts and conflicts, field mapping and routing, field derivation, the three connector bindings and transports with secret rotation, the outbox and event envelope, ordering with the lease query, retry, replay and retention, what event data may carry, jobs and leases, operation records, audit, natural idempotency, external actions, and contact permission and suppression records. |
| [Runtime contract, the MCP facade, and the redaction contract](runtime-and-mcp.md) | Request binding, capability discovery, normalized outcomes, native session handles, `ClaudeCliRuntime`, the OpenRouter shape, the MCP facade conventions and release-one tool set with roles, and what reaches a model provider. |
| [Confirmation, safety classes, pipeline presets, and the opportunity records](confirmation-and-safety.md) | The six classes' dispatch behaviour, the approval record and binding, standing grants, execution guards, the test fixtures, what a pipeline preset can configure and how records migrate, and the opportunity, its parties, qualifications, drafts, and handoffs at column level. |
| [Deletion, export and restore, and migration verification](deletion-export-migration.md) | The record-level deletion cascade, the content-free deletion record, receipt survival, the export artifact and restore, migration verification, and release-one disaster recovery. |
| [Identity, sessions, tokens, and URL topology](identity-and-topology.md) | The identity-provider boundary, sessions and the active workspace, CLI and MCP tokens with the non-token-issuable rule, the issuer bound, and the named package sets, host-only cookies with per-host secrets and the session grant, the routing configuration and table, the reference deployment's hosts, and the hosted-edition constraint. |
| [Theme token contract, version 1](theme-contract.md) | A theme as declarative data: the 43 tokens in six groups, `validateTheme` and `compileTheme`, the value grammars that guard the `<style>` element, the confirmation chrome only built-in themes set and its two real defences, the `color-scheme` residual, and the seed default. |
| [Later phases](later-phases.md) | Phases four to eight, one page each. |

## Architecture decisions

Each is decided here, with its rationale in the named document. Requirements decisions (D, R) and
functional requirements (FR) are cited by number throughout; these are the additions.

| # | Decision | Where |
| --- | --- | --- |
| A1 | The per-workspace storage unit is a **database**, not a schema. | [storage](storage-and-workspaces.md#a1-the-per-workspace-unit-is-a-database) |
| A2 | Durable record and event identifiers are **UUID version 7**; records are named across modules by a typed **record reference** `<module>.<type>:<uuid>`; nothing encodes the workspace. | [identifiers](identifiers.md) |
| A3 | Inside a workspace database, **one Postgres schema per module** plus `core`; tables are always fully qualified. | [storage](storage-and-workspaces.md#a3-one-postgres-schema-per-module) |
| A4 | The **secret store** is a file-backed store with an environment backend behind one protocol; `secret://` references; scope tokens per presenting component; credential slots so references never travel. | [storage](storage-and-workspaces.md#a4-the-secret-store-fr-13-guardrail-14) |
| A5 | **Configuration precedence** is package defaults, deployment settings, workspace and member rows, with an operator **policy floor** expressed as a comparator per key and enforced at write and at read. | [storage](storage-and-workspaces.md#a5-configuration-precedence-and-the-policy-floor-fr-12) |
| A6 | The **module manifest** is a typed Python object; modules are discovered through a packaging entry-point group; the registry is process-global and activation is per workspace. | [module contract](module-contract.md) |
| A7 | **Intake belongs to Leads**; the three transports are connectors over one `accept_delivery` operation; receipts are unique per connection and source event, conflicts are records. | [intake and events](intake-and-events.md#part-one-the-intake-contract) |
| A8 | The **outbox** fans out at write time, orders per subject, dedups per consumer in the consumer's transaction, makes up to eight attempts with backoff, and replays only to replay-safe consumers; the **audit row** is written by the dispatcher, never by a module. | [intake and events](intake-and-events.md#part-two-events-durable-work-operations-audit) |
| A9 | The **runtime contract** binds a request to an operation, carries permitted tools as a run-scoped MCP token, discovers capabilities before invocation, and yields fixed outcome and failure kinds. | [runtime and MCP](runtime-and-mcp.md#runtime-contract-version-1) |
| A10 | The **MCP facade** is names over operations with no handlers; tools restate their operation's class; **approvals are never given through MCP**. | [runtime and MCP](runtime-and-mcp.md#the-mcp-facade) |
| A11 | Sessions use **host-only cookies** with a one-time **session grant** from the identity host; the core serves `/auth/*` on every application host; both topologies are one implementation. | [identity and topology](identity-and-topology.md#sessions-and-cookies) |
| A12 | The **process topology** is `core`, `worker` (same image), `web`, and `postgres` behind the operator's reverse proxy, with a fixed routing table. | [overview](overview.md#processes) |
| A13 | The **redaction contract** is three field tiers declared per record type in the manifest, a purpose-gated context builder, and bounded retention. | [runtime and MCP](runtime-and-mcp.md#the-redaction-contract) |
| A14 | **Deletion** is a coordinator running the owning module and every registered participant in one transaction, then removing held export artifacts after commit. | [deletion, export, migration](deletion-export-migration.md#the-cascade) |
| A15 | **Pipeline presets** are versioned copy-on-write; opportunities and qualifications pin a version; migration is an explicit operation with a stage map. | [confirmation and safety](confirmation-and-safety.md#pipeline-presets-and-their-limits) |
| A16 | A **merged party is an alias**: it keeps its row, references outside the module are never rewritten, the resolver follows one hop, and the merge record holds the pre-merge ownership so unmerge restores from the record. | [relationships](relationships.md#a16-alias-based-merge-decided) |
| A17 | Every **memory carries an explicit audience and purpose set**; derivation takes the intersection and refuses when empty; one invalidation rule serves deletion, correction, and supersession. | [memory](memory.md#a17-explicit-audience-and-purposes-intersection-one-invalidation-rule) |
| A18 | The **web interface is composed from module manifests at build time and filtered per workspace at runtime**; the **theme is a versioned declarative token contract** whose confirmation-chrome subset only built-in themes set. | [theme contract](theme-contract.md), [module contract](module-contract.md#extension-points) |

## What this specification does not decide

The licence (framework-proof phase). Domain-pack composition rules, the capability registry as a
product surface, and the lifecycle acceptance criteria beyond install and enable (phase seven).
The hosted edition's tenancy scheme, encryption, and billing (phase eight). The external correction
flow for copies of personal data held by other systems, and how modules extend the purpose
vocabulary (both named as later work in the documents above). The reusable-pack export rules and
the packaging allowlist detail, which wait for packs to exist.

## Conventions

Tables are given with column names and Postgres types at entity level; a builder derives the
migration. Settings keys are given with their package default and, where the operator owns them,
their floor comparator. Names follow the [namespace table](identifiers.md#namespaces-in-one-place).
No document here names a hosting vendor, a proxy product, a person, or a real funnel; the
reference deployment is "a single-server container deployment behind a reverse proxy with a
wildcard certificate" (D10), and every example is synthetic.
