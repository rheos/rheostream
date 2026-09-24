# rheoStream

An open, self-hostable framework for composing agent-assisted working environments.

> Leads enter the stream. Current carries the work. Recallatron remembers.

rheoStream is for independent professionals whose working life is spread across
too many tools. You assemble a workspace from modules: **Leads** develops
opportunities from whatever sources feed you work, **Current** carries active
commitments and projects, and **Recallatron** keeps the context that would
otherwise be lost between sessions. An agent named **rheo** operates all of it
in conversation, over Claude, Telegram, or voice. Conversation is the interface,
not the database: the modules stay explicit systems of record that you can
inspect, export, and own.

The first reference configuration serves a freelance web developer and software
entrepreneur. The same contracts are meant to carry other professions: an HR
consultant, a regulatory consultant, a sales representative, or a business
routing inquiries from its own websites and forms. Job search is one optional
workflow; a workspace can feed Leads from its own funnels and referral partners
and never touch a job board.

**Status: the walking skeleton is built.** A Python core (FastAPI)
and a Next.js web shell run against Postgres, one database per workspace.
Identity sign-in is GitHub OAuth behind a pluggable provider boundary;
sessions are host-only cookies with a one-time identity-host grant; the
core issues its own tokens under a non-token-issuable rule enforced at both
ends; routing supports path-based and subdomain topologies from one config;
an operator CLI (`rheo account`, `workspace`, `member`, `token`, `routing`,
`doctor`) and the first seam of the MCP façade both exist. Durable background
work now runs: a worker leases jobs with `SKIP LOCKED`, runs the handler, and
applies retry with backoff, terminal failure, or cancellation — surviving the
process dying mid-job, because a lease expires rather than a crash losing the
work. A state change and its outgoing event now commit in one transaction, the
worker redelivers after a crash between the commit and the delivery, and the
consumer deduplicates, so an event arrives exactly once as the consumer
observes it. Every long-running operation returns an identifier before it
finishes and reports one of a fixed set of terminal statuses, including an
explicit unresolved. Every mutating operation writes an audit record naming
actor, workspace, operation, and time, and an operation registered with no
audit path fails registration at startup rather than quietly writing nothing.
Enqueueing is still not atomic end to end: the job row lands in the workspace
database and its due mark in the control database, with no transaction
spanning the two, so a crash between them leaves a job undue until
reconciliation. The reference compose topology runs that worker as its own
service. **Recallatron is the first product module underway.** Memory records exist
with `remember`, `derive`, `correct`, and `supersede`, plus retention and
eligibility rules, and export/restore. Recall now selects between lexical,
dense, and hybrid retrieval strategies — hybrid is the default, fusing lexical
and dense results, and degrades cleanly to lexical-only when no embedding
provider is configured. Embeddings run in-process (fastembed MiniLM-L6-v2),
with no credential and no network call. A read-only `dedup_candidates`
operation proposes near-duplicate memory pairs. A real deployment cannot yet
configure the embedding provider — a core settings-load-order defect keeps
production on lexical-only until that's fixed. Leads and Current are still
empty stubs, and the durable layer is still not finished: there are no
scheduled jobs. The module-manifest design is settled;
disable, remove, purge, and restore are not built. The license is AGPL-3.0.
Directory names under `modules/`, `connectors/`, `channels/`, `runtimes/`,
and `packs/` still mark intended boundaries, not implemented features; `apps/`
and `packages/` no longer do.

Start with the [idea document](docs/ideas/rheo-stream-idea.md). It sets out the
product thesis, the architecture direction, and a decision ledger that keeps
settled choices separate from open questions. The
[requirements and scope](docs/requirements/requirements-and-scope.md) and the
[build plan](docs/requirements/build-plan.md) propose what the first release
does and in what order.

## Why this exists

The project grows out of tools already in daily use by one working freelancer:
an opportunity triage system, a ticket desk, a memory service, and a collection
of agent routines. Each is useful; together they are a pile of dashboards with
history trapped in each one. rheoStream reconstructs the useful parts as one
system with clear domain boundaries, portable data, and a single agent interface,
built so that other people can run it too.

## How it is put together

A small framework core provides workspaces, permissions, module lifecycle, and
durable background work. Modules own their own records
and cooperate through
versioned contracts and events; none writes another's tables. rheo reaches the
system through one MCP facade with goal-level tools, and every call is checked
against the caller's workspace and permissions. Actions that affect the outside
world (sending, submitting, paying) require explicit policy and leave an audit
trail.

Agent execution is a replaceable adapter. Claude Code in print mode and the
OpenRouter API are the planned runtimes, with Codex CLI as a further target; no
module may assume a particular model or vendor.

```text
apps/                      Application entry points and interface adapters
packages/core/             Shared framework infrastructure
packages/contracts/        Versioned public contracts and module interfaces
modules/
  leads/                   Opportunity development
  current/                 Commitments and active work
  recallatron/             Permission-aware durable memory
  relationships/           Shared people and organization records (proposed)
connectors/                External source and destination adapters
channels/                  Conversation surfaces such as text and voice
runtimes/                  Replaceable agent execution adapters
packs/                     Reusable domain packs; freelance software work first
docs/                      Idea, requirements, and architecture records
examples/                  Synthetic examples only
tests/                     Contract, integration, and acceptance tests
scripts/                   Repository checks and development tooling
deploy/                    Generic deployment templates; compose topology
```

The preferred starting implementation is a modular monolith: logical boundaries
without premature microservices.

## Public code, private data

The repository contains reusable code, definitions, and synthetic examples.
Profiles, rates, client records, prompts, credentials, conversations, and
databases belong in private workspace storage that Git never sees. For
development, the public checkout sits inside an unversioned parent folder next
to its private siblings:

```text
rheo-stream-workspace/     Editor workspace; no Git repository here
  rheo-stream/             Public Git repository; build and publication root
  private/                 Local configuration, documents, agent context
  workspaces/              Private runtime data
  backups/                 Private backups
```

Only `rheo-stream/` is versioned. The [workspace layout guide](docs/workspace-layout.md)
describes the arrangement and its boundaries, and the idea document records the
[data placement rules](docs/ideas/rheo-stream-idea.md#private-data-placement-in-each-deployment-mode)
and [publication requirements](docs/ideas/rheo-stream-idea.md#public-repository-contents-and-private-workspace-contents).
The same rules will apply to a hosted edition: local ownership and hosted
convenience are both intended deployment modes.

## Repository checks

With Git and Python 3.9 or newer installed:

```sh
python3 scripts/check_repository.py
```

This verifies private-path exclusions, tracked artifacts, and local Markdown
link targets. It also runs in GitHub Actions. It is not a secret scanner and
does not replace review of public content.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing the architecture or
importing code. Local `AGENTS.md` and `CLAUDE.md` files are ignored on purpose;
internal build guidance stays in private agent context, as described in the
[workspace guide](docs/workspace-layout.md#private-instructions-and-new-sessions).

rheoStream is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(SPDX `AGPL-3.0-only`). Operators who convey a modified version must meet the
applicable Corresponding Source requirements. If that version supports remote
network interaction, they must offer Corresponding Source to those remote users
at no charge. The commercial model is still open.

Project home: [rheo.stream](https://rheo.stream).
