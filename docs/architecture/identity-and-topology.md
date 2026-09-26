# Identity, sessions, tokens, and URL topology

**Part of:** the [architecture specification](README.md). Designs against D6, D7, D10, R1, FR 1,
FR 3 to FR 6, FR 47, FR 48, and criteria 8, 9, 19, 22, 23. Records the ratified host assignments
and fixes what a token can and cannot carry.
**Decision:** A11 (host-only session cookies with an identity-host session grant; identity
endpoints on every application host). See the [decision list](README.md#architecture-decisions).

## The identity-provider boundary (D6, FR 3)

```text
IdentityProvider
  provider_id: str
  begin(state: str, callback_url: str) -> RedirectTarget
  complete(callback_params, state: str) -> ProviderIdentity
     ProviderIdentity: provider_id, subject, email | None, email_verified: bool, display_name
```

That is the whole boundary. `rheo_core.identity` owns it, resolves a `ProviderIdentity` to an
`account` through `control.identity`, creates the account on first login when signup is open
([first run](#first-run-and-who-may-sign-up)), and creates the session. Providers live in
`rheo_core.identity.providers.<provider_id>`; the GitHub implementation is the only shipped one,
configured by the `identity.providers.github.*` deployment settings with the client secret as a
reference (`client_secret_ref`), which startup mirrors into `control.identity_provider`. The
`state` value passed to `begin` is a random 192-bit value that the identity host also sets in a
host-only cookie (`rheo_oauth_state`, ten-minute expiry) before redirecting; `/auth/callback` compares the
cookie to the returned `state` and refuses `invalid_state` on a mismatch, so a callback that did
not begin in this browser creates no session. Domain modules import neither the providers package
nor the boundary; a CI check asserts it (criterion 8). The test double is a provider that
completes with a fixed synthetic identity, registered by the test harness only. A request that
needs a disabled identity provider (for example `/auth/login` with GitHub sign-in off) is refused
503 `{"state": "identity_provider_unavailable"}` (a browser whose `Accept` prefers HTML gets a
plain 503 page saying sign-in is unavailable instead), alongside `invalid_state` above and the other
`/auth` refusal states this document lists.

An account is not a workspace. Membership is many-to-many with a role (R1), and every context
carries the role read from `control.membership` at request time, never cached in the session.

## First run, and who may sign up

On a fresh deployment the control plane has no account. The sequence is fixed:

1. **First login creates the first account.** Signup is *computed*, not configured: a login
   whose identity is unknown creates an account when the control plane holds no account at all,
   or when the deployment setting `identity.allow_signup` is explicitly `true`. The setting's
   package default is `false`, and the first-account check runs whatever its value, so the default
   means "open until the first account exists, closed after": an operator who never touches it
   gets a deployment that admits exactly one person and does not change underneath them.
2. **The first workspace is created for that account** with `rheo workspace create --owner
   <account>` (a first-run screen in the shell is planned and not built yet), and that account is
   written as `owner` in `control.membership`. Every later workspace works the same way: its
   creator is its owner.
3. **A second person needs an account before a membership.** With signup closed, the operator
   pre-registers them: `rheo account create --provider github --subject <provider subject>
   --display-name <name>` writes the `account` and `identity` rows, so their first login resolves
   to an existing account. Then `rheo member add --workspace <id> --account <id> --role member`
   creates the membership (R1). Setting `identity.allow_signup = true` is the alternative and is
   the operator's explicit choice.

## Accounts, sessions, and the active workspace

A web session is a `control.session` row, identified internally by a UUIDv7 that never leaves the
server. What the browser holds is a **session secret**: 256 random bits from a CSPRNG, minted per
host ([below](#sessions-and-cookies)), of which the control plane stores only the SHA-256 in
`control.session_secret`. The cookie and the internal `X-Rheo-Session` header carry the secret;
the database holds the hash, the same rule the token and grant tables follow. The session's
`active_workspace_id` is the only source of "which
workspace" for a web request. The workspace switcher calls `POST /auth/session/workspace` with
`target_workspace_id`: a control-plane session endpoint served by the core's identity routes,
**outside the operation registry**, which is why the registry's reserved-field rule
([module contract](module-contract.md#operations-tools-events)) does not apply to it. It checks
that the session's account holds a membership in the target, updates the row, records the switch
on the session (`active_workspace_changed_at`) and in the structured log, and returns. It routes
nothing by the value: it is the target of a membership check, and the next request's storage
routing still runs from `active_workspace_id` through the registry
([storage](storage-and-workspaces.md#server-derived-routing-fr-2)). Criterion 6's test covers
it from both sides: a `workspace_id` supplied to any registered operation is ignored, and this
endpoint refuses a workspace the account is not a member of. After a switch, every request in
that session routes to the new workspace with no identifier in any URL, which is what criterion 6
requires literally. Session lifetime: idle expiry `identity.session_idle_days` (default 14),
absolute `identity.session_max_days` (default 30); logout revokes the row, which ends every
host's cookie at once because every host's secret resolves to the one revoked session.

## Tokens for CLI and MCP (FR 4)

| Concern | Design |
| --- | --- |
| Format | `rheo_<kind>_<43 base64url chars>`: 256 random bits from a CSPRNG. Only the SHA-256 is stored (`access_token.token_hash`). |
| Scope | Exactly one account, one workspace, one operation set. The operation set is **snapshotted at issuance** into `control.access_token_operation`, one row per permitted operation name, and nothing widens it later; a new scope is a new token. The snapshot is expanded from a named package set ([below](#the-named-package-operation-sets)) or, for a run-scoped token, from an explicit list. |
| Kinds | `cli` (a person's command-line token), `mcp` (a person's token for an MCP client), `runtime` (the run-scoped token the core issues for one model run, [runtime](runtime-and-mcp.md#the-run-scoped-token)). `access_token.kind` holds the value; `issued_from` records the issuing authority (`session`, `operator`, `runtime`). |
| Issuance | Through `core.token.issue` only, from one of three issuing authorities: an authenticated web session (the account issuing for itself in a workspace it belongs to); the operator command on a headless install (`rheo token issue --account <id> --workspace <id> --set <name> --kind <cli\|mcp>`, against the control plane); or the core's `runtime` package, on behalf of the actor that started a run. The value is shown once, or handed to the adapter once. No token can issue a token: `core.token.issue` is itself non-token-issuable (below). |
| Lifetime | `cli` default 90 days, `mcp` default 30 days (`identity.token_max_days.cli` and `identity.token_max_days.mcp`, floor `min`), `runtime` the run's deadline. |
| Presentation | `Authorization: Bearer` on the `api` and `mcp` surfaces, each accepting the kinds made for it: the `api` surface accepts `cli` only; the `mcp` surface accepts `mcp` and `runtime`. An `mcp` token on the `api` surface is `token_wrong_kind` (issue #130): it is a model's credential, and over HTTP it would receive an operation's raw output past the tool facade's redaction, so a `cli` token is the HTTP credential. Neither surface reads a cookie, and a session secret presented as a bearer value is `token_malformed` because it matches no token row. A person's `cli` token is refused on the `mcp` surface as `token_wrong_kind`, so a person's command-line credential handed to an MCP client is refused by kind before its scope is even read. No browser flow is ever involved after issuance. |
| Refusal | Malformed, wrong kind for the surface, expired, revoked, scope-invalid, carrying an unusable stored purpose, and out-of-scope each return a distinct state (`token_malformed`, `token_wrong_kind`, `token_expired`, `token_revoked`, `token_scope_invalid`, `token_purpose_invalid`, `operation_not_permitted`) at the boundary, before any service runs, with no partial effect (criterion 9). `token_scope_invalid` is returned when a presented token's snapshot contains a non-token-issuable operation; it can only arise from a row written outside `core.token.issue`, and the boundary checks it anyway so that the rule below is enforced at both ends. |
| Revocation | `core.token.revoke`; membership removal revokes the account's tokens for that workspace (no membership-removal path is built yet; presentation refuses `membership_missing` regardless). A run-scoped token is deleted with its snapshot rows when its run ends; the run's `runtime_request` row keeps what the run was permitted. |

### What a token can never carry, and what it holds for a gated operation

**The non-token-issuable set.** One rule, enforced by `core.token.issue` on every issuance
whatever the kind and whoever the issuer: the operations that grant, waive, or exercise the right
to let an effect proceed never appear in a token's snapshot. In release one that set is
`core.approval.approve`, `core.approval.refuse`, `core.standing_grant.create`,
`core.standing_grant.revoke`, `core.token.issue`, and `core.token.revoke`. An issuance whose
expanded set contains one of them is refused with `set_not_issuable` naming the operation, and
`token_scope_invalid` at presentation is the same check from the other end. The consequence is
that approval is an act of a web session (and, in phase six, of a channel confirmation by an
enrolled account), never of a token: `core.approval.*` declares roles `owner` and `member`, the
only contexts that both carry one of those roles and hold the operation are web sessions, and no
`cli`, `mcp`, or `runtime` token exists that could hold it. A model cannot approve what it
proposed because nothing a model can hold is able to approve anything (A10, R3 item 8).

**A gated operation in a token's set is a request right, not an execution right.** A token's
snapshot may name a destructive, external, or financial operation; criterion 19 requires an `mcp`
token to reach a destructive operation and receive `approval_required`, and the three release-one
deletion tools are listed for `mcp` tokens. What the snapshot confers for such an operation is
exactly the right to *request* it: the dispatcher creates a pending approval and stops
([confirmation](confirmation-and-safety.md#the-approval-record)). Execution is never on the
calling token's authority. For a destructive operation it runs inside the `core.approval.approve`
call, and for an external or financial one it is a job the worker runs against the approval; both
paths rerun the guards against the original actor, and neither is an operation a token calls.
So a token can propose, can never approve, and can never execute.

**The issuer bound.** A token's snapshot is a subset of the issuing authority's own permitted
set, in addition to the role bound every token carries (a token never exceeds its account's role,
rechecked from `control.membership` at every presentation):

| Issuing authority | Its permitted set | What `core.token.issue` does |
| --- | --- | --- |
| A web session | Every operation whose declared roles include the account's role in the target workspace. | Expands the named package set against the registry, intersects with that set, strips the non-token-issuable set, refuses `set_empty` if nothing remains, and snapshots the rest. A member's `cli_full` is therefore the member's full set, not the owner's. |
| The operator command | The package sets, for the target account. | May name only a package set (never an explicit list); expands it, intersects with the operations the target account's role permits, strips the non-token-issuable set, and snapshots. |
| The core's `runtime` package, for a run | The permitted set of the actor that started the run: that actor's own token snapshot when it is a token, or the role-permitted set when it is an account. | Takes the explicit list of operations the run's permitted tools name, intersects with that set, strips the non-token-issuable set, and snapshots; a run can therefore never reach past the person or token that started it, however the operation's tool needs are declared. |

### The named package operation sets

Three sets ship in the package, each defined as a rule the core evaluates against the registry at
issuance, so a builder is not enumerating names by hand and a set never has to be edited when an
operation is added:

| Set | Rule | What it yields in release one |
| --- | --- | --- |
| `read_only` | Every registered operation of class `read`. | Workspace status, operation reads, audit (owner), every module read. |
| `agent_default` | Every operation named by a registered MCP tool ([tool set](runtime-and-mcp.md#the-mcp-facade)). | The reads, drafts, and mutates a tool exposes, plus the request right for the three deletion tools' `core.record.delete`; by construction no configuration, token, grant, or approval operation, because none has a tool. |
| `cli_full` | Every registered operation except the non-token-issuable set. | Everything the account's role permits, including configuration operations for an owner, minus the six operations no token may carry. |

Workspace-defined named sets are not release-one machinery; nothing in the requirements needs
one, and a snapshot per token makes criterion 9's "one permitted operation set" a property of
the token row rather than of a shared row that could change under it. An owner-authored set is an
additive later feature: a fourth rule source, the same snapshot.

The operator command that adds a second member (`rheo member add --workspace <id> --account
<id> --role member`) is the only way a second membership exists in release one (R1); criterion 8's
member session comes from that member signing in through the provider.

## Sessions and cookies

The ratified requirements say that in subdomain mode one session covers the shell host, the
identity host, and the module hosts; that `api.` and `mcp.` authenticate by token and ignore the
cookie; and that the apex, `docs.`, and the reserved integration host must not receive the cookie.
A cookie scoped to the parent domain would be sent by the browser to every host beneath it,
including hosts served by other software, so cookie scope cannot satisfy the last rule and a
reverse proxy cannot strip what is sent to a different server. This specification therefore uses
**host-only cookies and a session grant**.

- The session cookie `rheo_session` is set with no `Domain` attribute, `Secure` (dropped only for
  a plain-`http` scheme outside the `production` profile), `HttpOnly`, `SameSite=Lax`, `Path=/`,
  on each application host separately. Its value is a **session
  secret minted for that host**: 256 random bits, whose SHA-256 is stored in
  `control.session_secret(secret_hash, session_id, host, created_at)` with `(session_id, host)`
  unique. One session, one secret per host; the session id itself is never in a cookie or a URL.
  A secret is per host because the control plane holds only hashes, so the identity host cannot
  hand an existing secret to another host, and a grant code that travels in a URL must never be
  the cookie value.
- The identity host (`auth.` in subdomain mode) holds its own cookie after login.
- When an application host receives a request with no valid cookie, the web tier mints a
  **continue nonce** (128 random bits), sets it in a host-only cookie `rheo_continue` on that
  host (`Secure`, `HttpOnly`, `SameSite=Lax`, five-minute expiry), and redirects to
  `<identity>/auth/continue?return=<absolute url>&nonce=<nonce>`, built through the routing
  configuration from the forwarded host, never from a hard-coded name.
- `/auth/continue` on the identity host first checks the `return` URL: its host must be in
  `RoutingConfig`'s **application-host set** (the shell host, the identity host, and the hosts of
  the modules the deployment loaded, in subdomain mode; the single host in path mode). Its scheme
  is not compared: the grant redirect is built with the configured scheme, and the final redirect
  carries only the return URL's path and query. Any other host is refused with `invalid_return`
  and no grant is written, so the identity host cannot be used as an open redirect or made to mint
  a grant for an arbitrary label. With a valid session cookie and a valid return, it writes a
  `control.session_grant` row (a random 256-bit code, its SHA-256 stored, `target_host` set to the
  return URL's host, the SHA-256 of the nonce in `nonce_hash`, 60-second expiry, single use) and
  redirects to `<return host>/auth/continue?code=<code>&return=<return url>`. Without a cookie, it
  starts the login flow and returns here afterward.
- The reverse proxy routes only the hosts the routing configuration names. The template for
  `deploy/` exists (`deploy/compose.flagship.yaml`) and hard-codes the flagship host set in its
  router labels, so that set must be kept in step with the hosts `rheo routing hosts` reports;
  it never routes a bare `*.<base_host>` wildcard to the application, so a
  label nobody configured reaches no listener and can neither receive a cookie nor complete a
  grant.
- `/auth/continue` on the application host: the core (which serves `/auth/*` on every
  application host) looks up the code's hash, checks `target_host` equals the request's forwarded
  host, the code is unused and unexpired, and the SHA-256 of the `rheo_continue` cookie equals
  the grant's `nonce_hash`; any mismatch is `invalid_grant` with no cookie set. It then marks the
  code used, clears `rheo_continue`, mints this host's session secret, writes its
  `session_secret` row, sets the host-only cookie, and redirects to the return path. The session
  id behind every host's secret is the same, so it is one session. The nonce is what stops a
  **login CSRF**: a grant minted in an attacker's browser cannot log this browser into the
  attacker's session, because this browser never held the nonce that grant carries.
- In single-host path mode there is one host: `/auth/continue` finds the cookie directly and
  redirects to the return path. Same code, no grant row, no nonce, which is why both modes are one
  implementation (criterion 22); login CSRF in path mode is covered by the OAuth `state` cookie
  ([the identity-provider boundary](#the-identity-provider-boundary-d6-fr-3)).
- Logout on any host revokes the session row; every other host's secret now resolves to a
  revoked session and its cookie is cleared on its next request.

CSRF: the `/auth/*` POSTs check `Origin` against the routing configuration's host set and refuse
`origin_not_allowed` otherwise; they are the only state-changing browser requests today, and any
Next.js server action or route handler the web tier later adds for a state change does the same.
The `api` and `mcp` surfaces accept no cookie, so they need no CSRF defence. CORS: no
`Access-Control-Allow-Origin` is emitted anywhere; `api.cors_origins` (a deployment setting,
empty) is reserved for a later second front end and is not declared yet. The first-party web tier
does not need it, because it reaches the core server-side.

**The internal API's trust boundary.** The core process has two listeners. The public one is
what the proxy routes to (`/auth/*`, the `api` surface, the `mcp` surface). The internal one is a
second port on the container network only (`RHEO_CORE_INTERNAL_API_URL`, `http://core:8100` in
the reference deployment); the proxy has no rule that reaches it and `deploy/` publishes no port for
it. The web tier, having read the `rheo_session` cookie, calls the internal listener with the
session secret in the `X-Rheo-Session` header and the forwarded host in `X-Rheo-Host`, plus a
shared secret in `X-Rheo-Internal` that the web tier reads from its own environment
(`RHEO_INTERNAL_SECRET`) and the core resolves through the internal listener's secret scope from
the `internal.secret_ref` setting. Its package default is empty, which refuses every internal
request until a deployment sets it (`deploy/compose.yaml` sets
`secret://env/RHEO_INTERNAL_SECRET`). The internal listener hashes the header value,
looks it up in `session_secret` for that host, joins the session row, and ignores cookies; the
public listener reads cookies on `/auth/*` only and ignores the header. A browser therefore
cannot reach the internal listener at all, and cannot make the public listener treat a header as
a session, which is what keeps the `api` surface cookie-free in fact and not only by policy.

## URL topology as configuration (D7, FR 47)

### The routing configuration

One typed object, loaded by the core from deployment settings and served to the web tier through
the internal API (`GET /internal/v1/routing`, fetched on first use and cached). The browser never
receives a copy; every link is rendered on the server:

```text
RoutingConfig
  mode: "path" | "subdomain"
  scheme: "https"
  base_host: "example.test"                 # Host-header match; port-free
  public_host: "example.test"               # URL authority; may carry a port; empty inherits base_host
  surfaces:
    shell:    { host: "circuit", path: "/" }
    identity: { host: "auth",    path: "/auth" }
    api:      { host: "api",     path: "/api" }
    mcp:      { host: "mcp",     path: "/mcp" }
    docs:     { host: "docs",    external: true }
    integration: { host: "tuttle", external: true, reserved: true }
    modules:  { recallatron: { host: "recallatron", path: "/recallatron" }, ... }  # loaded manifests
```

Both the Python core and the web tier expose one function, `url_for(config, surface, path)`
(`urlFor` in the web tier), and every link, redirect, callback URL, and MCP configuration file goes
through it. In `subdomain` mode it yields `https://<host>.<public_host><path>`, except that the
identity surface keeps its `/auth` prefix; in `path` mode
`https://<public_host><surface path><path>`.
`public_host` is the URL authority and may carry a port. `base_host` is the port-free
name used for Host-header matching; when `public_host` is empty it inherits `base_host`.
The OAuth callback URL registered with the provider is `url_for("identity", "/callback")`, which
is `https://auth.example.test/auth/callback` in one mode and `https://example.test/auth/callback`
in the other (criterion 22). A route string that names a host or a topology-specific prefix
anywhere else is a lint failure in both codebases.

### The routing table

The reverse proxy's rules are the same in both modes; only the host matching changes:

| Request | Goes to |
| --- | --- |
| Any application host, `/auth/*` | `core` (identity endpoints) |
| `api` surface | `core` (HTTP API, intake receiver); token auth |
| `mcp` surface | `core` (MCP facade); token auth |
| Any application host, everything else | `web` |
| apex, `docs`, `tuttle` | not this application |

"Application host" means the shell host, the identity host, and every module host in subdomain
mode, and the single host in path mode. Inside `web`, the module's route tree is mounted at
`url_for(module, "/")`, so a module's screens are the same components under either topology.

The `mcp` surface is matched inside `core` as well as at the proxy. In path mode `core` serves
MCP at the surface path (`/mcp` and `/mcp/`). In subdomain mode it serves MCP at `/` on the `mcp`
host and nothing else there: the `mcp` host is not an application host, so `/auth/*`, the HTTP
API and `/healthz` answer 404 on it, and no other host reaches MCP. The SDK's DNS-rebinding
protection admits only the surface's own `Host` values
([the MCP facade](runtime-and-mcp.md#the-mcp-facade)).

### The reference deployment's hosts

Recorded from the ratified requirements; each is configuration a self-hoster may change.

| Host | Role | Served by |
| --- | --- | --- |
| apex `rheo.stream` | Project and marketing page | Not the application. Never receives the session cookie. |
| `circuit.rheo.stream` | The application shell and the workspace switcher | `web` |
| `auth.rheo.stream` | Login, the OAuth callback, the session grant | `core` |
| `leads.`, `current.`, `recallatron.`, `relationships.` | Module surfaces | `web` |
| `api.rheo.stream` | HTTP API and intake | `core`, token auth |
| `mcp.rheo.stream` | MCP facade | `core`, token auth |
| `docs.rheo.stream` | Documentation | Not the application. |
| `tuttle.rheo.stream` | Reserved for the back-office integration surface | Not the application; nothing in release one. |
| `app.rheo.stream` | Kept in reserve as a permanent redirect to `circuit.` | Reverse-proxy configuration, implemented in the deployment phase; not part of this specification's application routing. |

Subdomain mode needs wildcard DNS (`*.rheo.stream` to the proxy) and a wildcard certificate for
one label; the proxy terminates TLS for every host in the table. Single-host path mode needs one
name and one certificate and is the default a fresh install produces (edge case 19), which is the
arrangement guardrail 15 keeps continuously exercised.

### Constraint carried to the hosted edition

Module subdomains and per-tenant subdomains compete for the one label a wildcard certificate
covers. A hosted edition that wants `<tenant>.rheo.stream` cannot also have
`leads.rheo.stream` mean "the Leads module for the current tenant" without either a second
label (`leads.<tenant>.rheo.stream`, needing a certificate per tenant or a deeper wildcard) or a
return to path mode per tenant. It must choose deliberately ([later phases](later-phases.md#phase-8-the-hosted-edition));
nothing in release one presumes either answer, because the routing configuration is the only
place hosts are named.

## Platform independence (D10, FR 48)

The web tier runs under `next start` behind the proxy with no edge function, no hosted image
pipeline, and no build integration from any hosting platform. CI fails on a platform-only import
or an edge-runtime declaration through a static scan of `apps/web`, every `modules/*/web`, and
`packages/web-contract` (`scripts/check_web_platform.py`, criterion 23); `next build` alone would
not catch either. Redirects from middleware are absolute URLs built from the forwarded host
through `url_for`, never relative, and never from the internal listener's host.
