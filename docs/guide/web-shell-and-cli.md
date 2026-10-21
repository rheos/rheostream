# Web shell and CLI

## What it is

**The web shell** is the Next.js application where people work: sign in, switch
workspaces, use each module's screens, and approve what agents propose. It lives on the
`circuit.` host in subdomain mode, or at the root of a single host in path mode. It holds
no database connection and no domain rules; every read and write goes to the core.

**The `rheo` command** is the operator's tool. It creates accounts and workspaces, adds
members, issues and revokes tokens, migrates, upgrades modules, exports and restores,
enrolls bridges, and checks the deployment's health with `rheo doctor`. It is the only
surface that acts as the `operator`.

**The HTTP API** sits between them for scripts: one route shape over the same registered
operations.

## What you can do with it

In the shell:

- Sign in with GitHub. A fresh deployment admits exactly one person (the first account)
  and then closes signup unless the operator opens it.
- Switch the active workspace. Every request after that routes there with no id in any
  URL.
- Use each enabled module's screens: Recallatron's browse, search and possible
  duplicates; Leads' list, board, capture, connections and job views; Current's queue,
  items, projects and areas.
- Approve or refuse a held operation, reading exactly what it will do.
- Pick a theme: GreenStream dark (the default), Novadiem dark or Novadiem light.
- See a banner on every screen while any background delivery has failed.

From the command line:

- `rheo account create`, `rheo workspace create|repair|list|status|export|restore`,
  `rheo member add`
- `rheo token issue|list|revoke|events`, including `--discover` for discover-then-call
- `rheo migrate`, `rheo module check|upgrade`
- `rheo evidence enroll|rotate|revoke` for the Claude Code bridge
- `rheo connector export-key` to hand a website its signing key
- `rheo routing hosts`, `rheo web compose`, `rheo openapi`, `rheo doctor`

## How the shell works

**It talks to the core on a private listener.** The core has two listeners. The public
one serves `/auth/*`, the API and the MCP door. The internal one is a second port on the
container network that the reverse proxy never routes to. The shell calls it server-side
with the session secret in a header and a shared internal secret, and the core ignores
cookies there. A browser therefore cannot reach the internal listener at all, and the
public API stays cookie-free in fact, not just by policy.

**Screens are composed from module manifests.** Each module ships a TypeScript package
declaring its navigation, routes, record views, forms and search providers. `rheo web
compose` renders every installed module's contribution into one generated file at build
time. At request time the shell shows a contribution only when it was composed into the
build, its host is in the routing configuration, its module is enabled in this
workspace, and (for navigation) the person's role is allowed. A module installed on the
host but not enabled in the workspace contributes nothing visible.

**A module's screens cannot overreach.** Compose records the module's own read
operations as its screens' allowlist, and the shell refuses any other name before the
call leaves the web tier: a screen cannot call a mutating operation, another module's
operation or a core operation. Writes go only through the forms the manifest declares,
as a shell-owned server action that rechecks the session, the module and the host on
every submission. A static check holds module web code to its own files, React and the
web contract: no `fetch`, no environment, no cookies, no server actions of its own.

**One session, many hosts, no shared cookie.** In subdomain mode the shell, sign-in and
module screens live on different hosts. A cookie scoped to the parent domain would leak
to every host beneath it, including ones run by other software. So each host gets its
own host-only cookie holding a 256-bit secret minted for that host; the control plane
stores only its SHA-256. A host without a cookie bounces through the identity host,
which writes a single-use 60-second grant bound to a nonce held only in this browser, so
a grant minted in an attacker's browser cannot log you into their session. Logging out on
any host revokes the one session behind every host's secret.

**Path mode and subdomain mode are one implementation.** One routing configuration names
every host, and both codebases build every link through one `url_for` function. A route
string that names a host anywhere else fails a lint. CI runs both modes.

**Themes are data.** A theme is 43 tokens in six groups: palette, state, form controls,
type, shape and density, and six chrome tokens reserved for built-in themes. Module
screens are written against the tokens, and a third-party theme cannot change how an
approval reads.

**It runs anywhere Node runs.** The shell uses `next start` behind any reverse proxy,
with no edge functions and no hosting-platform features. A CI scan fails on a
platform-only import or an edge-runtime declaration.

## How the CLI and API work

**The CLI uses the same core packages.** It runs in the core image and talks to the
control plane and workspace databases through the same code the server uses, so an
operator command cannot take a shortcut the server would refuse. Commands that create
something print exactly the new id on stdout, so scripts can capture it.

**`rheo doctor` checks numbers you can verify.** It reports the connection budget against
Postgres's limit, the relation between pool idle time and the worker's reconcile
interval, whether the database role can create the extensions loaded modules need, each
bridge enrollment's token expiry, the connector sign-in surface, and connector grants
near their cap or unable to refresh. An hourly host job can turn every `FAIL` line into
a ticket that pages.

**The API is one route.** `POST /api/v1/operations/<operation name>` with the operation's
input as the JSON body, and nothing in the query string. It answers `succeeded`,
`pending` with an operation id, `approval_required` with an approval id, or a named
refusal. It accepts `cli` tokens only. Its OpenAPI document is generated from the
registry and committed, and the shell's client is generated from that document; nobody
hand-edits either.

## Why it's built this way

The shell is thin so that the rules live in one place. A screen, an agent and a script
all hit the same operations with the same checks, and none of them can do something the
others cannot.

Composition is what makes modules real. Adding a module's screens takes four steps
(install the package, add its web package, compose, build), and none of them touches a
core file.

## Where to look

- Specification: [web extension points](../architecture/module-contract.md#extension-points),
  [identity, sessions and URL topology](../architecture/identity-and-topology.md),
  [theme contract](../architecture/theme-contract.md),
  [request lifecycle and the HTTP API](../architecture/overview.md#request-lifecycle),
  [processes](../architecture/overview.md#processes)
- Code: [shell](../../apps/web/src/shell),
  [themes](../../apps/web/src/theme),
  [web contract](../../packages/web-contract),
  [internal routes](../../apps/core/src/rheo_app_core/internal_routes.py),
  [auth routes](../../apps/core/src/rheo_app_core/auth_routes.py),
  [CLI commands](../../apps/cli/src/rheo_app_cli/commands),
  [web compose](../../apps/cli/src/rheo_app_cli/web_compose.py)
- Tests: [web compose](../../tests/test_web_compose.py),
  [web contribution](../../tests/postgres/test_web_contribution.py),
  [sessions](../../tests/postgres/test_sessions.py),
  [routing](../../tests/test_routing.py),
  [CLI](../../tests/postgres/test_cli.py),
  [OpenAPI document](../../tests/test_openapi_document.py)
