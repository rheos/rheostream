# Connectors

## What it is

A connector authenticates to a source or a destination, translates its contract, and
tracks delivery. It holds no domain rule. That separation is one of four extension
concepts the design keeps apart:

| Extension | Responsibility | Example |
| --- | --- | --- |
| Connector | Talk to a source or destination and translate its contract | Signed webhook, file import, email poller, job-board feed, CRM adapter |
| Workflow preset | A versioned starting configuration for fields, stages, qualification, drafts and outcomes | Job search, inbound services, referrals |
| Domain module | Own authoritative business records and rules | Leads, Current, Recallatron, Relationships |
| Domain pack | Bundle modules, connectors, presets, templates and vocabulary into a starting setup | Freelance software business, HR consulting, sales |

Connectors come in three kinds:

- Intake transports bring evidence into Leads: the signed webhook, CSV and JSON
  import, manual capture, scheduled polls, and an email poller.
- Outbound MCP connectors sign in to someone else's MCP server and use its tools on
  your behalf. The job feed is the first: it collects postings from a provider's remote
  MCP server.
- Destinations receive an approved handoff or message: a CRM, a referral partner, or
  a back-office system for clients, contracts, time and invoices (Tuttle, in the idea
  document).

## What you can do with it

- Connect a website form, an automation tool or any system that can sign an HTTP request,
  without contributing a provider to the codebase.
- Import a CSV or JSON file against a connection. A file whose columns do not match the
  mapping is refused whole, naming the first missing column, before any row is accepted.
- Paste an email, a conversation or a copied job posting into the capture screen.
- Connect an external MCP service with a one-time consent in the provider's own sign-in
  page, then reconnect or revoke it from the Leads connections screen.
- Rotate a connection's signing key with an overlap window, end the overlap early, or
  revoke the connection outright.
- Watch each connection's health: last accepted, last processed, pending, unresolved
  failures, lag and the last error.

## How it works

### A module declares what a connector may call

A module's manifest lists connector bindings: a transport, the one operation it may call,
and the HTTP route it registers on the API surface when it has one. Leads binds every
intake transport to the same operation, `leads.intake.accept_delivery`, so "identical
apart from transport" holds by construction.

### A connection is an actor with exactly one permission

After the signature checks out, the receiver builds a context whose actor is the
connection itself, with the `service` role and an operation set of exactly the bound
operation. Anything else it might try fails the registry's ordinary checks. It cannot
approve anything, install a module or rotate a secret.

### The webhook authenticates before it writes

The sender signs `<timestamp>.<body>` with HMAC-SHA256 and sends the timestamp and
signature as headers. The receiver, in order: looks the connection up through a
control-plane locator (the id in the URL is a lookup key, never authorization); refuses a
revoked connection before any signature work; refuses a timestamp outside the 300-second
replay window; verifies the signature in constant time against the current key, and the
previous key while an overlap is open; parses one JSON object, refusing duplicate keys and
non-finite numbers; and only then calls the bound operation. The body is capped at 256
KiB, only JSON, no compression, no query string. An authentication refusal returns `401`
and stores no receipt.

### A flood of bad requests costs one write a minute

A refused request is counted on the connection's health so the owner can see it, but the
counter is written at most once per interval (60 seconds by default), in a single
conditional statement keyed on its own column. Anyone who has seen a webhook URL can send
garbage at it; they cannot turn that into unbounded database writes.

### Keys rotate by generation

Rotating writes a new key under a new reference, keeps the old one valid for an overlap
you choose (zero by default, an hour at most by default), and bumps the connection's key
generation. Every receipt records which generation verified it, and the worker rechecks
the generation before processing, so a delivery accepted under a revoked key is refused
even if it was already queued. The key itself is handed to the website's server once, by
the host operator, as a private file; it never travels through an operation, an event or
an export.

### Pollers advance their cursor only after acceptance

A poll or email connector keeps a durable cursor on its connection and moves it only after
the delivery is durably accepted. No model is involved: moving a known email format into
intake is deterministic work.

### Outbound MCP connectors

The job feed signs in to the provider as an OAuth client with the owner's consent, then
the worker calls the provider's tools on a schedule. Nothing reaches the provider until
the operator turns the client on and an owner connects. Every request goes over `https`
only, follows no redirects, and goes only to hosts whose every resolved address is public.
Tool names and actions are on an allowlist. An optional wire trace logs one line per
request with no secrets and no content.

### Destinations sit behind approvals

Sending to a destination is an external-class operation: it needs a bound approval, a
destination guard that refuses when the destination is not registered or its credential is
unavailable, and an external-action record with its own outcome. A CRM connection declares
which system owns which fields, so two systems never both claim authority over one value.

### Credentials stay inside their adapters

A connection's signing key lives in the deployment's secret store under a workspace-scoped
id. Only the receiver can resolve it, and only for the exact connection the locator named.
A connector never passes a client's token through to an unrelated service.

### Back-office integration

For a back-office system, the design connects rather than absorbs. The paths run from
least to most coupled: structured export and import, a documented local interface, or a
small local node that makes an authenticated outbound connection and exposes only approved
operations to a remote rheo session.

## Why it's built this way

Every user's funnel is different, and none of them should need a change to the core. A
connector handles transport and translation and calls one operation, nothing more. A new
source is a connection and a mapping, never a new column or a branch in the intake path.

Outbound connectors also serve the one-door goal. Each external MCP server a connector
proxies is one fewer server in every agent session, with its access governed by the same
grants and audit as everything else.

## Where to look

- Specification: [transports](../architecture/intake-and-events.md#transports),
  [connector bindings](../architecture/module-contract.md#the-manifest),
  [secrets](../architecture/storage-and-workspaces.md#a4-the-secret-store-fr-13-guardrail-14),
  [extensions and the back office](../ideas/rheo-stream-idea.md#framework-core-modules-and-domain-packs),
  [job feed operator guide](../../deploy/README.md#job-feed-collector),
  [connectors boundary](../../connectors/README.md)
- Code: [webhook receiver](../../packages/core/src/rheo_core/connectors/receiver.py),
  [connector credentials](../../packages/core/src/rheo_core/connectors/credentials.py),
  [outbound egress rules](../../packages/core/src/rheo_core/connectors/egress.py),
  [MCP client](../../packages/core/src/rheo_core/connectors/mcp_client.py),
  [OAuth client](../../packages/core/src/rheo_core/connectors/oauth.py),
  [connector routes](../../apps/core/src/rheo_app_core/connector_routes.py),
  [job feed](../../modules/leads/src/rheo_leads/jobfeed)
- Tests: [website intake](../../tests/postgres/test_website_intake.py),
  [egress](../../tests/test_egress.py),
  [MCP client](../../tests/test_connector_mcp_client.py),
  [OAuth client](../../tests/test_connector_oauth.py),
  [job feed poll acceptance](../../tests/postgres/test_jobfeed_poll_accept.py)
