# rheoStream — requirements and scope

**Status:** Accepted. Ratified by the maintainer's review and merge of the pull request that
carried this document (PR #5, 2026-09-09). The eleven settled decisions D1 to D11 record
directions the maintainer already gave; the five decisions under
[Questions resolved here](#questions-resolved-here), R1 to R5, were taken on the maintainer's
behalf and are ratified by that merge. The `circuit.` shell-host name was confirmed at
ratification, with the reference deployment additionally keeping the `app.` name in reserve as a
redirect to `circuit.`. D12 was added at run 1a2's close-out (2026-09-23) and was ratified by the
maintainer's merge of the pull request that carried it (PR #107, 2026-09-24).
**Lineage:** This document supersedes no earlier requirements document. The path it replaced,
`docs/requirements/rheo-stream-requirements.md`, held no requirements.
**Source:** [Idea document](../ideas/rheo-stream-idea.md), including its 26 architectural
guardrails, its 24 open questions, and its architecture acceptance-scenario table.
**Companion:** [Build plan](build-plan.md), which carries the phased plan and the numbered
acceptance criteria.

## What this document is

The idea document establishes the product thesis and deliberately leaves decisions open.
This document closes the decisions that release one cannot start without, records their
reasoning, states what release one must do, and draws an explicit line around what it will
not do. It does not contain schemas, API shapes, event contracts, or tool signatures. Those
belong to the architecture specification, which is the next document.

Where this document changes a direction the idea document had settled, the change is
recorded with its reason, in the two places the idea document requires: here, under
[Recorded changes of direction](#recorded-changes-of-direction), and in the idea document's
own decision ledger.

## Outcome

Release one succeeds when one person can receive a real inbound inquiry from a funnel they
already operate, work it as an opportunity through a configured pipeline, and reach a
terminal outcome, using rheoStream alone and without touching either predecessor
application for that path. Everything else in this document exists to make that sentence
true without foreclosing the framework the idea document describes.

## Who this is for

The audience list comes from the idea document. Release one serves the first two personas
directly and must not block the rest.

| Persona | Primary need | Release-one position |
| --- | --- | --- |
| Independent freelancer running the suite privately | Opportunities from their own funnels, worked to an outcome, with context that survives between sessions | Served. This is the first reference configuration. |
| Business developing inquiries from its own sites, forms, assessments, events, or referral partners, with no job-search requirement | Generic intake and a pipeline that never mentions candidates or applications | Served. Job search ships later and stays optional. |
| Job-search user | The enrichment and application-preparation depth that made the opportunity-discovery predecessor useful | Later phase. The core must not acquire job-shaped fields in the meantime. |
| Consultant or small studio with several collaborators in one workspace | Shared workspace, per-member private material | Structurally supported (membership and roles exist from day one), not exercised. The invitation flow ships in the framework-proof phase, phase seven of the build plan. |
| HR consultant, regulatory approval consultant, sales representative | Specialist records and rules in their own modules on shared infrastructure | Not served. Exercised only by a synthetic example module during the framework-proof phase. |
| Technical user wanting selected modules | Install some modules, connect other tools | Partly served: modules are separately installable and enableable; disable and removal ship later. |
| Hosted customer | Managed isolated workspace | Not served. The hosted edition is a later, deliberately unplanned phase. |
| Contributor building a source adapter, channel, memory strategy, or integration | A documented extension point that does not require learning the whole system | Partly served: both the intake connector contract and the runtime contract are documented in release one. The intake contract is exercised by three transports in release one; the runtime contract is exercised by a second implementation by v1.0, not in release one (D4). |

## Settled decisions

Each row is decided. The build plan implements these; the architecture specification designs
against them; neither reopens them.

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | **Postgres is the sole reference storage backend for release one, with one database (or schema) per workspace, plus a small shared control plane for accounts and the workspace registry.** A self-hosted install is a container stack with a Postgres container. Billing belongs to the hosted edition and is added to the control plane in that phase; release one holds none. | The idea document requires choosing and operating one reference storage implementation first, and warns that keeping SQL behind adapters does not by itself prove parity: transactions, locks, search, migrations, and recovery each need validating per backend. Database-per-workspace keeps the physical-isolation property the idea document values without paying for two production backends. Partially answers idea-doc question 2. |
| D2 | **Per-workspace SQLite is a contract goal, not release-one code.** The storage adapter seam, the export format, and the behavioural test suite are written so a later local-first edition can supply a second backend. No SQLite implementation ships in release one. | Preserves the portability promise without claiming support that has not been exercised. Guardrail 7 becomes an architecture obligation rather than a release-one deliverable. |
| D3 | **The implementation stack is a Python core (FastAPI domain services) plus a Next.js web interface.** | Decided after an explicit comparison against a single TypeScript monolith. The TypeScript option would have bought a one-process deployment and a cheaper port of the work-in-motion interface. The Python option wins on porting the existing scoring and proposal-preparation code, on typed-contract fit, on adjacency to the external back-office integration target, and on maintainer familiarity. Both options were viable; this is a preference backed by port cost, not a claim that the alternative fails. |
| D4 | **`ClaudeCliRuntime` ships first. The OpenRouter adapter is the second implementation and is required for v1.0, not for the first working slice. Codex CLI remains a validation target.** | The runtime contract is only proven by a structurally different second implementation, so the OpenRouter adapter is not optional; it is sequenced. Shipping both before the first useful slice would delay every domain decision behind runtime work. See [Recorded changes of direction](#recorded-changes-of-direction). |
| D5 | **Module lifecycle: full contract on paper, install and enable in code.** The specification defines the complete manifest and lifecycle contract. Release one implements install and enable only. Disable, remove, purge, and restore are a defined later milestone. | The idea document names "modularity that ends at installation" as a principal risk, so the contract must be written now. It also names "building an automation platform before a useful workflow" as a risk, so the lifecycle machinery must not be built now. Writing the contract and deferring most of its implementation is the only way to honour both. |
| D6 | **GitHub OAuth is the first login provider, behind a pluggable identity-provider boundary.** Other providers, email and password first among them, land later without touching domain code. Self-hosters register their own GitHub OAuth application; client id and secret live in private deployment configuration. CLI and MCP access uses local tokens, not browser OAuth. | The first users are developers who already hold a GitHub account, so it removes a password store from release one entirely. Naming the provider keeps the first implementation unambiguous for a builder. The boundary exists so that this convenience does not become a permanent requirement for non-developer users, and so that GitHub is replaceable rather than assumed. |
| D7 | **URL topology is configuration, not hard-coded.** Single-host path mode is the default for self-hosters. Subdomain-per-module is a supported option and is what the reference deployment uses. All link generation goes through routing configuration. | A self-hoster with one hostname and one certificate must not be forced into wildcard DNS. The reference deployment must not be the only tested arrangement. See [Recorded changes of direction](#recorded-changes-of-direction). |
| D8 | **The web interface is ported from the two predecessor applications, not rewritten.** Screens are carried over, design tokens and theme unified, and upgrades made only where cheap. The application shell is the one exception: phase one builds it new, and phase two unifies navigation and theme on it, as the [UI port inventory](#ui-port-inventory) records. *Amended 2026-09-24, issue #40:* "theme unified" no longer means one theme. Phase two ships a themable contract: a versioned, declarative [theme token contract](../architecture/theme-contract.md) that sets color, type and shape, with the approval and confirmation chrome drawn from a token subset only built-in themes set (architecture decision A18). The recorded change of direction is in the [idea document's decision ledger](../ideas/rheo-stream-idea.md#recorded-changes-of-direction). | Rebuilding working interface code is the fastest route to the "endless rewrite" risk the idea document names. The port is bounded by the [UI port inventory](#ui-port-inventory) and gated by guardrail 1 and the publication rules, because ported interface code carries legacy names, hard-coded paths, and sometimes real data. |
| D9 | **The memory port includes a real retrieval-layer rework.** The memory layer inside the work-in-motion predecessor uses `sqlite-vec` for embeddings and FTS5 for search. Neither exists in Postgres. The port replaces them with pgvector and with `tsvector`/`pg_trgm` or a hybrid, behind the retrieval adapter the idea document already requires. | Sized explicitly as its own requirement so it is not discovered mid-port. Containing it behind the retrieval adapter keeps the choice of ranking strategy revisable. |
| D10 | **The reference instance is a single-server container deployment behind a reverse proxy with a wildcard certificate.** It is the reference self-host deployment under guardrail 15, so the web interface must not depend on platform-only features: no reliance on a particular hosting provider's edge functions, image pipeline, or build integrations. | Guardrail 15 says hosted-only assumptions must not accumulate unnoticed. The cheapest way to enforce that is to make the maintainer's own daily instance a plain self-host. |
| D11 | **The memory module is the first real module, shipping after the walking skeleton and before the opportunity core.** | It is the smallest domain that can prove the module contract end to end. It owns records, migrations, configuration, tools with declared safety classes, an export format, and interface contributions, and it needs no other module to do any of that. Proving the contract on the opportunity core instead would entangle the first module-contract test with intake, party resolution, and pipeline configuration, so a contract defect and a domain defect would be indistinguishable. |
| D12 | **Build the product, borrow the algorithms: rheoStream writes its own product code, and may adopt a published algorithm under a compatible licence where it fits the problem as posed.** A file whose content is borrowed carries a header naming the source, its licence, the exact commit or version borrowed from, and what changed; hand-written code carries no such header. The first evaluation under this rule borrowed nothing. Run 1a2 read EverAlgo (`github.com/EverMind-AI/EverAlgo`, Apache-2.0) at commit `d830fec` for narrowing dedup candidates and declined it: `cluster_by_geometry` is an online operator that assigns one incoming item to a cluster, where the dedup path scores pairs over a batch; it carries a built-in 7-day session-adjacency window that the run's scope excluded; and it needs `numpy` and `everalgo-core` and imports LLM types at module level, none of which Recallatron declares. `everalgo-rank`'s `rrf(*sources, k=60)` was noted and not adopted, because hybrid retrieval's reciprocal rank fusion is hand-written. No file carries an EverAlgo attribution header. | A borrowed algorithm saves work only when it fits the problem as posed; bending one that does not costs more than writing the few lines the problem needs, and brings dependencies to carry. The row is separate from D9 and does not amend it: D9 decides that the memory port reworks retrieval onto pgvector and `tsvector`, and D12 decides where that work's algorithms may come from. The declined evaluation is recorded with the policy so that a later reader who finds EverAlgo's `rrf()` knows the hand-written fusion was a choice, and so that the row cannot be read as a borrow that did not happen. |

### Historical migration notes

Stated at the level of detail the idea document already uses, to size the port. These are
facts about superseded private systems, not descriptions of rheoStream.

- The opportunity-discovery predecessor (MOL in the idea document's legacy map) is FastAPI
  plus React/Vite, running on MySQL in production. Its migration is a MySQL-to-Postgres move
  regardless of any other decision here.
- The work-in-motion predecessor (M.O.T.) is Next.js with Drizzle over `better-sqlite3`.
- The memory layer (Recallatron) currently lives inside that work-in-motion predecessor and
  uses `sqlite-vec` for embeddings and FTS5 for search. It does not use pgvector. This is the
  reason for D9.

## Recorded changes of direction

The idea document asks later documents not to reverse its settled direction without
recording the reason. Three reversals follow.

**1. Runtime rollout order.** The idea document states that Claude Code print mode and
OpenRouter API execution "are explicit requirements." This document softens that into a
rollout order: `ClaudeCliRuntime` ships first, the OpenRouter adapter is the required second
implementation for v1.0, and Codex CLI stays a validation target. Reason: "both are
requirements" was written to prevent the architecture from assuming one runtime, and that
purpose is fully served by requiring the second adapter before v1.0. Read literally as a
first-slice requirement, it delays every domain decision behind two runtime integrations for
no design benefit. The guardrail this protects (guardrail 4, domain services do not depend
on Claude) is unchanged and is tested from phase one by the runtime contract, not by the
number of shipped adapters.

**2. Module subdomains.** The idea document states that "individual module subdomains are
unnecessary unless the modules later become independently deployed public services." This
document overrides that. Subdomain-per-module is a supported topology and is what the
reference deployment uses: `leads.rheo.stream`, `current.rheo.stream`, and so on, alongside
`api.`, `mcp.`, and `docs.`, with the apex serving the project and marketing page. The
application shell and the workspace switcher are served on `circuit.rheo.stream`, and the login
and the OAuth callback on `auth.rheo.stream`, so that the apex stays a static page and identity has
one endpoint that does not move when the shell changes. Splitting identity off the shell host
keeps the registered callback URL stable for a self-hoster and lets a later second front end
share one identity endpoint rather than registering its own. `tuttle.rheo.stream` is reserved
for the integration surface only, since that
back-office application stays external and local-first. Reason: the maintainer wants the module
boundaries visible in the address bar of the instance they use daily, and one application
serving all hosts through host-based routing costs a routing table, not a deployment per
module. Constraints that come with the override:

- One application serves the shell host, the identity host, and every module subdomain
  through host-based routing. Neither the modules nor identity are separately deployed
  services: `auth.` is a route boundary and a stable callback address, not a second
  deployment.
- In subdomain mode one session covers the shell host, the identity host, and the module
  hosts, which are the hosts the application serves. The `api.` and `mcp.` hosts authenticate
  by token (FR 4) and ignore the session cookie. The apex, the `docs.` host, and the reserved
  integration host are not served by the application and must not receive the session cookie;
  whether that is arranged by cookie scope or by the reverse proxy stripping it is an
  architecture-specification decision. Wildcard DNS plus a wildcard certificate cover the set.
- The identity host is the only registered OAuth redirect target. In single-host path mode
  there is no identity host and the callback is a path on the single origin; both arrangements
  are produced from routing configuration, never from a hard-coded host.
- Single-host path mode remains the default and stays continuously exercised, because
  guardrail 15 makes the plain self-host arrangement the one that must not rot.
- Carried forward to the hosted edition: module subdomains compete with per-tenant subdomains
  for the same namespace level, and a wildcard certificate covers one level only. A hosted
  multi-tenant edition must choose its scheme deliberately rather than inheriting this one.

**3. The shell host is `circuit.`, not `app.`** The idea document's endpoint list names
`app.rheo.stream` as the hosted application. This document renames it. Reason: under the module
subdomains above, `app.` is ambiguous, since `leads.` and `current.` are equally "the
application"; `circuit.` names the surface carrying the shell and the workspace switcher
specifically. An electrical circuit and a water circuit are both ordinary usage, so the name
sits in the same register as `rheo.stream` and `current.`, and a circuit is also a route made in
rounds, which is what the shell is for. Nothing structural changes: the shell host remains one
host among the several a single application serves through host-based routing, and in
single-host path mode it does not exist at all. Like every other host here, the name is
configuration, and a self-hoster may choose another.

## Questions resolved here

Five decisions are taken below rather than deferred. Four of them answer an idea-document
question outright. The fifth, R5, closes the deletion half of idea-doc question 19, which
release one cannot honestly ship with open. Each is a decision the maintainer ratifies by
accepting this document.

### R1. Workspace, membership, role, and invitation for a one-person start

*Answers idea-doc question 1.*

**Decision.** Release one implements workspaces as first-class records with a real membership
relation and a role on every membership, even though exactly one person will use it. It
implements two roles, `owner` and `member`, and no invitation flow.

Specifically:

- An account is separate from a workspace, and the relation between them is many-to-many from
  day one. The idea document already requires client data that needs separate ownership to
  live in separate workspaces even when one operator manages both, so a single human holding
  several workspaces is a release-one case, not a future one.
- Every service call receives an authenticated triple of actor, workspace, and role. There is
  no code path that infers "the only user."
- `owner` may change workspace configuration, install and enable modules, manage connections,
  and read every workspace-owned record. `member` may work records and may not change
  workspace configuration or module composition. Finer permissions are additive later; two
  roles are the smallest set that makes the check meaningful rather than decorative.
- Member-level scope is defined now and barely used: personal preferences and personal
  credentials are distinguishable in the model from workspace-owned records, because the idea
  document requires that a shared workspace not make every member's private information
  readable by all collaborators. Release one stores only the owner's.
- A second member can be added only by an operator-level administrative command against the
  control plane. There is no invitation email, no pending-invite state, no seat management,
  and no self-service member removal.

**Rationale.** The membership relation and the role column are cheap to add now and expensive
to retrofit, because retrofitting them means auditing every query and every authorization
check in the system. The invitation flow is the opposite: it is a self-contained feature with
its own state machine, its own email delivery dependency, and its own abuse surface, and none
of it is exercised by a one-person start. So the structure ships and the feature waits.

### R2. The first release slice

*Answers idea-doc question 4.*

**Decision.** The capability that proves custom-funnel intake is a single path, end to end:

> An inbound-inquiry form on a site the maintainer already operates posts to an authenticated
> intake endpoint. rheoStream durably accepts the delivery, records a receipt, normalizes it
> into an observation with per-field provenance, resolves or creates a party through the
> relationships module, applies an explicit routing rule that creates an opportunity in an
> inbound-services pipeline, and the maintainer works that opportunity through its stages to a
> terminal disposition, both in the web interface and through rheo.

That path is the release-one definition of done. It contains no job board, no candidate
profile, no application tooling, and no field named after any source product.

**How much work-in-motion support belongs in it: none.** The work-in-motion module ships in a
later phase. The opportunity workflow reaches a terminal disposition without creating work
anywhere. The handoff operation is defined in the contract and has exactly one release-one
destination: none. This is deliberate. The idea document is explicit that there is no
mandatory sequence in which every lead becomes a project, and building the destination before
the source would invert the dependency.

**How much memory support belongs in it: the whole module, off the critical path.** The memory
module ships in phase two, before the opportunity core, per D11. In the first release slice rheo
can recall and remember with workspace and record permissions enforced, and memories can link
back to Leads records. The intake-to-outcome path must complete correctly in a workspace where
the memory module was never installed and never enabled. If it cannot, the module boundary is
wrong. Release one has no workspace-level disable path to test against, because D5 places
disable in the later module-lifecycle milestone, so the boundary is proved by the module's
absence instead.

**Rationale.** The idea document's own success criterion is one person installing the system,
connecting their own funnel, choosing a pipeline, and developing opportunities while retaining
ownership of the data. That sentence names intake, pipeline, and ownership, and it does not
name work management. Cutting the slice at the terminal disposition keeps it honest: the first
thing shipped is the thing the success criterion describes.

### R3. Confirmation-policy outline

*Answers idea-doc question 11, at the level of policy shape and safety classes. The final
rules are an architecture-specification deliverable.*

**Decision.** Six safety classes, one declared class per operation, a floor per class that
workspace policy may tighten and may never loosen.

| Class | Meaning | Confirmation floor |
| --- | --- | --- |
| Read | Inspect or retrieve information | None. Authorization check only. |
| Draft | Prepare a proposal, message, plan, or possible state change with no external effect | None. The draft is a record, not an act. |
| Mutate | Reversible internal state change | None by default; audit record required. |
| Destructive | Deletion, purge, or any internal change with no reversal path | Explicit per-action confirmation. Never covered by a standing grant. |
| External side effect | Send, submit, publish, or otherwise affect another person or system | Explicit per-action confirmation. Never covered by a standing grant. |
| Financial | Move money, incur a charge, or commit to a payable obligation | Explicit per-action confirmation, always, with the amount and payee restated at the point of confirmation. |

The shape of the policy, independent of the eventual rules:

1. Every service operation and every MCP tool declares exactly one class in its manifest. An
   operation with no declared class is refused at registration, not at call time.
2. Deletion is its own class and is never classified as an ordinary reversible mutation. The
   idea document says so directly; the class list makes it structural.
3. An approval binds a tuple: actor, workspace, operation, recipient or destination, payload
   digest, purpose, and an execution window. Changing any element invalidates the approval.
4. Permissions and contact restrictions are rechecked at execution, not at approval. An
   earlier approval in a conversation cannot override a later revocation.
5. Standing grants exist only for read, draft, and mutate, and are scoped to a workspace and a
   named operation set. They are never available for the destructive, external, or financial
   classes.
6. A headless or channel-mediated request that hits a confirmation requirement returns an
   explicit approval-required state. It must never hang and must never proceed.
7. Every confirmed action writes a durable approval record carrying the payload digest, so
   after the fact it is answerable what exactly was approved rather than what was intended.
8. Untrusted evidence (imported text, URLs, attachments, retrieved memories) never satisfies a
   confirmation requirement and never changes a class. Text is evidence; only an authenticated
   actor approves.

**Rationale.** Four classes were proposed by the idea document; two are added because the idea
document itself distinguishes deletion from ordinary mutation and because a financial action's
failure mode differs in kind from a mis-sent message. Fixing the class list and the approval
binding now is what lets tool signatures be designed later without reopening the safety model.

### R4. Minimum relationships-module contract

*Answers idea-doc question 18.*

**Decision.**

**What the relationships module owns**, and nothing more: a stable party identifier; whether
the party is a person or an organization; a display name; a set of contact points, each with
its type, its value, its verification state, and its provenance; and affiliation links between
persons and organizations, each with a validity period so that a person representing different
organizations over time is representable without rewriting history.

**What it does not own:** opportunity participation (Leads), confidential case facts (a
specialist module), evidence relationships (a specialist module), notes, tags, scores, or any
field a profession would recognize as its own. Referencing a party grants no access to records
about that party. The shared contact record is not a container for specialist data.

**What an external CRM may own:** any field except the rheoStream party identifier itself.
A CRM connection declares, per field, which system is authoritative and which direction of
update is permitted. A write to a field the CRM owns is rejected, not merged. Replacing a CRM
provider requires an explicit identity migration; two competing authorities for one field is a
configuration error the connection refuses to accept.

**What evidence permits an automatic match:** exactly two things, both scoped to one workspace
and one source connection.

1. An external subject identifier within that connection's own namespace, where the connection
   authenticated the subject.
2. A normalized email address that the source connection itself verified.

Everything else produces a review candidate and never an automatic link. Name similarity,
shared company domain, phone number, and postal address are hints. The idea document is
explicit that email and domain matches are party hints and not global merge keys. Matching
never crosses a workspace boundary.

**What makes a merge reversible:** a merge does not destroy either input. Both original party
identifiers remain resolvable and both continue to resolve after the merge, one of them as an
alias. The merge writes a record naming the evidence, the actor, and the time. Unmerge
restores the prior state from that record. Reversibility is a property of the representation,
not of an undo log that might be pruned.

**Rationale.** The idea document requires shared party identity to sit behind a small reusable
module independent of Leads, because other domains need people and organizations even when
opportunity management is absent. The smallest contract that satisfies that is identity plus
contact points plus affiliation. Automatic matching is restricted to authenticated evidence
because a wrong merge is a privacy event, not an inconvenience: it can expose one client's
correspondence inside another's record.

### R5. Record-level deletion for observations, parties, and opportunities

*Answers the deletion half of idea-doc question 19.*

**Decision.** Release one ships a record-level deletion path for observations, parties, and
opportunities. It sits in the destructive safety class under R3, so every deletion takes an
explicit per-action confirmation and no standing grant ever covers it. Deletion cascades the way
FR 28 already requires for memory, to derived summaries and embeddings, and beyond that to
queued actions that depend on the record and to export artifacts that carry it. Every deletion
leaves a durable record of what was deleted, by whom, and when, and that record retains none of
the deleted content.

Specifically:

- The unit of deletion is one record of one of the three types, named by identifier. Release one
  has no bulk erase and no scheduled expiry beyond the memory retention setting FR 29 already
  requires.
- "Exports that carry the record" means export artifacts the system still holds under its data
  root and tracks by an export record. Deleting a record removes every such artifact that
  carries it and marks the artifact's export record as removed by deletion. An artifact that has
  already left the system, downloaded or copied elsewhere, is outside this path's reach, and the
  path does not claim otherwise.
- Deleting an observation does not delete its delivery receipt. The receipt retains the source
  event identifier, the content digest, and the timestamps only; it holds no copy of the payload,
  so it carries none of the person's data. It survives so that FR 33's durable-receipt guarantee
  holds and so that FR 34 can still detect the same source event arriving again: a re-delivery
  of a deleted observation's source event is recorded as a conflict against the surviving receipt
  and never recreates the observation. This is the minimum suppression metadata the idea
  document allows deletion to retain.
- Deleting a party does not delete the observations that referenced it. Those observations lose
  the party reference and become unlinked evidence, because observations are evidence records
  with their own lifecycle (FR 39): the delivery happened whether or not the party record
  survives.
- Deletion is distinct from permission withdrawal under FR 46. Withdrawal stops future contact
  and future use; deletion removes the record. Neither implies the other, and the interface must
  not present them as one act.
- The deletion record holds identifiers, actor, time, and record type only. It is not an undo
  log. Deletion is not reversible, and nothing in this path restores a deleted record.

**Rationale.** Release one accepts real personal data belonging to third parties through a live
inbound form. Someone who filled in that form and later asks to be removed has to be answerable,
and nothing else in this document answers them: FR 28 cascades deletion for memory only, and
FR 46 covers permission to contact, which is not erasure. The alternative was to state plainly
that release one has no deletion path and to defer one to the architecture specification. That
was rejected because the data belongs to people who did not choose this system, and a first
release that can ingest a stranger's details but cannot remove them is not a defensible starting
position. The path is deliberately narrow, one record at a time, always confirmed, never
reversible, so that its narrowness is a safety property rather than a missing feature.

## Functional requirements

Release one must satisfy the following. Numbering is stable and is cited by the build plan's
acceptance criteria.

### Workspace, identity, and access

- **FR 1.** Every HTTP request, MCP call, background job, and channel session resolves a
  trusted workspace context before any domain service is called.
- **FR 2.** Storage routing for a workspace is derived server-side from authenticated context.
  No client, model argument, incoming payload, or workspace setting may supply a database
  path, connection string, or tenant identifier.
- **FR 3.** Authentication uses a pluggable identity-provider boundary with GitHub OAuth as the
  first implementation. Domain code depends on the boundary, not the provider.
- **FR 4.** Command-line and MCP clients authenticate with locally issued tokens scoped to an
  actor, a workspace, and a permitted operation set. A token is issued in one of two ways: from
  an already-authenticated web session, or, on a headless install with no browser available, by
  an operator-level command against the control plane. Once issued, the token is presented and
  used without any browser flow.
- **FR 5.** Membership carries a role of `owner` or `member`; every authorization check reads
  the role rather than assuming a single user (see R1).
- **FR 6.** Personal preferences and personal credentials are distinguishable in the model from
  workspace-owned records, so that adding a second member later does not expose the first
  member's private material.

### Storage, configuration, and private data

- **FR 7.** Each workspace's operational data lives in its own Postgres database or schema. A
  separate small control plane holds accounts and the workspace registry. The hosted edition adds
  billing metadata to that control plane in its own phase; release one holds none.
- **FR 8.** Storage-specific SQL sits behind repository or storage-adapter interfaces, and the
  behavioural test suite is written against the interface rather than against Postgres, so a
  second backend can later be proven rather than assumed.
- **FR 9.** Every workspace records its installed core version, its installed module versions,
  and each module's schema version, as inspectable product data rather than as operational
  scripts.
- **FR 10.** Runtime data is written outside the source checkout by default, to an
  operator-configurable data root that the host validates at startup. The reserved
  checkout-local directory is the explicit opt-in, and it is ignored by Git.
- **FR 11.** Modules obtain storage through the core's storage API and cannot choose arbitrary
  filesystem paths or write records into their installed package. A module writes no other
  module's storage, and effects a change to a record another module owns only through that
  module's public operations and events.
- **FR 12.** Configuration has three distinct sources with validated precedence: public package
  defaults, private deployment settings, and private workspace or member overrides. A workspace
  override cannot relax an operator security policy.
- **FR 13.** Secrets are held by reference in configuration and are scoped to the narrowest
  component that needs them. Source credentials, mail access, model credentials, and signing
  keys never appear in prompts, tool arguments, or general service context.
- **FR 14.** A fresh clone builds and runs a synthetic demo with no real workspace database,
  profile, credential, or infrastructure, and initialization leaves tracked source unchanged.

### Durable work and operations

- **FR 15.** A module commits its state change and its outgoing event in one transaction
  through an outbox; a worker delivers the event; consumers record processed event identifiers
  alongside their own state changes.
- **FR 16.** Background work uses leases, bounded retries, inspectable failures, cancellation,
  and restart recovery. Correctness never depends on one open HTTP request or one uninterrupted
  conversational turn.
- **FR 17.** Long-running operations return an operation identifier and expose progress and a
  terminal status. An operation whose outcome is unknown stays visible as unresolved rather than
  being reported as complete or silently retried.
- **FR 18.** Every mutating operation writes an audit record identifying actor, workspace,
  operation, and time.

### Agent runtime and the MCP boundary

- **FR 19.** rheo's model execution is behind a runtime adapter contract. Domain services accept
  structured requests and return structured results, and no domain module or channel references
  a specific runtime, model, or vendor.
- **FR 20.** `ClaudeCliRuntime` is implemented in release one. The runtime contract's capability
  discovery (tool calling, structured output, streaming, continuation, cancellation, usage
  reporting) is defined and enforced from the first adapter, so an unsupported requirement is
  rejected before a workflow starts rather than discovered mid-run.
- **FR 21.** A headless run that hits an unavailable executable, an expired credential, a denied
  tool, a timeout, or an incomplete stream returns an explicit non-success state. It never hangs
  and never reports a false completion.
- **FR 22.** The MCP façade exposes namespaced, goal-level tools that call application services.
  It exposes no raw database access and no privileged SQL path.
- **FR 23.** The workspace and actor for a tool call come from the authenticated session, never
  from a model-supplied argument.
- **FR 24.** Every MCP tool and every service operation declares one safety class from R3, and
  an operation with no declared class is refused at registration.
- **FR 25.** Imported text, URLs, attachments, and retrieved memories are treated as untrusted
  evidence. They cannot grant a tool, change a policy, or authorize an external action.

### Memory

- **FR 26.** The memory module is installable and enableable through the module registration
  contract, and every other release-one path completes correctly in a workspace where the module
  was never installed and never enabled. Workspace-level disable is not a release-one path (D5),
  so release one proves the boundary by absence rather than by disable.
- **FR 27.** Retrieval enforces the caller's current workspace and record permissions before any
  content reaches a model. Derived memories inherit the access and purpose restrictions of their
  sources, and combining sources never widens the audience.
- **FR 28.** Correction, supersession, and deletion invalidate derived summaries and embeddings
  as well as the source record.
- **FR 29.** Memory retention is explicit per workspace. Nothing is retained permanently by
  default. This forbids accidental indefinite hoarding, not a workspace choosing to keep its
  memories: age-based expiry is an opt-in, per-workspace setting (maintainer, 2026-09-21; see
  [memory § Retention](../architecture/memory.md#retention-fr-29-criterion-30)).
- **FR 30.** Retrieval runs behind an adapter so that the ranking strategy (dense, lexical, or
  hybrid) is a replaceable choice rather than a schema commitment (see D9).

Release-one memory also creates conservative memories automatically from eligible explicitly
stated transcript evidence, without a remember command or mandatory review per note. The
automatic-memory carrier delivers both Rheo-owned runtime evidence and local Claude Code
CLI/Desktop Code capture enrolled by machine and project/workspace, after retrieval and before
migration/cutover. It uses durable sanitation/digestion/extraction and trusted source receipts;
arbitrary cloud chats and blind promotion of tool/file material are excluded. FR27–29 continue to
bind every automatic result, and cutover must prove ambient non-regression. D9 remains the
retrieval-layer rework and is not the authority for extraction.

### Intake and evidence

- **FR 31.** A documented, authenticated ingestion API accepts deliveries and resolves the
  workspace from the connection's credential. An event's claimed tenant, destination, or
  permissions never override that binding.
- **FR 32.** Release one supports three intake transports: a generic signed webhook receiver,
  CSV/JSON import, and manual capture. Each maps source events to normalized observations
  through declarative, versioned field mapping.
- **FR 33.** A delivery is acknowledged only after its receipt and its pending processing work
  are stored durably. An acknowledged receipt means processing is pending; it never means an
  opportunity exists.
- **FR 34.** Receipt uniqueness is enforced within the trusted workspace and source namespace.
  A repeated event identifier with changed content is surfaced as a conflict, never applied as a
  silent update.
- **FR 35.** Intake works with no model session running.
- **FR 36.** Observations carry per-field provenance, source occurrence time, server receipt
  time, and a bounded copy of the source payload. Acquisition attribution (which funnel and
  campaign) is preserved separately from delivery transport.
- **FR 37.** Deriving current field values uses explicit source priority, revision or freshness,
  and field-ownership rules. A late or thinner observation never overwrites richer evidence or
  user-owned state, and missing fields are distinguishable from explicit clears.
- **FR 38.** Connection health, last successful intake, lag, and unresolved failures are visible
  to the workspace owner.

### Opportunities, parties, and pipelines

- **FR 39.** Signals, parties, opportunities, and qualifications are distinct records with
  distinct lifecycles. An observation may remain unlinked, be linked later, or support several
  opportunities through recorded decisions.
- **FR 40.** Party identity, contact points, and affiliations are owned by the relationships
  module per R4, independently of Leads, and a workspace can use relationships without Leads.
- **FR 41.** Automatic party matching uses only the two evidence classes in R4. Every other
  candidate goes to review, and every merge is reversible by construction.
- **FR 42.** Opportunities carry pipeline identity and configuration version, stable stage
  identifiers separate from editable labels, an owner, linked parties, provenance, and a terminal
  disposition mapped to a comparable outcome.
- **FR 43.** Editing a pipeline preset does not reinterpret existing records. Opportunities and
  qualification results pin the configuration version they were created under.
- **FR 44.** Workflow-specific fields are added through versioned, namespaced, schema-validated
  extensions. The common core acquires no job-shaped column and no product-specific identifier,
  including a renamed one.
- **FR 45.** A handoff is an explicit operation with a durable identifier, a source opportunity,
  a purpose, a destination, an approved payload snapshot, and a tracked result. Release one
  defines the operation and ships no destination.
- **FR 46.** Observed interest is stored separately from permission to contact or share, with
  the declared purpose, source disclosure version, timestamp, channel, permitted recipient scope,
  and any later withdrawal or suppression.

### Interface

- **FR 47.** URL topology is configuration. Single-host path mode is the default; subdomain mode
  is supported. Every link is generated through routing configuration, and no route string is
  hard-coded to one topology.
- **FR 48.** The web interface depends on no hosting-platform-only feature. It runs in a
  single-server container deployment behind a reverse proxy.
- **FR 49.** No route, package, table, MCP tool, service, environment variable, configuration
  key, or user-facing string carries a legacy product name or a generalized source-product
  prefix. Historical migration notes in documentation may identify their sources.
- **FR 50.** Ported interface code is reviewed for legacy names, hard-coded API paths, embedded
  personal data, and private deployment details before it enters the public repository.

### Deletion, export, and migration

These three are appended after FR 50 so that the numbering above stays stable.

- **FR 51.** Deleting an observation, a party, or an opportunity is a supported record-level
  operation in the destructive safety class (R3, R5). It takes an explicit per-action
  confirmation that no standing grant satisfies; it removes the record's derived summaries and
  embeddings along with the record, and every export artifact still held under the data root
  that carries the record; it cancels queued actions that depend on the record; it leaves an
  observation's delivery receipt in place, holding identifier, digest, and timestamps and no
  payload, so that a re-delivery of the deleted source event is recorded as a conflict and never
  recreates the observation; and it writes a durable deletion record naming the actor, the
  time, the record type, and the record identifier while retaining none of the deleted content.
- **FR 52.** A workspace export produces an artifact that restores into an empty deployment with
  its module composition, configuration versions, module schema versions, and records intact.
  Each module declares its own export format in its manifest, so a module's records travel with
  the workspace rather than needing core knowledge of that module. A restored pending approved
  action requires a fresh approval before it can execute.
- **FR 53.** Migrating a predecessor data store into a rheoStream module is verified before
  switchover: every source record has a destination record, and a sampled set of retrieval
  queries returns the same records under the new implementation as under the old one, within a
  documented tolerance. The verification result is recorded where the migrated data lives, in
  private workspace storage, and never in the public repository.

## Release-one scope boundary

### In scope

- Repository bootstrap, the framework core, workspace and Postgres storage foundation, durable
  work, the approval and confirmation records R3 defines, and the MCP façade.
- One agent runtime adapter (`ClaudeCliRuntime`), against a contract designed for more.
- The memory module, ported with its retrieval layer reworked, plus migration and cutover from
  the predecessor.
- Release-one memory also creates conservative memories automatically from eligible explicitly
  stated transcript evidence, without a remember command or mandatory review per note. The
  automatic-memory carrier delivers both Rheo-owned runtime evidence and local Claude Code
  CLI/Desktop Code capture enrolled by machine and project/workspace, after retrieval and before
  migration/cutover. It uses durable sanitation/digestion/extraction and trusted source receipts;
  arbitrary cloud chats and blind promotion of tool/file material are excluded. FR27–29 continue
  to bind every automatic result, and cutover must prove ambient non-regression. D9 remains the
  retrieval-layer rework and is not the authority for extraction.
- The relationships module at the R4 contract.
- Generic intake with three transports, the opportunity core, and the first real funnel.
- Exactly one configurable pipeline preset, inbound services. Both release-one funnels, the real
  one and the synthetic event-referral fixture, route into it through different declarative
  mappings and different routing rules.
- A record-level deletion path for observations, parties, and opportunities (R5).
- Workspace export and restore, and verified migration of the predecessor memory store.
- The first web interface, ported.
- Module install and enable.
- Login through GitHub OAuth.

### Out of scope for release one

- The work-in-motion module. Opportunities reach a terminal disposition without creating work.
- The optional job-search workflow: no job boards, no candidate profile, no application
  preparation, no scoring rubric for employment fit.
- Module disable, remove, purge, and restore. The contract is written; the code is later.
- Any second pipeline preset, including a dedicated referral preset.
- Bulk erase, scheduled expiry outside the memory retention setting, and any reversal of a
  deletion (R5).
- Domain packs, the capability registry as a queryable product surface, and arbitrary hosted
  plugins.
- The second runtime adapter, Codex CLI validation, Telegram, and voice.
- The external back-office integration and the local node protocol.
- The hosted multi-tenant edition, per-workspace encryption, and key management.
- Any second storage backend, including the SQLite contract goal.
- Invitation flow, seat management, and roles beyond `owner` and `member`. The invitation flow
  lands in the framework-proof phase of the build plan.
- Bidirectional offline synchronization and multi-device concurrent writes.
- The contributor model. The licence itself was settled on 2026-09-17 as AGPL-3.0 (`LICENSE`),
  earlier than the framework-proof phase this document first planned for it.

## UI port inventory

D8 makes the interface a port. This inventory bounds it. A screen-by-screen audit of the two
predecessors is a phase-three deliverable; what follows is the surface grouping and the known
stack facts, at the granularity the idea document's legacy-to-new map already uses.

| Surface group | Source | Source stack | Carries over to | Port position |
| --- | --- | --- | --- | --- |
| Opportunity list and triage queue | Opportunity-discovery predecessor (MOL) | React/Vite over a FastAPI service, MySQL in production | Leads | Port in phase three. The list is the first real interface. Its filters and columns are the most legacy-shaped part of the port and need the hardest review. |
| Opportunity detail with source evidence | Same | Same | Leads | Port in phase three. Provenance display is a new requirement (FR 36), so this screen gains rather than loses. |
| Qualification and scoring view | Same | Same | Leads | Port in phase three, with the rubric generalized away from employment fit per FR 44. The scoring code itself is Python and is the main reason for D3. |
| Proposal drafting and answer library | Same | Same | Leads, job-search workflow | Deferred to phase four. Out of release one. |
| Source and connection settings | Same | Same | Leads, core | Port in phase three, reshaped around the generic connection contract rather than per-provider forms. |
| Work queue, board, and work-item detail | Work-in-motion predecessor (M.O.T.) | Next.js with Drizzle over `better-sqlite3` | Current | Deferred to phase five. Out of release one. |
| Memory browse, search, and entity views | Memory layer inside the work-in-motion predecessor | Next.js, `sqlite-vec` embeddings, FTS5 search | Recallatron | Port in phase two. The interface carries over; the retrieval layer beneath it does not (D9). |
| Application shell, navigation, theme | Both, divergently | React/Vite and Next.js | Core | The shell itself is not a port. Phase one builds it new on Next.js as part of the repository bootstrap, carrying only a login, a workspace switcher, and the routing configuration every link goes through. Phase two unifies navigation and theme on top of it: one token set, one theme, one navigation contract that modules contribute to. *Amended 2026-09-24, issue #40:* one token set and one navigation contract stand, but "one theme" is replaced by a themable contract: the token set is the versioned [theme token contract](../architecture/theme-contract.md), a theme is a data file of values for it, and phase two ships one seed built-in theme on it that a later UI phase replaces or extends. This is the only part of the interface work that is deliberately a rewrite rather than a port. |

What the renaming sweep touches, in every ported file, per FR 49 and guardrail 1:

- Component, file, route, and directory names carrying a legacy product name.
- Hard-coded API path prefixes, which must become routing-configuration lookups per FR 47.
- Environment variable names and configuration keys.
- Every user-facing string: page titles, navigation labels, empty states, error messages,
  tooltips, and document titles.
- Fixture and test data, which must be replaced with synthetic content rather than sanitized.
- Any embedded personal data, rate, client name, or private deployment detail, which is removed
  rather than renamed.

The port is not a licence to skip review. Guardrail 1 and the publication rules apply with
force here, because ported interface code is the most likely place for a legacy name or a real
client's name to reach a public repository.

## Edge cases and failure modes

Release one must behave correctly in each of these. They are the cases the build plan's
acceptance criteria test.

1. **A webhook arrives while nothing is listening for it conversationally.** No model session
   is running. The delivery must still be authenticated, stored, acknowledged, and processed.
   (FR 35.)
2. **The same delivery arrives twice, concurrently.** Two workers pick up the same source event
   identifier at the same moment. Exactly one observation and one processing effect result.
   (FR 34.)
3. **The same event identifier arrives with different content.** This is a conflict, not an
   update. It is surfaced and not silently applied. (FR 34.)
4. **A late observation carries older facts than the record already holds.** It must not
   overwrite newer evidence, and it must not overwrite a stage or note the user set by hand.
   (FR 37.)
5. **One person submits two different forms.** Two observations, one party if and only if the
   R4 evidence rules permit an automatic match, and not necessarily two opportunities. (FR 41.)
6. **Two different people at the same company submit inquiries.** A shared email domain is a
   hint, never a merge. Both are review candidates at most. (FR 41.)
7. **A merge turns out to be wrong.** Unmerge restores the prior state, and both original party
   identifiers still resolve. (FR 41.)
8. **A pipeline preset is edited while opportunities are mid-stage.** Existing opportunities
   keep the stage meaning of the configuration version they were created under until an explicit
   migration. (FR 43.)
9. **A caller presents a valid record identifier from another workspace.** Intake, tools, jobs,
   and retrieval all refuse. Knowing an identifier is not authorization. (FR 1, FR 23.)
10. **A model argument names a workspace.** Ignored. The workspace comes from the session.
    (FR 23.)
11. **An inbound form submission contains text instructing the agent to send something.** The
    text stays evidence. It cannot authorize an external action or change a safety class.
    (FR 25.)
12. **The agent runtime binary is missing, its credential has expired, its deadline passes with
    no answer, or its stream truncates mid-run.** Explicit non-success, no hang, no false
    completion. (FR 21.)
13. **A workflow requires structured output from a model that cannot produce it.** Rejected
    before the workflow starts, not discovered at parse time. (FR 20.)
14. **The process dies between a state change and its event delivery.** The outbox makes them
    one transaction; the worker redelivers; the consumer deduplicates. (FR 15.)
15. **A webhook connection's signing secret is revoked while a delivery it signed is still
    queued.** The queued processing rechecks the connection at execution and fails closed with no
    observation created; a later delivery signed with the old secret is refused before any
    receipt is stored; both refusals are visible on the connection's health. (FR 31, FR 38,
    R3 item 4.)
16. **Contact permission is withdrawn after a memory was derived from the contact's data.**
    Retained memory does not bypass the restriction, and derived summaries and embeddings are
    invalidated along with the source. (FR 27, FR 28.)
17. **The memory module was never installed or enabled in the workspace.** The whole
    intake-to-outcome path still completes. Disabling an enabled module is not a release-one
    operation (D5), so this is the absence case rather than the disable case. (FR 26.)
18. **A developer runs the demo with no private configuration at all.** It works, on synthetic
    data, writing nothing into the checkout. (FR 14.)
19. **A self-hoster has one hostname and no wildcard certificate.** Single-host path mode is the
    default and is fully functional. (FR 47.)
20. **A ported screen still contains a legacy name or a real client's name.** The publication
    check and the review gate catch it before it reaches the public repository. (FR 49, FR 50.)

## Assumptions

Each with its source. An assumption without a source is an open question, not a fact.

| Assumption | Source | Note |
| --- | --- | --- |
| The maintainer already operates a site with an inbound-inquiry form that can post a webhook | Maintainer decision, recorded before this document; idea document's custom-funnel requirement | The first real funnel. Never named in public documentation. |
| That form supplies neither an authenticated subject identifier nor a source-verified email address | Implied by R4 | ASSUMED default: neither, until the form's own behaviour is confirmed. Under R4 that makes the first funnel review-candidate-only for party matching: a second inquiry from the same person creates a second party and a review candidate rather than an automatic link. If the form later gains a confirmation step, its verified email becomes automatic-match evidence with no change to the contract. |
| The opportunity-discovery predecessor runs FastAPI plus React/Vite on MySQL | Maintainer decision, recorded before this document; verified in source | Sizes the port and makes it a MySQL-to-Postgres move. |
| The work-in-motion predecessor runs Next.js with Drizzle over `better-sqlite3` | Maintainer decision, recorded before this document; verified in source | Sizes the port. |
| The memory layer uses `sqlite-vec` and FTS5, not pgvector | Maintainer decision, recorded before this document; verified in source | The reason D9 exists. |
| The maintainer prefers Python for domain services | Maintainer decision, recorded before this document | One of four inputs to D3; not the only one. |
| The maintainer chose the memory module as the first module after the walking skeleton | Maintainer decision, recorded before this document | The source for D11. The rationale in D11's row is this document's account of why the direction holds, not the origin of the direction. |
| The maintainer accepts `ClaudeCliRuntime` first and the OpenRouter adapter as a v1.0 requirement rather than a first-slice one | Maintainer decision recorded in the idea document's decision ledger | The source for the first entry under [Recorded changes of direction](#recorded-changes-of-direction). Without it, D4's rollout order is a preference with no recorded origin. |
| The maintainer wants module boundaries visible in the address bar of the instance they use daily | Maintainer decision recorded in the idea document's decision ledger | The source for the second entry under [Recorded changes of direction](#recorded-changes-of-direction). It buys a routing table, not a deployment per module, and single-host path mode stays the default so guardrail 15 is unaffected. |
| The reference instance is a single-server container deployment behind a reverse proxy with a wildcard certificate | Maintainer decision, recorded before this document | Makes guardrail 15 self-enforcing. |
| Postgres extensions for vector and text search are available in the deployment | Implied by D1 and D9 | ASSUMED default: a standard Postgres image with pgvector available. If a target deployment cannot supply it, the retrieval adapter absorbs the change (FR 30). |
| The first users hold a GitHub account | Implied by D6 | ASSUMED default. The identity-provider boundary exists precisely because this stops being true for later personas. |
| Release one has one human user | Maintainer decision, recorded before this document (one-person start) | Drives R1's scope, not R1's structure. |
| `rheo.stream` is registered and available for the reference deployment's subdomains | Idea document | Wildcard DNS and certificate are a deployment prerequisite for subdomain mode only. |

## Disposition of every idea-document open question

All 24, none dropped.

| # | Question | Disposition |
| --- | --- | --- |
| 1 | Workspace, membership, role, invitation model | **Answered here**, R1. |
| 2 | Which modules use SQLite, Postgres/RLS, or a hybrid in the first release | **Answered here**, D1 and D2, for release one: all modules on Postgres, database per workspace, small shared control plane. The hosted edition's topology remains open. |
| 3 | Encryption threat model and key management for the hosted edition | Deferred to the hosted-edition phase and to the security, tenancy, and data-lifecycle specification. Not release one. |
| 4 | Which first release slice proves custom-funnel intake and optional job search | **Answered here**, R2. Job search is explicitly not in the slice. |
| 5 | Which proposal-generation capabilities belong in the job-search workflow's initial release | Deferred to the optional job-search workflow phase. |
| 6 | Exact event schemas, ordering rules, retry limits, replay contracts | Deferred to the architecture specification. FR 15 to FR 17 fix the required properties; the specification fixes the shapes. |
| 7 | What the local node protocol permits and how capabilities are revoked | Deferred to the back-office integration phase. Not release one. |
| 8 | How much back-office integration can be built against supported upstream interfaces | Deferred to the back-office integration phase. |
| 9 | Which job-source providers can legally and economically be enabled in a hosted service | Deferred to the hosted-edition phase, and blocked on the licence decision. |
| 10 | Which runtime ships first, which is the hosted default, what checks each must pass | **Answered here**, D4, for rollout order. The hosted default remains open until the hosted-edition phase. The capability checks are fixed by FR 20 and designed in the architecture specification. |
| 11 | Confirmation policy for messages, submissions, destructive changes, financial actions | **Answered here**, R3, as policy shape and safety classes. Final rules deferred to the architecture specification. |
| 12 | What data is sent to model providers; redaction, retention, user control | Deferred to the architecture specification's security section. FR 13 and FR 27 fix the boundary; the specification fixes the redaction contract. |
| 13 | How export/import, schema migration, and disaster recovery are tested | Partly answered by FR 52 and FR 53: export and restore is required and is tested at the end of each release-one phase, and a predecessor migration is verified before switchover. The full disaster-recovery test plan is deferred to the architecture specification. |
| 14 | Which open-source licence and contributor model | Licence **answered 2026-09-17: AGPL-3.0** (`LICENSE`, SPDX `AGPL-3.0-only`; recorded in the idea document's decision ledger). The contributor model stays deferred to the framework-proof phase, before a v0.1 release and before accepting substantial outside contributions. |
| 15 | Minimum useful direct-voice experience, and whether voice waits | Deferred to the channels phase. Direction recorded: voice waits until text interaction and the permission model are stable. |
| 16 | Which intake transports and which two unrelated custom-funnel examples validate the contract | **Answered here**: three transports in FR 32; the two funnels are the maintainer's own inbound-inquiry form and a synthetic event-referral funnel exercised as a fixture, per the opportunity-core phase. |
| 17 | What users can configure in pipeline presets, and how records migrate on change | Deferred to the architecture specification. FR 43 fixes the required property (pinned configuration versions); the specification fixes the limits and the migration operation. |
| 18 | Minimum relationships contract, CRM field ownership, automatic match and merge evidence | **Answered here**, R4. |
| 19 | Permission purposes, recipient scopes, suppression, retention, external correction and deletion | Partly answered. The deletion half is **answered here**, R5 and FR 51: release one has a record-level deletion path for observations, parties, and opportunities. Permission, suppression, and retention are answered by FR 46 and FR 29. The full set of purposes, the recipient-scope vocabulary, and the external correction flow are deferred to the architecture specification. |
| 20 | What belongs in the minimum core, which capabilities become shared modules, which dependencies the first configuration needs | Partly answered here: the core surface is the six responsibilities in the idea document's shared-infrastructure table; relationships is the only shared business module in release one; the first configuration depends on relationships, Leads, and optionally the memory module. Boundary detail deferred to the architecture specification. |
| 21 | Module manifest, capability compatibility, registration, provenance, contract-test requirements | Deferred to the architecture specification, per D5: the full contract is written there, and release one implements install and enable only. |
| 22 | How domain packs compose and upgrade without overwriting workspace choices | Deferred to the framework-proof phase. Domain packs are out of release one. |
| 23 | How detached records are read and restored, migrations recovered, jobs reconciled on removal | Deferred to the module-lifecycle milestone, per D5. |
| 24 | Private data paths, member scopes, secret stores, packaging allowlists, publication checks | Partly answered by FR 10 to FR 14, and the private-data-path and publication-check halves are enforced from the walking-skeleton phase. Deferred to the architecture specification: the packaging allowlist detail, the configuration precedence rules themselves, and the choice of secret store, which this document does not name. |

## The 26 guardrails in this document

The idea document's guardrails are constraints on the whole project. Here is where each one
lands: **R** means release one carries it as a requirement with an acceptance criterion; **A**
means it is an architecture-specification obligation; **L** means a named later phase owns it.

| Guardrail | Lands as | Where |
| --- | --- | --- |
| 1. No legacy names in new public contracts | R | FR 49, FR 50, and the UI port inventory's renaming sweep |
| 2. Workspace context enters at the boundary | R | FR 1 |
| 3. No user-selected database paths or tenant identifiers | R | FR 2 |
| 4. Domain services do not depend directly on Claude | R | FR 19 |
| 5. The MCP server calls services, not databases | R | FR 22 |
| 6. Modules do not write one another's storage | R | FR 11 |
| 7. Storage-specific SQL stays behind repositories or adapters | A | FR 8 states the seam as a requirement, but release one has no acceptance criterion for it (the build plan's coverage table records FR 8 as untested); D2 makes proving a second backend an architecture obligation first and the later local-first edition's work second |
| 8. Every workspace tracks composition and schema versions | R | FR 9 |
| 9. Identifiers are globally safe | A | Identifier scheme is an architecture-specification deliverable |
| 10. Source provenance is first-class | R | FR 36, FR 37 |
| 11. Side effects are confirmable and auditable | R | R3, FR 18, FR 24 |
| 12. Long-running work is resumable or observable | R | FR 16, FR 17 |
| 13. Channels share one authorization model | A + L | The authorization model is fixed by FR 1 and FR 23 now; the channels phase proves it with a second surface |
| 14. Secrets are scoped to the narrowest component | R | FR 13, tested in phase one against the model credential and the OAuth client secret |
| 15. Self-hosted is continuously exercised | R | D10, FR 48, and single-host mode as the default in FR 47 |
| 16. The back-office integration remains independently replaceable | L | Back-office integration phase |
| 17. Funnels do not define the core schema | R | FR 44 |
| 18. Identity is resolved at the right level | R | R4, FR 34, FR 41 |
| 19. Configuration changes are versioned | R | FR 43 |
| 20. Accepted intake and completed outcomes are different | R | FR 33, FR 45 |
| 21. Derived context inherits access and retention | R | FR 27, FR 28 |
| 22. The core has no profession-specific rules | R | FR 44, and the core surface named in question 20's disposition |
| 23. Module removal is a supported workflow | L | Module-lifecycle milestone, per D5 |
| 24. Shared business records do not require Leads | R | FR 40 |
| 25. Personalization is private data | R + A | FR 13 and FR 50 carry criteria; FR 12's precedence rules and the operator's policy floor are tested by criterion 69 of the build plan (added append-only during phase-one detailed planning); the mechanism remains an architecture-specification deliverable |
| 26. Runtime output stays outside the source tree by default | R | FR 10, FR 11 |

## What this document does not decide

Deliberately absent, and owned by the architecture specification: database schemas, API
endpoint shapes, event payload contracts, MCP tool signatures, the identifier scheme, the
module manifest format, the redaction contract, the pipeline preset configuration limits, and
whether the per-workspace storage unit under D1 is a database or a schema, which changes the
migration orchestration and the connection routing but no requirement here. The licence, once
also absent here, was settled on 2026-09-17 as AGPL-3.0 (`LICENSE`).

The phased plan, with per-phase goals, scope boundaries, and numbered acceptance criteria,
is in the [build plan](build-plan.md).
