# The rheo agent and its channels

## What it is

rheo is the agent that operates across the stream. It searches, summarizes, proposes,
remembers, and carries out approved actions, using the tools the enabled modules expose
through the [MCP door](mcp-and-runtimes.md). It is a conversational identity, not a system
of record: everything it changes lives in a module's tables, under that module's rules.

People reach rheo through several surfaces:

- Interactive Claude: Claude Code or Claude on the web, with rheoStream connected as
  an MCP server. rheo's tools come from the door; Claude supplies the conversation.
- Telegram: asynchronous, mobile access, in a private chat or a group.
- Direct voice: speech recognition and synthesis wrapped around the same session.
- The web shell: where you read and approve what rheo proposed.

These are channels into one agent, not separate bots with diverging abilities.

## What you can do with it

- Ask rheo what came in today, what is overdue, or what you decided about a client last
  spring, from whichever surface is closest.
- Have it capture an inquiry, qualify an opportunity, schedule a follow-up, create a work
  item or remember a decision.
- Have it propose something with real consequences (sending, handing off, deleting) and
  get back an approval request instead of a finished action.
- Approve that request in the web shell, or with a confirmation in the channel by the
  enrolled account.
- Run rheo on the runtime and model your workspace chooses, from the lists the operator
  allows.

## How it works

### A channel is a boundary adapter

Like the web session, a token, a job and an intake connection, a channel builds a
`WorkspaceContext` before it calls anything. It resolves the person through an
authenticated enrollment record: which channel, which external conversation, which
account, which workspace, and which audience. The context it builds carries `entry =
channel` and `audience = channel:<conversation>`, and then calls exactly the same services
as every other door.

### A channel never has more power than the web

No channel gets an operation a web session lacks. Telegram and voice cannot become back
doors around the policy that governs everything else.

### Audiences keep conversations apart

A private chat and a group chat are different audiences. Channels share identity and
policy, not transcripts. A runtime session bound to one audience is never resumed for
another, and no adapter ever resumes a global "latest" session. Memory follows the same
model: a channel audience is one more value of a memory's audience, not a new mechanism.

### Approvals come back as messages

When rheo calls a gated operation from a channel, the `approval_required` answer arrives
as a message carrying the approval id. Approval itself happens in the web shell, or
through a channel confirmation that is an authenticated action by the enrolled account
(never a token), recorded with `approved_entry = channel`.

### Revocation reaches queued work

Revoking a channel enrollment revokes the queued work bound to that audience, not only the
next message.

### Voice


```text
speech -> transcription -> rheo session -> MCP and domain action -> response -> speech
```

The voice provider is not an independent reasoning agent with its own tool behaviour. Text
and voice may render answers differently, but they share permissions, memory,
confirmations and action history.

### The runtime is a setting

rheo's reasoning runs on a runtime adapter: the Claude CLI for personal installations, the
OpenRouter API with a controlled provider policy, or Codex CLI, held to the same
capability gate and contract suite. Runtime and model are separate choices. Switching
runtimes starts a new native session with the selected authorized context; nothing carries
across adapters, and completed effects are never replayed.

### One surface keeps rheo's context small

The design goal is to bring as many of the operator's agent services as possible behind
the one MCP door: a module replaces a service outright, a channel module takes over a chat
service such as Telegram, or a connector proxies an existing external MCP server. What
costs context in every session is the number of tool schemas a client sees, and per-token
grants and discover-then-call keep that number small.

### Where rheo's changes live

rheo reads and writes through operations. Its replies are not proof: an operation record
reaches `succeeded` only with a recorded check of what actually happened, and imported
text, retrieved memories and messages are untrusted evidence that can never grant a tool,
change policy or approve an action.

## Why it's built this way

The point of one agent with several surfaces is continuity. You start something at your
desk in Claude, check on it from your phone in Telegram, and ask about it by voice in the
car, and it is the same rheo with the same memory and the same limits each time.

That only works if the limits really are the same. Building each channel as one more
boundary adapter over the same services, rather than a bot with its own logic, is what
gives a Telegram message the same checks as a web click.

## Where to look

- Specification: [rheo: agent and interaction model](../ideas/rheo-stream-idea.md#rheo-agent-and-interaction-model),
  [channels and the second runtime](../architecture/later-phases.md#phase-6-channels-and-the-second-runtime),
  [the workspace context](../architecture/overview.md#the-workspace-context),
  [runtime contract](../architecture/runtime-and-mcp.md#runtime-contract-version-1)
- Code: [channels boundary](../../channels),
  [runtime adapters](../../runtimes),
  [boundary factories](../../packages/core/src/rheo_core/boundary/factories.py),
  [MCP facade](../../apps/mcp/src/rheo_app_mcp)
- Tests: [runtime gate](../../tests/test_runtime_gate.py),
  [continuation binding](../../tests/postgres/test_runtime_continuation_binding.py),
  [runtime orchestrator](../../tests/postgres/test_runtime_orchestrator.py),
  [boundary](../../tests/test_boundary.py)
