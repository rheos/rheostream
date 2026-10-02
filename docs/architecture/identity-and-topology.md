# Identity, sessions, tokens, and URL topology

**Part of:** the [architecture specification](README.md). Designs against D6, D7, D10, R1, FR 1,
FR 3 to FR 6, FR 47, FR 48, and criteria 8, 9, 19, 22, 23. Records the ratified host assignments
and fixes what a token can and cannot carry. Issue #287 adds connector sign-in, the OAuth front
door for MCP clients that cannot send a header (recorded change of direction 18).
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
workspace" for a web request. A new session starts in a default workspace, chosen in the same
transaction as the insert: the one the account's earlier sessions were most recently used in,
if the membership still exists and the workspace is `active`; else the oldest membership whose
workspace is `active`; else none. A session with none refuses `workspace_unselected`, and the
web shows a signed-in "no workspace yet" state with sign-out rather than the signed-out page
(issue #157). The workspace switcher calls `POST /auth/session/workspace` with
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
| Kinds | `cli` (a person's command-line token), `mcp` (a person's token for an MCP client), `runtime` (the run-scoped token the core issues for one model run, [runtime](runtime-and-mcp.md#the-run-scoped-token)). `access_token.kind` holds the value; `issued_from` records the issuing authority (`session`, `operator`, `runtime`, `connector`). |
| Issuance | From one of four issuing authorities. Three go through `core.token.issue`: an authenticated web session (the account issuing for itself in a workspace it belongs to); the operator command on a headless install (`rheo token issue --account <id> --workspace <id> --set <name> --kind <cli\|mcp>`, against the control plane); or the core's `runtime` package, on behalf of the actor that started a run. The fourth, `connector`, is the OAuth code exchange of [connector sign-in](#connector-sign-in-oauth-for-mcp-clients): it mints an `mcp` token through `issue_connector_token` inside the exchange's own transaction, never through an operation, and only after the account approved it on the consent page. The value is shown once, or handed to the adapter or the connector once. No token can issue a token: `core.token.issue` is itself non-token-issuable (below), and the connector exchange takes no bearer. |
| Lifetime | `cli` default 90 days, `mcp` default 30 days (`identity.token_max_days.cli` and `identity.token_max_days.mcp`, floor `min`), `runtime` the run's deadline. A `connector` token's value lives `identity.oauth.access_token_minutes` (default 60) and is rotated in place on the same row at every refresh, so its id never changes; the grant behind it ends at `min(identity.oauth.grant_days, identity.token_max_days.mcp)` days (default 30) after issuance, and no refresh outlives that end. |
| Presentation | `Authorization: Bearer` on the `api` and `mcp` surfaces, each accepting the kinds made for it: the `api` surface accepts `cli` only; the `mcp` surface accepts `mcp` and `runtime`. An `mcp` token on the `api` surface is `token_wrong_kind` (issue #130): it is a model's credential, and over HTTP it would receive an operation's raw output past the tool facade's redaction, so a `cli` token is the HTTP credential. Neither surface reads a cookie, and a session secret presented as a bearer value is `token_malformed` because it matches no token row. A person's `cli` token is refused on the `mcp` surface as `token_wrong_kind`, so a person's command-line credential handed to an MCP client is refused by kind before its scope is even read. No browser flow is ever involved after issuance. |
| Refusal | Malformed, wrong kind for the surface, expired, revoked, scope-invalid, carrying an unusable stored purpose, and out-of-scope each return a distinct state (`token_malformed`, `token_wrong_kind`, `token_expired`, `token_revoked`, `token_scope_invalid`, `token_purpose_invalid`, `operation_not_permitted`) at the boundary, before any service runs, with no partial effect (criterion 9). `token_scope_invalid` is returned when a presented token's snapshot contains a non-token-issuable operation; it can only arise from a row written outside `core.token.issue`, and the boundary checks it anyway so that the rule below is enforced at both ends. |
| Revocation | `core.token.revoke`; membership removal revokes the account's tokens for that workspace (no membership-removal path is built yet; presentation refuses `membership_missing` regardless). A run-scoped token is deleted with its snapshot rows when its run ends; the run's `runtime_request` row keeps what the run was permitted. A `connector` token is revoked like any other (`rheo token revoke <id>`, which also writes a `grant_revoked` event); its row is never deleted. |

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
| The operator command | The package sets, for the target account. | May name only a package set (never an explicit list); expands it, intersects with the operations the target account's role permits, strips the non-token-issuable set, and snapshots. `--discover` adds `core.tool.call` to the expansion ([discover-then-call](runtime-and-mcp.md#discover-then-call)). |
| The core's `runtime` package, for a run | The permitted set of the actor that started the run: that actor's own token snapshot when it is a token, or the role-permitted set when it is an account. | Takes the explicit list of operations the run's permitted tools name, intersects with that set, strips the non-token-issuable set, and snapshots; a run can therefore never reach past the person or token that started it, however the operation's tool needs are declared. |
| A connector grant (`connector`) | The `agent_default` set, for the approving account's role in the workspace chosen on the consent page. | Not `core.token.issue`: `issue_connector_token` computes `(agent_default & role-permitted) - non-token-issuable - {core.tool.call}`, the bound an operator issuance of `agent_default` gets minus the discover grant, refuses `set_empty` if nothing remains, and snapshots it once with `set_name = agent_default`. A refresh never re-snapshots, so a later role change narrows it only through the role recheck at presentation. |

### The named package operation sets

Three sets ship in the package, each defined as a rule the core evaluates against the registry at
issuance, so a builder is not enumerating names by hand and a set never has to be edited when an
operation is added:

| Set | Rule | What it yields in release one |
| --- | --- | --- |
| `read_only` | Every registered operation of class `read`. | Workspace status, operation reads, audit (owner), every module read. |
| `agent_default` | Every operation named by a registered MCP tool ([tool set](runtime-and-mcp.md#the-mcp-facade)). | The reads, drafts, and mutates a tool exposes, plus the request right for the three deletion tools' `core.record.delete`; by construction no configuration, token, grant, or approval operation, because none has a tool. |
| `cli_full` | Every registered operation except the non-token-issuable set. | Everything the account's role permits, including configuration operations for an owner, minus the six operations no token may carry. |

None of the three contains `core.tool.call`, the discover-then-call grant: each subtracts it, so
it reaches a snapshot only when an issuance asks for it (`discover: true`, or an explicit list
naming it). A token holding it is listed a fixed set of MCP tools and reaches its other grants
through `operations_call` ([discover-then-call](runtime-and-mcp.md#discover-then-call)); it
grants nothing else, and an issuance whose only surviving operation would be the grant is refused
`set_empty`.

Workspace-defined named sets are not release-one machinery; nothing in the requirements needs
one, and a snapshot per token makes criterion 9's "one permitted operation set" a property of
the token row rather than of a shared row that could change under it. An owner-authored set is an
additive later feature: a fourth rule source, the same snapshot.

The operator command that adds a second member (`rheo member add --workspace <id> --account
<id> --role member`) is the only way a second membership exists in release one (R1); criterion 8's
member session comes from that member signing in through the provider.

## Connector sign-in (OAuth for MCP clients)

A client that can set an `Authorization` header (Claude Code, the local bridge, a bot) uses a
token issued as above. A claude.ai custom connector cannot: it is added by URL, starts sign-in
from a `401`, and discovers everything else from OAuth metadata. Issue #287 adds that front door
for MCP only ([recorded change of direction 18](../ideas/rheo-stream-idea.md#recorded-changes-of-direction)).
It ends in an ordinary `mcp` token row with `issued_from = connector`, so the `mcp` surface's
bearer gate and `resolve_token` are unchanged. GitHub stays the only login; the flow reuses
`/auth/login`. The code is `rheo_core.oauth` (`surface`, `service`, `operations`),
`rheo_core.storage.oauth_store`, and `apps/core`'s `oauth_routes.py` and `mcp_mount.py`.

**Off unless configured.** `rheo_core.oauth.oauth_surface(settings, routing)` returns either the
surface (every URL it advertises, the redirect allowlist and the lifetimes) or `OAuthUnconfigured`
with a reason. With the surface unconfigured the `mcp` gate's `401` is the bare `Bearer` it always
was, the metadata paths are 404 and `/auth/oauth/*` answer 404, so nothing is advertised that does
not work. Its reasons are `disabled` (`identity.oauth.enabled` false, the default),
`identity_provider_disabled` (GitHub sign-in off or without a client id), `allowlist_empty`, and
`setting_invalid` (an allowlist entry that is neither `https` with a host nor `http` on
`localhost` or `127.0.0.1`, or that carries userinfo, a fragment or an unparseable port;
`routing.identity.path` other than the served `/auth`; or a routing host that makes the metadata
pointer unfit for a `WWW-Authenticate` header: not ASCII, or carrying a `"`). Every reason except
`disabled` is `rheo doctor`'s `oauth surface` line as `FAIL enabled but unconfigured: <reason>`.
An `identity.oauth.*` integer below its minimum does not get that far: the house `KeySpec.minimum`
rule refuses the settings load, so the process does not start.

| Setting (`identity.oauth.*`, deployment scope) | Default | Minimum |
| --- | --- | --- |
| `enabled` | `false` | |
| `redirect_uris` | `https://claude.ai/api/mcp/auth_callback` only | non-empty, each valid as above |
| `access_token_minutes` | 60 | 5 |
| `grant_days` | 30 | 1 |
| `refresh_grace_seconds` | 10 | 0 |
| `max_clients` | 100 | 1 |
| `registrations_per_source_per_hour` | 30 | 1 |
| `abandoned_client_minutes` | 60 | 15 |

**What is advertised.** Every URL comes from `url_for`. The canonical resource is
`url_for(MCP, "/")`, `https://mcp.example.test/` in subdomain mode and
`https://example.test/mcp/` in path mode, and is what the person types as the connector URL. The
issuer is that string without its trailing slash. Its two documents sit at the RFC 9728 / RFC 8414
insertion paths, on the `mcp` surface:

| Document | Subdomain mode (`mcp` host) | Path mode (the single host) |
| --- | --- | --- |
| Protected resource (RFC 9728): `resource`, `authorization_servers` (the issuer), `bearer_methods_supported: ["header"]` | `/.well-known/oauth-protected-resource` and the same with a trailing `/` | `/.well-known/oauth-protected-resource/mcp` and `.../mcp/` |
| Authorization server (RFC 8414): the issuer, the three endpoints, `code` only, `authorization_code` and `refresh_token`, `S256` only, auth method `none`, `authorization_response_iss_parameter_supported` | `/.well-known/oauth-authorization-server` | `/.well-known/oauth-authorization-server/mcp` |

Path mode serves only these path-inserted documents: nothing answers at the root
`/.well-known/oauth-authorization-server`. The endpoints live on the identity host:
`url_for(IDENTITY, "/oauth/register" | "/oauth/authorize" | "/oauth/token")`, plus the consent page
at `/auth/oauth/consent`. They answer 404 on any other host in subdomain mode. There is no
`scopes_supported` (any `scope` parameter is ignored), no client-ID metadata documents and no
revocation endpoint.

**The flow.**

1. The connector `POST`s to the resource with no token and gets `401` with
   `WWW-Authenticate: Bearer resource_metadata="<protected-resource URL>"`. Every refusal from the
   gate carries the pointer once the surface is configured, so a dead or superseded value also
   sends the client back to refresh.
2. It fetches the two documents and registers at `POST /auth/oauth/register` (RFC 7591, JSON, body
   at most 16 KiB). Each `redirect_uris` entry must be an exact member of the allowlist (else
   `invalid_redirect_uri`); everything else is overridden, not refused: the auth method is set to
   `none`, the grant and response types to the two above, the client name is stripped of control
   characters and cut to 100 characters, and unknown fields are dropped. The answer is `201` with
   the effective metadata and a 128-bit `client_id`; a public client has no secret.
3. The browser opens `GET /auth/oauth/authorize`. A duplicated parameter, a bad `client_id` or an
   unregistered `redirect_uri` is an error page; every later error redirects to the client with
   `state` and `iss` (`unsupported_response_type`; `invalid_request` for a missing or malformed
   `S256` challenge or a `state` over 1024 characters; `invalid_target` for a
   `resource` that is not the canonical one, where subdomain mode also accepts the empty-path
   spelling `https://mcp.example.test`). On success the request is held server-side in an
   `oauth_authorization` row and the browser gets `303` to `/auth/oauth/consent` with a host-only
   `rheo_oauth_request` cookie (a 256-bit nonce, `Max-Age` 600, the attributes of the other auth
   cookies). The cookie is the only handle: no request id is ever in a URL.
4. `GET /auth/oauth/consent` with no live request (no cookie, an unknown, decided or expired one)
   is the `authorization_expired` page. With no session it redirects to
   `/auth/login?return=<consent URL>`, so GitHub's callback is the existing one. An account with no
   active workspace gets `workspace_unselected` and the request is decided, so it cannot be
   completed later. Otherwise the page shows the client name (marked as chosen by the app), the
   redirect URI's host, the signed-in account, the workspace (a choice when there are several),
   the operations the grant will hold, and its lifetime in days. It is server-rendered by core,
   every value escaped, and sent with
   `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'`,
   `X-Frame-Options: DENY` and `Cache-Control: no-store`.
5. `POST /auth/oauth/consent` checks `Origin` first (`origin_not_allowed`), then that the cookie's
   pending row is the `request_id` the page showed and that the form's `account_id` is the
   session's (else `authorization_expired`, nothing written), then a live session. Allow writes a
   60-second single-use code and redirects to the client with `code`, `state` and `iss`; Deny
   redirects with `error=access_denied`. A workspace that is not one of the account's active
   memberships is `not_a_member`. Either way the cookie is cleared.
6. The client redeems the code at `POST /auth/oauth/token` (form-encoded only, body only,
   duplicated parameters refused, `Cache-Control: no-store`). The code is burned first, whatever
   follows. A different `client_id` is `invalid_client` (401); a different `redirect_uri`, an
   expired code or a PKCE mismatch is `invalid_grant`; a `resource` that is present and not the
   canonical one is `invalid_target` (absent means the value bound at `/authorize`). Success
   writes, in one transaction, the `connector` token, its `oauth_grant` row and its first refresh
   token, and answers `{access_token, token_type: "Bearer", expires_in, refresh_token}`. A replayed
   code revokes whatever it issued. The `invalid_client` 401 carries no `WWW-Authenticate`: a
   public client sends no `Authorization` header, so there is no scheme to challenge.

The access value is `rheo_mcp_...`; it reaches the facade through the unchanged gate. Every token
and registration error is RFC 6749 §5.2 JSON (RFC 7591 §3.2.2 for registration) with a fixed
description per error word, so no request value is echoed back. Every refusal that changes state (a burned
code, a revocation, a refused event row) is a returned value, not an exception, so it commits.

**Refresh and reuse.** A grant is one `access_token` row. A refresh (`grant_type=refresh_token`)
reads the presented refresh token's row `FOR UPDATE`, so two concurrent refreshes of one value
serialize. A live refresh token passes, in order: its client equals the request's `client_id`
(else `invalid_client`), the access token is not revoked, the grant has not reached
`grant_expires_at`, the membership still holds, and the workspace is `active` (each else
`invalid_grant`); then `resource` as above. Success writes a new access value and `expires_at`
onto the same row (`rotate_access_token`), so the old value matches no row and answers
`token_malformed`, marks the old refresh row rotated, and issues a new refresh token. Rotation is
strict: a rotated refresh token presented within `refresh_grace_seconds` (default 10 s) of its
rotation is `invalid_grant` and changes nothing, which absorbs a benign client retry; presented
after that window it revokes the grant and writes `refresh_reuse_revoked`. Nothing is ever
re-issued from the grace, so no refresh token is kept in the clear. The access value's
`expires_at` is `min(now + access_token_minutes, grant_expires_at)`.

**The six tables** (schema `control`, revision `0003_oauth`; secrets stored as SHA-256 only):

| Table | Holds |
| --- | --- |
| `oauth_client` | One per registration: `client_id`, the sanitized `client_name`, `registered_from` (the request's client address), `created_at`. No secret column. |
| `oauth_client_redirect_uri` | Each registered redirect URI, every one an allowlist member. Cascades with its client. |
| `oauth_authorization` | One request from `/authorize` to redemption: the request nonce's hash, client, redirect URI, PKCE challenge, `state`, its 10-minute expiry, then the decision (account, workspace, `decided_at`), the code's hash, its 60-second expiry and use, and the `token_id` it issued. Cascades with its client. |
| `oauth_grant` | Keyed by the access token's id: the client and `grant_expires_at`. Never deleted; `RESTRICT` to both the token and the client, so a wrong cleanup fails loudly instead of erasing a grant. |
| `oauth_refresh_token` | Every refresh token a grant has held, by hash, with `rotated_at`. A live grant has exactly one unrotated row; rotated rows are kept, which is how reuse is detected. |
| `oauth_event` | The content-free audit trail: `occurred_at`, `event` (`client_registered`, `authorization_granted`, `authorization_denied`, `code_redeemed`, `refresh_used`, `refresh_reuse_revoked`, `grant_revoked`), `outcome` (`succeeded`, `refused`), and the client, account, workspace and token ids where known. No foreign keys, so it outlives client cleanup. |

The grant's id is the token's id (`token_id`): it is the id `rheo token list` prints, the id
`rheo token revoke` takes and the `token_id` column of `rheo token events`. There is no separate
grant id.

**Clients: cleanup and the cap.** A client is **abandoned** when no `oauth_grant` row has ever
existed for it, it is older than `abandoned_client_minutes`, and none of its authorization requests
or codes is still live. Registration deletes abandoned clients first (never inside the hour the
per-source count looks at), and authorization rows that ended more than a day ago and issued
nothing; there is no background job. A client with any grant, live, expired or revoked, is never
deleted. A **counted** client is one that is not abandoned and either has no grant yet or holds a
live grant (not revoked, before `grant_expires_at`); a client whose grants are all dead is kept
for the listing but not counted. Registration is refused `429 temporarily_unavailable` when the
counted total is at `max_clients`, or when the request's source registered
`registrations_per_source_per_hour` clients in the last hour; both refusals are recorded. Refresh
and existing grants are never affected by the cap. The source is the ASGI client address, which
behind a reverse proxy is the proxy's unless the server trusts its forwarded headers
(`FORWARDED_ALLOW_IPS`), so behind a proxy the per-source limit acts as one global limit
([deploy](../../deploy/README.md#mcp-connector-sign-in)).

**Reading the trail.** `oauth_event` is read only through the operation `core.oauth_event.list`
(class `read`, roles owner and operator; `limit` 1 to 500, default 50), newest first. It returns
exactly the table's columns. An owner sees their workspace's rows; the operator also sees rows
bound to no workspace (registrations and refusals before a workspace was chosen). The operator
command is `rheo token events --workspace <id> [--limit N]`, one tab-separated line per event:
time, event, outcome, account, workspace, client id, token id. `rheo token list [--workspace]
[--account]` lists every token row with its issuing authority, and for a connector grant the
client name and grant end; it never prints a value or hash. `rheo doctor` adds `oauth surface`
and `connector grants` (counted clients against the cap, grants active, expired, revoked and
ending within 7 days, and live grants with no live refresh row); the second is `FAIL` at the cap
or when a live grant cannot refresh.

**Access logs.** uvicorn logs every request line with its query string, and `/auth/oauth/authorize`
carries the PKCE challenge and `state` there. `rheo_core.log_config.RedactQueryFilter`, attached
to `uvicorn.access` after both listeners' configs are built, drops the query string from every
access line. It also stops the pre-existing logging of GitHub's `GET /auth/callback?code=...`. A
reverse proxy's own access log is outside the process and is the operator's to check.

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
    modules:  { recallatron: { host: "recall", path: "/recallatron" }, ... }  # loaded manifests
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

Building the object validates `routing.identity.path`: an empty or root path, or one that is
not canonical (no leading slash, a trailing or doubled slash, a `.` or `..` segment, a character
outside RFC 3986's unreserved set), raises `IdentityPathInvalid` naming that key, so a bad value
fails startup instead of sending the browser round an `/auth/*` loop (issue #155). The web
tier's routing fetch times out after 2 seconds; a timeout takes the same "routing unavailable"
path as any other failure, logs one warning, and is not cached, so the next request tries again.

### The routing table

The reverse proxy's rules are the same in both modes; only the host matching changes:

| Request | Goes to |
| --- | --- |
| Any application host, `/auth/*` | `core` (identity endpoints) |
| `api` surface | `core` (HTTP API, intake receiver); token auth |
| `mcp` surface | `core` (MCP facade); token auth. In subdomain mode the whole `mcp` host, which includes its OAuth metadata documents |
| Path mode only: `/.well-known/oauth-protected-resource/mcp` (and `.../mcp/`) and `/.well-known/oauth-authorization-server/mcp` | `core` (the OAuth metadata documents, [connector sign-in](#connector-sign-in-oauth-for-mcp-clients)) |
| Any application host, everything else | `web` |
| apex, `docs`, `tuttle` | not this application |

"Application host" means the shell host, the identity host, and every module host in subdomain
mode, and the single host in path mode. Inside `web`, the module's route tree is mounted at
`url_for(module, "/")`, so a module's screens are the same components under either topology.

The `mcp` surface is matched inside `core` as well as at the proxy. In path mode `core` serves
MCP at the surface path (`/mcp` and `/mcp/`). In subdomain mode it serves MCP at `/` on the `mcp`
host, and when [connector sign-in](#connector-sign-in-oauth-for-mcp-clients) is configured, the
two OAuth metadata documents at three `GET` paths: `/.well-known/oauth-protected-resource`, the
same with a trailing `/`, and `/.well-known/oauth-authorization-server`. Nothing else is served
there: the `mcp` host is not an application host, so `/auth/*`, the HTTP API and `/healthz`
answer 404 on it, a metadata path answers `405` with `Allow: GET` to any other method and 404
when the feature is unconfigured, and no other host reaches MCP. The SDK's DNS-rebinding
protection admits only the surface's own `Host` values
([the MCP facade](runtime-and-mcp.md#the-mcp-facade)).

**The path-mode limit.** In path mode `core` serves the documents at the path-inserted root paths
`/.well-known/oauth-protected-resource/mcp` (and `.../mcp/`) and
`/.well-known/oauth-authorization-server/mcp`, on any host, as it serves MCP itself; there is no
fallback at the bare `/.well-known/oauth-authorization-server`. Under the proxy rules above those
root paths would otherwise fall to `web`, so a real single-host front must route the two
`/.well-known/.../mcp` paths to `core`. The repository ships no such front configuration (the
flagship overlay is subdomain mode), and the tests prove path mode against the core ASGI
application only.

### The reference deployment's hosts

Recorded from the ratified requirements; each is configuration a self-hoster may change.

| Host | Role | Served by |
| --- | --- | --- |
| apex `rheo.stream` | Project and marketing page | Not the application. Never receives the session cookie. |
| `circuit.rheo.stream` | The application shell and the workspace switcher | `web` |
| `auth.rheo.stream` | Login, the OAuth callback, the session grant, and connector sign-in's `/auth/oauth/*` (register, authorize, consent, token) | `core` |
| `leads.`, `current.`, `recall.`, `relationships.` | Module surfaces | `web` |
| `api.rheo.stream` | HTTP API and intake | `core`, token auth |
| `mcp.rheo.stream` | MCP facade, and connector sign-in's two OAuth metadata documents when it is configured | `core`, token auth; the metadata is public `GET` |
| `docs.rheo.stream` | Documentation | Not the application. |
| `tuttle.rheo.stream` | Reserved for the back-office integration surface | Not the application; nothing in release one. |
| `app.rheo.stream` | Kept in reserve as a permanent redirect to the same path and query on `circuit.` | Reverse-proxy configuration in `deploy/compose.flagship.yaml`, not part of this specification's application routing. Not an application host (issue #174). |
| `recallatron.rheo.stream` | Permanent redirect to the same path and query on `recall.` | Reverse-proxy configuration in `deploy/compose.flagship.yaml`. Not an application host: nothing the application generates links to it (issue #158). |

**What the flagship serves today.** Run 0d deployed the reference instance with
`deploy/compose.flagship.yaml` (PR #144). It routes `circuit.`, `auth.`, `recall.`, `api.` and
`mcp.`, plus the `recallatron.` and `app.` redirects. `leads.`, `current.` and `relationships.`
have no module behind them yet, so the overlay routes none of them; `docs.` is not configured
either. The apex serves the project page from elsewhere.

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
