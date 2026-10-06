# rheoStream

rheoStream runs agent-accessible workspace operations, background jobs and
persistent memory through a self-hostable FastAPI core, MCP facade and Next.js shell.

## Mechanisms you can inspect

- Each workspace has its own Postgres database. Server-resolved identity selects
  the database; an operation's payload cannot redirect it to another workspace.
- Jobs persist with leases and terminal operation records. A transactional outbox
  tracks event delivery, with consumer deduplication and retry.
- Successful mutations commit with their audit records. Field sensitivity tiers
  control redaction before data reaches a model.
- Recallatron stores memories with `remember`, `derive`, `correct` and `supersede`.
  Hybrid retrieval combines lexical search with local embeddings.
- Public code and synthetic examples stay in Git; private workspace data stays
  outside it. Workspace export and restore are implemented.

## Current status and limits

The core, worker, shell, Recallatron, module install/enable and Claude CLI adapter
are implemented. Relationships owns shared party identity; Leads supports durable
intake, configurable opportunity pipelines, permissions and tracked handoffs through
operations and MCP. Their UI and authenticated intake transports remain separate
work. Current, connectors, channels, packs and module disable/remove/purge are
unimplemented. Modules are trusted code,
not sandboxed plugins. Job enqueue spans two databases without a shared transaction:
a crash between writes can delay discovery until reconciliation. The
[architecture tour](docs/architecture/code-tour.md) documents recovery and its limits.

## Try this in 5 minutes

Check the public/private repository rules. Requires Git and Python 3.9+;
no database, model account or API key is needed.

```sh
git clone https://github.com/rheos/rheostream.git
cd rheostream
python3 scripts/check_repository.py
```

The check validates private-path exclusions, tracked artifacts and local Markdown
links. It does not scan file contents for secrets or exercise the running app.
For the Docker stack, follow the [deployment guide](deploy/README.md).

## Follow the evidence

Start with the [architecture tour](docs/architecture/code-tour.md): trace one
request through authorization, storage routing, audit and the worker.
Its test links cover workspace isolation, failed commits and missed due-work marks.
The [decision index](docs/architecture/README.md) records architecture choices;
the [workspace guide](docs/workspace-layout.md) defines the public/private layout.

## Status

**Release one is two phases in, and the reference instance is live.** Phase one (the
walking skeleton) and phase two (Recallatron) are merged. On 2026-09-30 the reference
instance finished phase two's last piece: the predecessor memory service's data moved into
Recallatron in one accounted transfer, and Recallatron became the only memory writer.
Automatic memory now saves from enrolled Claude Code sessions, and clients recall through
the one MCP surface. Phase three (Leads) is next. The license is AGPL-3.0.

**The flagship runs on `rheo.stream`.** One Coolify-managed server hosts it in subdomain
mode, with a Let's Encrypt wildcard certificate: `circuit.` (the shell), `auth.` (GitHub
sign-in), `recall.` (Recallatron's screens), `api.` and `mcp.`. The old `recallatron.` host
301-redirects to `recall.`. A green push to `main` redeploys it once every CI workflow on
that commit has passed. [deploy/README.md](deploy/README.md) covers the topology, rollback,
the connection budget and the nightly backups.

**What the framework does today.**

- A Python core (FastAPI), a durable worker and a Next.js web shell, on Postgres with one
  database per workspace.
- GitHub OAuth behind a pluggable provider boundary, host-only session cookies with a
  one-time identity-host grant, and core-issued `cli`, `mcp` and `runtime` tokens. A new
  session opens in the workspace the account used last.
- Path-based and subdomain routing from one configuration.
- An operator CLI: `rheo account`, `workspace`, `member`, `token`, `routing`, `doctor`.
- The MCP facade, mounted in core over streamable HTTP.
- Durable work: leased jobs with retry, cancellation and scheduled jobs; a transactional
  outbox with consumer deduplication; retry, skip and replay for failed deliveries.
- An operation record with a terminal status for every long-running call, and an audit
  record for every mutating one.
- Redaction on everything bound for a model, by field sensitivity tier.
- Module install and enable, a web contribution contract, and a declarative theme
  contract with one seed dark theme.
- Workspace export and restore.
- A Claude CLI runtime adapter.

**Recallatron** is the first product module. Memory records support `remember`, `derive`,
`correct` and `supersede`, with audience and purpose rules and opt-in retention. Recall is
lexical, dense or hybrid (the default), with in-process embeddings (fastembed
MiniLM-L6-v2) that need no credential or network call. The flagship runs the local
embedding provider. Read-only screens cover browse, search, item detail and possible
duplicates.

**Not built yet.** Leads, Current and Relationships are stubs. Module disable, remove and
purge are designed but not built. Enqueueing a job still spans two databases (the job row
in the workspace database, its due mark in the control database), so a crash between them
leaves the job waiting for the reconcile pass. `connectors/`, `channels/` and `packs/` mark
intended boundaries with no implementation behind them.

Start with the [idea document](docs/ideas/rheo-stream-idea.md). It sets out the
product thesis, the architecture direction, and a decision ledger that keeps
settled choices separate from open questions. The
[requirements and scope](docs/requirements/requirements-and-scope.md) and the
[build plan](docs/requirements/build-plan.md) set out what the first release
does and in what order; both are accepted.

## Why this exists

The project grows out of tools already in daily use by one working freelancer:
an opportunity triage system, a ticket desk, a memory service, and a collection
of agent routines. Each is useful; together they are a pile of dashboards with
history trapped in each one. rheoStream reconstructs the useful parts as one
system with clear domain boundaries, portable data, and a single agent interface,
built so that other people can run it too.

## How it is put together

Start with the [architecture tour](docs/architecture/code-tour.md): one request
through the code, the workspace-storage decision, and a known enqueue limitation
with links to the tests.

A small framework core provides workspaces, permissions, module lifecycle, and
durable background work. Modules own their own records
and cooperate through
versioned contracts and events; none writes another's tables. rheo reaches the
system through one MCP facade with goal-level tools, and every call is checked
against the caller's workspace and permissions. Actions that affect the outside
world (sending, submitting, paying) require explicit policy and leave an audit
trail.

Agent execution is a replaceable adapter. The Claude Code print-mode adapter is
built; the OpenRouter API is the next planned runtime, with Codex CLI as a further target; no
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
deploy/                    Local compose stack, flagship overlay, backup script
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
