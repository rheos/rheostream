# How rheoStream works, by design

rheoStream is an open framework for agent-assisted work. It is one system you own: a web
shell, an MCP door for agents, a durable worker, and a Postgres database for each
workspace. On top of that small core sit four modules. Leads develops opportunities,
Current carries active work, Recallatron remembers, and Relationships keeps the people
and organizations they all share. An agent named rheo operates all of it, from Claude,
from Telegram, or by voice.

> Leads enter the stream. Current carries the work. Recallatron remembers.

This guide describes the system as designed, one part per page. Each page says what the
part is, what you can do with it, how it works, and why it is built that way. Each ends
with links to the specification, the code and the tests. The
[architecture specification](../architecture/README.md) is the authority; this guide is
the readable route into it.

## The problem it solves

Working alone or in a small studio means living in a pile of tools: something that finds
work, something that tracks it, something that remembers, and a few agent routines
stitched between them. Each one is useful. Together they trap history: the lead knows
nothing about the project it became, the memory knows nothing about either, and the
agent has a different door into each.

rheoStream rebuilds those parts as one system with clear boundaries between domains,
data you can carry out in one file, and a single interface an agent can use. A lead
found today becomes tomorrow's project, and the decisions, people and promises around it
are still there months later.

## The shape

```text
  Claude · Telegram · voice          web shell           your funnels and tools
            │                            │                         │
       rheo runtime                 internal API            connectors, intake
            │                            │                         │
        MCP door                         │                         │
            └────────────────────────────┼─────────────────────────┘
                                         │
                core: boundary → WorkspaceContext → service registry
                    (operations, safety classes, audit, outbox)
                                         │
             modules: Leads · Current · Recallatron · Relationships
                                         │
              one Postgres database per workspace + a control plane
                                         │
                   worker: jobs, event delivery, schedules
```

Four processes run it: `core` (the FastAPI application, which also serves the MCP door),
`worker` (the same image, running jobs and event deliveries), `web` (the Next.js shell)
and `postgres`. A reverse proxy sits in front. There is no message broker and no
workflow engine. Ordinary tables, an outbox, a job table and an audit table carry
everything.

## One inquiry, start to finish

Follow a single website inquiry through the system and every part shows up once.

1. Your website's server signs the form submission with an HMAC key and posts it to the
   intake route. The receiver checks the connection, the timestamp and the signature
   before it writes anything. ([Connectors](connectors.md))
2. In one transaction Leads stores a receipt keyed by the sender's event id, keeps a
   bounded copy of the payload, and writes an outbox event. Only after that commit does
   the sender get its `202`. A retry with the same event id is a duplicate, not a second
   lead. ([Leads](leads.md), [durable work](durable-work.md))
3. The worker leases the event and processes it: it applies the connection's pinned
   field mapping, creates an observation with per-field provenance, and asks
   Relationships who this person is. Only authenticated evidence links to an existing
   party; a name or an unverified email becomes a review candidate.
   ([Relationships](relationships.md))
4. Routing rules create an opportunity in a pipeline, or attach the observation to an
   open one for the same person. No model has been involved yet, and none needs to be.
5. You ask rheo, in Claude or Telegram, what came in today. It reaches the system
   through the MCP door with a token scoped to one workspace and one set of operations.
   It reads the opportunity, records a qualification, and drafts a follow-up. The draft
   is a record; nothing leaves. ([The MCP door](mcp-and-runtimes.md),
   [rheo and channels](rheo-and-channels.md))
6. rheo proposes handing the opportunity to Current as new work. Creating that work is an
   external-class step, so it stops at `approval_required`. You approve in the web
   shell; the approval is bound to that exact payload, and guards recheck permissions
   before it runs. ([Safety and approvals](safety-and-approvals.md), [Current](current.md))
7. Recallatron remembers why you took the work. The memory carries its audience and
   purposes, links back to the opportunity, and disappears if that opportunity is ever
   erased. ([Recallatron](recallatron.md))
8. Every mutation along the way committed with its audit row. Export the workspace and
   all of it travels in one artifact, with the same identifiers, ready to restore
   somewhere else. ([Export, restore and deploy](export-restore-deploy.md))

## The ideas that run through everything

**The workspace is the unit.** Each workspace is its own Postgres database. A connection
opened for one workspace cannot read another's rows, and export, backup and deletion all
act on that one unit. ([Workspaces and storage](workspaces-and-storage.md))

**Every surface uses the same door.** The web shell, the MCP door, the HTTP API, intake
and the worker all build the same server-resolved `WorkspaceContext` and call the same
registered operations. None of them contains a branch on domain meaning.

**Work survives crashes.** A state change and its event commit together. Jobs run under
leases, deliveries are recorded per consumer, and a crash re-runs work instead of
doubling it. ([Durable work](durable-work.md))

**Nothing leaves without a person.** Destructive, external and financial operations need
an approval bound to the exact call. No token of any kind can approve, so a model can
propose an action and never approve it. ([Safety and approvals](safety-and-approvals.md))

**Memory carries its own permissions.** A memory stores who may read it and for which
purposes. A memory derived from two others gets the narrower of the two, and erasing a
source erases what was made from it. ([Recallatron](recallatron.md))

**Modules own their records.** Each module has its own schema and talks to the others
through operations and events. The core imports no module, and the test suites pass in a
workspace where a module was never installed.

**Models are replaceable.** A runtime adapter runs the model's conversation loop;
rheoStream keeps identity, permissions, data, audit and durable state. Claude CLI,
OpenRouter and Codex CLI fit the same contract. ([The MCP door](mcp-and-runtimes.md))

## The pages

| Page | What it covers |
| --- | --- |
| [Workspaces and storage](workspaces-and-storage.md) | A database per workspace, the control plane, routing from authenticated context, settings and secrets |
| [Durable work](durable-work.md) | Leased jobs, the transactional outbox, operation records, audit |
| [Safety classes and approvals](safety-and-approvals.md) | The six classes, approvals bound to one call, guards, standing grants |
| [The MCP door and replaceable runtimes](mcp-and-runtimes.md) | One MCP surface, scoped tokens, redaction, connector sign-in, runtime adapters |
| [Recallatron](recallatron.md) | Memory with audience and purposes, hybrid recall, correction and supersession |
| [Leads](leads.md) | Intake from any funnel, observations, pipelines, qualification, handoffs, contact permission |
| [Current](current.md) | Work items, projects, dependencies, snooze, source dedup, handoffs in |
| [Relationships](relationships.md) | Shared parties, authenticated matching, reversible merges |
| [The rheo agent and channels](rheo-and-channels.md) | One agent across Claude, Telegram and voice, one authorization model |
| [Connectors](connectors.md) | Intake transports, outbound MCP connectors, destinations |
| [The Claude Code bridge](claude-code-bridge.md) | Automatic memory from your Claude Code sessions, and recall on the first prompt |
| [Web shell and CLI](web-shell-and-cli.md) | The composed shell, sessions across hosts, themes, the `rheo` operator command |
| [Export, restore and deploy](export-restore-deploy.md) | The workspace artifact, erasure, backups, the four-container deployment |

## Where to look

- Specification: [architecture index](../architecture/README.md),
  [system overview](../architecture/overview.md),
  [idea document](../ideas/rheo-stream-idea.md)
- A request through the code: [architecture tour](../architecture/code-tour.md)
- Code: [core](../../packages/core/src/rheo_core), [contracts](../../packages/contracts/src/rheo_contracts),
  [modules](../../modules), [entry points](../../apps)
- Tests: [test suites](../../tests), [acceptance evidence](../acceptance/README.md)
