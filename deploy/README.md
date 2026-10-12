# Deployment templates

The local stack (`deploy/compose.yaml`): `make up` brings up three services, `postgres`
(`pgvector/pgvector:pg16`), `core` (the FastAPI app, built from the repository
root `Dockerfile`), and `worker`. Both `postgres` and `core` are published to the
host — `core` on `RHEO_CORE_PORT` (default `8000`) and `postgres` on `RHEO_PG_PORT`
(default `5432`); override either if its default is already bound by another
project on your machine. Neither container's own port changes: `core` always
answers on `8000` and `postgres` on `5432` inside the compose network regardless
of a host-side remap. `make down` tears the stack down.

The **test-only cluster** (`deploy/compose.test.yaml`) is separate from all of the
above: a single throwaway `postgres` with durability off and a disposable data
volume (tmpfs through the `compose.test.tmpfs.yaml` overlay), for `make test-fast`
only. It runs in its own compose project per host port
(`rheo-stream-test-<port>`, default 5434), so it never shares state with `make up`'s
stack. See [tests/README.md](../tests/README.md).

The **`worker` service** runs the same image as `core` — one Dockerfile, one
tracked tag, no second image to maintain — with its command overridden to
`python -m rheo_app_worker.main`; that command override is the only difference
between the two. It publishes no port, shares the same `rheodata` volume `core`
mounts, and starts and stops with `make up`/`make down` like every other
compose service.

The **data-root volume**: `core` writes checkout-external runtime state (the file
secret backend, deployment config, per-workspace upload/export/scratch directories —
see `docs/architecture/storage-and-workspaces.md` § The data root) under
`/var/lib/rheo-stream` inside the container, mounted from the named volume
`rheodata`. It is a **named volume, never a bind mount into the repository** — the
compose stack must write nothing into the tracked tree (criterion 2), and a bind
mount would do exactly that. Set `RHEO_DATA_ROOT` to point `core` at a different
path instead (e.g. for a host-side operator command sharing the container's data);
`RHEO_IN_CONTAINER=1` is what makes `/var/lib/rheo-stream` the in-image default when
`RHEO_DATA_ROOT` is unset. The image creates that directory at mode 0700, and its
entrypoint, `deploy/core-entrypoint.sh`, re-applies 0700 at every container start,
because a volume created by an older image keeps its old mode (issue #161). It
touches only the in-image path; a root moved with `RHEO_DATA_ROOT` is the operator's
to lock down, and the application warns if it is wider than 0700.

**Health checks.** Both compose files give `core`, `worker` and `postgres` a health
check, and the flagship overlay gives `web` one too (issue #156). `core` probes its
own `/healthz` and `web` its own `/healthz`; neither calls the database. `worker`
has no port, so its loop touches a heartbeat file (`RHEO_WORKER_HEARTBEAT_FILE`)
once per iteration and the probe (`python -m rheo_app_worker.heartbeat`) fails
once that file is more than ten minutes old. A worker waiting out a database outage
still counts as healthy; `postgres` has its own `pg_isready` check. `core` gets a
300-second start period, because a cold start runs migrations before it listens.
That figure was sized when every start also re-synced the virtualenv; the image no
longer does (issue #152), so it can shrink.

The **0b1 operator sequence**, run against a freshly migrated cluster, in this exact
order:

```
rheo migrate
rheo account create --provider <id> --subject <subject> --display-name <name>
rheo workspace create --owner <account id>
```

`account create` must run before `workspace create --owner`: `membership.account_id`
is a foreign key to `account`, so a workspace's owner account has to exist first.
`account create` and `workspace create` each print exactly the new id and a newline
to stdout (every diagnostic goes to stderr), so a script can capture the id directly
from the command's output. Also available: `rheo workspace repair|list|status` and
`rheo doctor`. `rheo member add --workspace <id> --account <id> --role owner|member`
is how a second membership is created in release one — there is no self-service
invitation flow yet, so an operator runs this after the second identity has signed
in through the provider at least once (`resolve_or_create` needs the `account` row
to exist first); it mints no id of its own and prints only a confirmation to
stderr.

## Routing and application hosts

`core` and the web tier implement one URL topology in two modes
(`routing.mode`, `docs/architecture/identity-and-topology.md` § URL topology as
configuration has the full contract): **`path`** — a fresh install's default,
one host, every surface a path prefix — and **`subdomain`** — one label per
surface under a shared `base_host`, needing wildcard DNS and a wildcard
certificate for that one label. Both modes are one implementation, mode-branched
only inside `url_for`/`urlFor`; nothing in `core` or the web tier ever spells a
host or a topology-specific prefix out directly (`scripts/check_routing_literals.py`
is the mechanical check).

The reverse proxy's own rules are identical in both modes; only the host match
changes:

| Request | Goes to |
| --- | --- |
| Any application host, `/auth/*` | `core` (identity endpoints) |
| `api` surface | `core` (HTTP API); token auth |
| `mcp` surface | `core` (MCP facade); token auth |
| Any application host, everything else | `web` |
| apex, `docs`, the reserved integration host | not this application |

"Application host" means the shell host, the identity host, and every enabled
module host in subdomain mode, or the single host in path mode. `rheo routing
hosts` prints the exact list for the deployment as configured, then the OAuth
callback URL to register, hosts first, one per line:

```
$ rheo routing hosts
routing mode subdomain: 3 application host(s), then the OAuth callback URL
auth.example.test
circuit.example.test
leads.example.test
https://auth.example.test/auth/callback
```

Register that last line as the callback URL on the identity provider's OAuth app
(GitHub in release one) before setting `identity.providers.github.enabled = true`
— `/auth/login` builds the exact same URL server-side through `url_for`, so a
mismatch here is a `redirect_uri_mismatch` from the provider, not a bug in `core`.

**The internal listener (port 8100) is never routed by the reverse proxy, under
any configuration, and the web tier can reach it only from inside the container
network.** This is not a route the proxy happens to omit: `deploy/` publishes no
port for it, and the reference proxy template has no rule that could reach it
even by mistake. `make demo`'s own checkpoint asserts this mechanically — a
host-side request to `:8100` fails to connect, while the identical request run
inside the container network succeeds. The web tier's own reach to it is gated
the same way: `RHEO_CORE_INTERNAL_API_URL` only resolves to something when the
web tier itself runs on the compose network, which is why `make demo` (which
runs the web shell with `next start` on the host, outside compose) deliberately
leaves that variable unset and the shell renders its routing-unavailable state
instead of guessing at a URL. A deployment where web is not on the container
network has no route to the internal listener at all, by construction — not by
an oversight left to fix later.

`RHEO__routing__base_host` must be spelled **port-free**: `normalize_host` strips
the port before the application-host match, so a `base_host` carrying one can
never match and `/auth/*` refuses every request. `from_settings` refuses a colon
in that key. A direct deploy on a non-default port sets
`RHEO__routing__public_host` (for example `localhost:3000`); that value is the
authority `url_for` / `rheo routing hosts` print. Empty `public_host` inherits
`base_host`. This directory's own `compose.yaml` sets `localhost` and leaves
`public_host` unset — the documented reverse-proxy topology — and
`tests/test_routing.py` reads that file. `RHEO__routing__scheme` is the routing key most worth setting
explicitly in a non-TLS local topology: it is the one that is not already the
packaged default (`config/defaults.toml` ships `https`), so leaving it unset
silently changes the routing config the web tier reads. `.env.example` documents
the environment variables an operator sets by hand, including why
`RHEO_CORE_PUBLIC_URL` is spelled `localhost` rather than `127.0.0.1`;
`RHEO_CORE_INTERNAL_API_URL` and `RHEO_INTERNAL_SECRET` are documented there too.

Rate limiting of the webhook receiver and of `/auth/*` is the reverse proxy's
responsibility in release one; it is not implemented by `core` itself. This is a
recorded decision, not an omission — the reverse proxy is operator-provided and
generic per ratified decision D10.

Templates contain placeholders and secret references only. Runtime workspace data
belongs in private databases and volumes, outside source and image build contexts.
The root `.dockerignore` is a source-only starting policy; every future image and
package needs its own content review. The only deployment CI triggers is the
flagship's, described [below](#flagship-subdomain-mode-deployment).

## Flagship (subdomain mode) deployment

The overlay is `deploy/compose.flagship.yaml`; every variable it interpolates is
documented in `deploy/.env.flagship.example` (not re-listed here). The reference
instance at `rheo.stream` runs this overlay under Coolify, with GitHub sign-in on.

It routes five hosts, each on `${RHEO_BASE_HOST}`: `circuit`, `auth` and
`recall` (Recallatron's screens) reach `web` for everything except `/auth/*`,
which core serves on all three of those hosts; `api` and `mcp` each reach `core`
directly, over token auth. The apex host is not routed by this overlay.

Recallatron's host was `recallatron` until issue #158. That name is now
redirect-only: `recallatron.${RHEO_BASE_HOST}`, over https or plain http, gets a
permanent redirect straight to `https://recall.${RHEO_BASE_HOST}` with the same
path and query. Traefik answers 301 to a GET and 308 to any other method. The
existing wildcard record and certificate cover both names. Nothing the
application generates links to the old host, which exists only for old or
external links.

`app.${RHEO_BASE_HOST}` is reserved the same way (issue #174): over https or plain
http it gets a permanent redirect to `https://circuit.${RHEO_BASE_HOST}` with the
same path and query. It is not an application host either.

The wildcard certificate needs a DNS-01 resolver configured on the operator's own
reverse proxy, named by `RHEO_TLS_CERTRESOLVER`; the overlay only references that
name, it does not define the resolver. On a Coolify-fronted proxy, add the
resolver through Coolify's own proxy-configuration mechanism, never by hand-editing
the proxy's compose file directly. Coolify's database copy is the primary source,
and it overwrites the file on the proxy's next start. Supply the DNS provider token to
the proxy container by a `*_FILE` path, never as a plain environment value. Re-add
the resolver after any "Reset configuration" action in the proxy admin UI, which
regenerates the proxy's defaults and drops custom resolvers.

`RHEO_TRAEFIK_NETWORK` is the Docker network the deploy platform creates for this
application and attaches its own proxy to.

A Coolify-hosted deploy also needs the Application settings listed in the header
comment of `deploy/compose.flagship.yaml`: see that header for the exact list,
not repeated here. The image starts from the virtualenv built into it and needs no
network access to PyPI at container start (issue #152).

Provisioning order, with no real ids below (placeholders only):

1. First deploy runs with GitHub sign-in disabled.
2. Create the owner's account by their numeric provider-subject id.
3. Create the owner's workspace.
4. Enable GitHub sign-in.
5. The owner signs in.
6. Issue operator tokens.
7. Install and enable the modules the deployment needs, through the API.

Two token kinds: a `cli`-kind token for API/CLI work, and a separate `mcp`-kind
token for the `mcp` host, which refuses a `cli` token as `token_wrong_kind`.

Pushes to `main` redeploy only once the three repository secrets `COOLIFY_TOKEN`,
`COOLIFY_BASE_URL` and `COOLIFY_APP_UUID` exist; until then
`.github/workflows/deploy-flagship.yml` is a green no-op that logs a notice and
skips the deploy job. The workflow starts when Repository checks completes on a
`main` push, and deploys only if every push-event CI workflow on that commit
succeeded and the commit is still the tip of `main` (Coolify builds the branch tip,
so an older commit steps aside for the newer one's own run). A green `deploy` job
means the Coolify deployment reached `finished`; a `failed` or `cancelled`
deployment, or one still running after 30 minutes, fails the job. The log carries
only HTTP codes and the deployment status, so read the build output in Coolify.

`COOLIFY_TOKEN` needs two Coolify API abilities: `deploy` to start the deployment,
and `read` to check the application's settings and poll the deployment's status.
`read` does not expose secret values (`read:sensitive` would). A 401 or 403 from
either read fails the job at once with a message naming the missing `read`
ability (issue #172); a 429 or 5xx from the status poll is treated as transient
and retried until the 30-minute limit.

Before it starts a deployment, the job reads the application and refuses to deploy
unless `settings.is_preview_deployments_enabled` is `false` (issue #164). Coolify
keeps a separate preview copy of every application variable, and the flagship's
preview copies of its secrets are empty, so a preview deployment would start
without them. Keep preview deployments off.

### Connection budget

The flagship runs the packaged pool defaults, so it needs no pool setting of its
own: `storage.pool_cache_size` 6 and `storage.pool_max_connections` 5 give each
process 6 * 5 + 6 = 36 connections (six cached workspace engines, the control
engine, one maintenance connection). Core and worker make 72 against the overlay
postgres's stock `max_connections` of 100. `rheo doctor` reads `ok` while the pair
stays within 80 (80%); the remaining 28 cover an operator's `rheo` command, which
is a third process with engines of its own (the phase-1b migration run is one),
plus `psql` and the superuser-reserved slots. To move a number, set
`RHEO__storage__pool_cache_size` or `RHEO__storage__pool_max_connections` in the
application's environment, or raise `max_connections` on the postgres service,
then re-run `rheo doctor` (issue #162).

### Rollback

1. **Fast path:** pin the deploy platform's app to the last known-good `main`
   commit SHA and redeploy. This rebuilds a known commit from source, so it needs
   no retained image. **Then unpin:** once the fix (or the revert in step 2) is on
   `main`, set the app back to tracking `main` HEAD and redeploy. A pinned app
   keeps rebuilding the pinned SHA, so later pushes would not ship.
2. **Durable path:** `git revert` the bad merge on `main`; the push redeploys the
   reverted state (after unpinning, if step 1 was used).
3. **Failed build:** the previous containers keep running; this is the platform's
   default.
4. **Limit:** migrations are forward-only. Rolling back across a migration
   boundary means restoring a verified backup taken before the incompatible
   migration, not reverting code, and not simply the most recent backup, which may
   postdate it. Any write made after that backup's point is lost on restore. Startup
   refuses a database that records a revision the image does not know
   (`schema_ahead`), so an older image does not start against a newer schema. The
   control chain's `0003_oauth` is such a boundary
   ([below](#mcp-connector-sign-in)): pin only to a commit that has it.
5. **Proxy change:** the shared-proxy resolver has its own rollback; rolling back
   the app never needs it, since an unused resolver is inert.

### MCP connector sign-in

`core` can let an MCP client that cannot send an `Authorization` header (a
claude.ai custom connector) sign in through an OAuth flow on the identity host and
receive an ordinary `mcp` token (issue #287;
`docs/architecture/identity-and-topology.md` § Connector sign-in has the design).
It ships off: with `identity.oauth.enabled` false the `mcp` host answers exactly as
before, a bare `Bearer` 401, and the metadata paths and `/auth/oauth/*` are 404.

**The migration comes first, and it is forward-only.** Any image with this feature
applies the control chain's `0003_oauth` at core startup, whether or not the feature
is on: six new `control.oauth_*` tables, and the `access_token_issued_from` CHECK
dropped and recreated with a fourth value, `connector`. No column changes, and every
existing token satisfies the wider CHECK. Its downgrade raises. A pre-`0003` image
then refuses to start against the migrated control plane (`schema_ahead`), so a code
rollback to it is not supported. Recover from a bad deploy by rolling forward: a fix
on `main`, or a pin to an earlier commit that already has `0003_oauth`. The only way
back past it is restoring a control-plane backup taken before it (Rollback item 4).

**Before turning it on:**

1. GitHub sign-in is enabled with a client id, and `routing.identity.path` is the
   default `/auth`. The OAuth routes are served under `/auth`; with any other prefix
   the surface stays unconfigured.
2. Check the reverse proxy's access log. `core` drops the query string from its own
   uvicorn access log (`rheo_core.log_config.RedactQueryFilter`), which also stops the
   GitHub `GET /auth/callback?code=...` line it used to write. A proxy in front logs
   request lines itself: the first `/auth/oauth/authorize` line carries the PKCE
   challenge and `state` in its query. Confirm the proxy's access log is off or strips
   query strings. On a shared proxy this is the proxy's configuration, not the
   application's; the repository ships no proxy log configuration.
3. Decide on `FORWARDED_ALLOW_IPS`. Registration is limited per source address
   (`identity.oauth.registrations_per_source_per_hour`, default 30), and the source is
   the ASGI client address. uvicorn trusts `X-Forwarded-For` only from `127.0.0.1`
   unless `FORWARDED_ALLOW_IPS` names the proxy, so behind a proxy in another container
   every registration comes from the proxy and the limit acts as one global limit of
   30 an hour. That still bounds a flood; `identity.oauth.max_clients` (default 100
   counted clients) is the real bound. Setting `FORWARDED_ALLOW_IPS` in core's
   environment to the proxy's address makes it per client. Either is a valid choice.
   This limit is core's own; the general `/auth/*` rate limiting above stays the
   proxy's.

**Turning it on.** Set `RHEO__identity__oauth__enabled=true` in core's environment
and restart core. The surface is resolved when core starts, so every
`identity.oauth.*` change takes effect at restart. Then run `rheo doctor`:

- `oauth surface ok configured: issuer <issuer>; <n> allowed redirect URIs` is ready.
- `oauth surface FAIL enabled but unconfigured: <reason>` means enabled but
  incomplete, and nothing is advertised: `identity_provider_disabled` (GitHub sign-in
  off or no client id), `allowlist_empty`, or `setting_invalid` (an allowlist entry
  that is neither `https` with a host nor `http` on `localhost` or `127.0.0.1`, or
  that carries userinfo, a fragment or an unparseable port; an identity path other than `/auth`, or a routing host that
  cannot sit in a `WWW-Authenticate` header).
- An `identity.oauth.*` integer below its minimum (for example
  `access_token_minutes` under 5) refuses the settings load, so core does not start
  at all, and doctor's `settings` line fails with the key.

`connector grants` reports the counted clients against the cap and the grants; it is
`FAIL` at the cap or when a live grant has no live refresh token.

**The connector URL** is `url_for(mcp, "/")`, trailing slash included, and that exact
string is the OAuth resource identifier the metadata names. On a subdomain deployment
it is `https://mcp.<base host>/`; the reference instance's is
`https://mcp.rheo.stream/`. In path mode it is `https://<host>/mcp/`. Add the
connector with that URL and choose automatic client registration; the client then
registers, signs in through GitHub on the identity host and shows the consent page.

**The redirect allowlist** (`identity.oauth.redirect_uris`) ships as one entry, the
callback Claude's connector documentation names:
`https://claude.ai/api/mcp/auth_callback`. A second callback,
`https://claude.com/api/mcp/auth_callback`, is mentioned elsewhere but not confirmed.
If a surface turns out to use it (registration refused `invalid_redirect_uri`, with a
`client_registered` `refused` row in `rheo token events`), add it as one line of
configuration. A list setting is comma-separated in the environment and replaces the
default, so name both:

```
RHEO__identity__oauth__redirect_uris=https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback
```

**Operating it.** `rheo token list` shows each grant as one `connector` row with its
client name and grant end; `rheo token revoke <id>` ends it. `rheo token events
--workspace <id>` prints the content-free sign-in trail, newest first. The grant id is
the token id those commands print. The token endpoint's `invalid_client` 401 carries
no `WWW-Authenticate` header: the client is public and sends no `Authorization`
header, so there is no scheme to challenge.

**The path-mode limit.** In single-host path mode `core` serves the two metadata
documents only at the path-inserted root paths
`/.well-known/oauth-protected-resource/mcp` (and `.../mcp/`) and
`/.well-known/oauth-authorization-server/mcp`; there is no root `/.well-known`
fallback. Under the routing table above those paths would reach `web`, so a real
single-host front must route those two `/.well-known/.../mcp` paths to `core`. This
directory ships no such front configuration, and the tests prove path mode against
the core application only. The subdomain flagship needs no proxy change: the whole
`mcp` host and `/auth/*` already reach `core`.

### Upgrading installed modules

A green `/healthz` confirms process liveness, not module schema compatibility.
Startup and `rheo migrate` apply the control/core chains; they do not upgrade
already installed module schemas. Include the explicit readiness check in every
release verification, using the new core image and its normal deployment settings:

```sh
rheo module check --all
```

The command exits nonzero for an installed module whose package, schema or dependency
is incompatible. Output includes the workspace/module, problem, installed package
and loaded target. Removed modules are skipped. No data or lifecycle state changes.
An enabled incompatible module is shown as unavailable and its operations refuse;
other compatible modules keep serving.

Before deploying a module with pending schema changes, take and verify a complete
cluster backup (below). Review its frozen migrations and prepare the exact targets.
After starting the new image, run the following inside its core container for each
installation that needs upgrading, before declaring that module available:

```sh
rheo module upgrade --workspace "$WORKSPACE_ID" --module leads \
  --target-version 0.1.0 --target-schema 0008_attach_by_field
rheo module check --all
```

Use the target version/schema reported by the new image; the example names the
first supported Leads upgrade. This host-operator command is audited, preserves
installed/enabled/disabled state, and refuses stale targets, downgrades and pending
migrations lacking `non_destructive_upgrade = True`. It also refuses a destructive
marker; there is no force option. New installations still use install/enable.

If the command returns `upgrade_busy`, let active module work finish and retry.
Module calls in that workspace are briefly refused while an upgrade holds its lock.
Required extensions must already be provisioned.

Migration, health checks, package version and schema history are transactional.
After failure, correct the cause and retry the same target; a completed target is
a no-op. Do not reinstall, edit Alembic markers manually, or route a module chain
through `migrate_workspace`. Roll back by restoring the verified pre-upgrade backup
with the matching previous image; merely switching to older code can leave its
schema incompatible. Destructive upgrades require the future export/approval
workflow and are not supported by this command.

### Backups

A host cron job runs `deploy/backup/rheostream-pgdumpall.sh` once a night. It
dumps every database in the flagship Postgres with `pg_dumpall` (one database per
workspace, so a single-database dump would miss workspaces), then applies
retention. Coolify's own scheduled backups cover Coolify-managed database
resources, not a Postgres service inside a compose application, which is why this
is a host cron job.

Each run:

1. Finds exactly one running container with the compose labels
   `com.docker.compose.project=$RHEO_BACKUP_COMPOSE_PROJECT` and
   `com.docker.compose.service=postgres`, and refuses zero or several.
2. Pipes `pg_dumpall` through `gzip` into a hidden `.partial` file under
   `set -o pipefail`, so a failing `pg_dumpall` fails the run even though `gzip`
   succeeded.
3. Verifies the file: the gzip stream tests clean, it is at least
   `RHEO_BACKUP_MIN_BYTES` (1000) bytes, and it ends with pg_dumpall's
   "cluster dump complete" line, which a cut-off dump lacks.
4. Renames it to `pgdumpall-YYYYMMDDTHHMMSSZ.sql.gz` (UTC) in
   `RHEO_BACKUP_DIR` (`/root/rheostream-backups`), readable by root only.
5. Applies retention. A run that failed at any earlier step exits non-zero
   before this, so a failed dump never deletes an older one.

Retention keeps the newest dump from each of the 14 most recent days that have
one, plus the newest dump from each of the 8 most recent Sundays (UTC) that have
one. The two sets overlap, so a steady nightly schedule holds about 20 files.
Retention counts dumps and never compares a dump's date with the clock, so a
clock that jumps forward or back cannot age out the real dumps. On top of that it
never deletes the dump the run just wrote, the newest good dump by name, or the
most recently modified dump when it verifies. If no dump verifies, it deletes
nothing and exits 3. Files not named like a dump are never touched.

Log lines go to syslog under the tag `rheostream-pgdumpall`
(`journalctl -t rheostream-pgdumpall`), and errors also go to stderr. Set
`RHEO_BACKUP_LOG=stderr` to log to stderr only, or `RHEO_BACKUP_LOG_FILE` to
append to a file as well. The script's header lists every setting.

Dumps stay on the same host as the database. Copying them off the host is not
set up yet.

The script also has `prune [--dry-run] [--keep FILE]` (retention only) and
`verify FILE`. `tests/test_backup_retention.py` runs the retention and
failure paths against fake dump files, with no Postgres or Docker.

#### Install or update on the host

Run as root on the host. Pin `SHA` to the `main` commit that carries the version
you want, and compare the checksum with the same file in your own checkout
(`git show "$SHA:deploy/backup/rheostream-pgdumpall.sh" | sha256sum`).

```sh
set -euo pipefail
SHA=<main commit sha>
CRON=/etc/cron.d/rheostream-pgdumpall
# 1. Keep the current cron file outside /etc/cron.d for the undo.
[ -e "$CRON" ] && cp -p "$CRON" /root/rheostream-pgdumpall.cron.bak
# 2. Fetch the pinned script, check it, install it.
curl -fsSL -o /root/rheostream-pgdumpall.new \
  "https://raw.githubusercontent.com/rheos/rheostream/$SHA/deploy/backup/rheostream-pgdumpall.sh"
sha256sum /root/rheostream-pgdumpall.new      # must match your checkout
install -o root -g root -m 755 /root/rheostream-pgdumpall.new /usr/local/sbin/rheostream-pgdumpall
rm -f /root/rheostream-pgdumpall.new
# 3. The compose project label. On an update, reuse the one the cron file has;
#    on a first install, set it to the application's compose project.
PROJECT=$(grep -oE '(com\.docker\.compose\.project=|RHEO_BACKUP_COMPOSE_PROJECT=)[A-Za-z0-9_-]+' "$CRON" | head -1 | cut -d= -f2)
[ -n "$PROJECT" ]
# 4. See what retention would delete before anything is deleted.
RHEO_BACKUP_LOG=stderr /usr/local/sbin/rheostream-pgdumpall prune --dry-run
# 5. Write the cron file (the name must stay dot-free or cron ignores it).
umask 022
printf 'SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\nRHEO_BACKUP_COMPOSE_PROJECT=%s\n15 3 * * * root /usr/local/sbin/rheostream-pgdumpall\n' \
  "$PROJECT" > /root/rheostream-pgdumpall.cron.new
install -o root -g root -m 644 /root/rheostream-pgdumpall.cron.new "$CRON"
rm -f /root/rheostream-pgdumpall.cron.new
# 6. Run it once in cron's environment and check the result.
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin SHELL=/bin/bash HOME=/root \
  RHEO_BACKUP_COMPOSE_PROJECT="$PROJECT" RHEO_BACKUP_LOG=stderr /usr/local/sbin/rheostream-pgdumpall
ls -la /root/rheostream-backups
journalctl -t rheostream-pgdumpall -n 20 --no-pager
```

The job runs at 03:15 in the host's local time zone, while dump names use UTC.
Step 6 writes a second dump for the current UTC day, so retention removes the
earlier dump from that day. That is expected.

**Undo.** This puts the previous cron job back and removes the script. Dumps
already on disk stay, but retention deletions cannot be undone, which is what
step 4's dry run is for.

```sh
set -eu
install -o root -g root -m 644 /root/rheostream-pgdumpall.cron.bak /etc/cron.d/rheostream-pgdumpall
rm -f /usr/local/sbin/rheostream-pgdumpall
```

#### Restore

A `pg_dumpall` file recreates roles and databases, so restore it into an empty
cluster, for example a Postgres container started on a new, empty volume. On a
cluster that already has the databases, the `CREATE` statements fail.

```sh
gzip -t pgdumpall-YYYYMMDDTHHMMSSZ.sql.gz
gzip -dc pgdumpall-YYYYMMDDTHHMMSSZ.sql.gz | docker exec -i <postgres container> psql -U rheo -d postgres -v ON_ERROR_STOP=0
```

Read the `psql` output. Errors saying the `rheo` role and the `rheo` database
already exist are expected, since the image creates both at first start (the
database takes its name from `POSTGRES_USER` when `POSTGRES_DB` is unset), and the
dump then restores into that empty database. Any other error needs a look.

### Scheduled doctor and paging

A host cron job runs `deploy/doctor/rheostream-doctor-alert.py` once an hour. It
runs `rheo doctor` inside the flagship's core container and turns every `FAIL`
line into a critical ticket in the operator's ticket service, which pages on a
critical ticket's creation. A repeat failure updates the same open ticket (same
`source_ref` and `ticket_type`) rather than paging again, and when the check
recovers the script closes the ticket, so the next failure pages afresh. The
script's header has the ticket service contract (list open, create or dedupe,
close; bearer auth).

Each run:

1. Reads `TICKETS_API_KEY` from `RHEO_DOCTOR_ENV_FILE`
   (`/root/.config/rheostream-doctor.env`, root-only) and lists this pager's open
   tickets from `RHEO_DOCTOR_TICKETS_URL`. That list is also the preflight: if the
   service is unreachable or rejects the key, the run exits 2 before doctor's
   result counts, so a broken pager shows up in syslog instead of passing quietly.
2. Finds exactly one running container with the compose labels
   `com.docker.compose.project=$RHEO_DOCTOR_COMPOSE_PROJECT` and
   `com.docker.compose.service=core`. Zero or several becomes a `doctor run` FAIL.
3. Runs `rheo doctor` there. A non-zero exit with no `FAIL` line is also a
   `doctor run` FAIL.
4. Files each `FAIL` line as a ticket (`ticket_type: infra-alert`, `severity:
   critical`, `provenance: status-poll`, `source_ref: rheostream-doctor:<check>`,
   plus any fields in `RHEO_DOCTOR_TICKET_FIELDS`, a JSON object). The ticket body
   is the FAIL line; doctor's lines are counts, ids and settings keys only.
5. Closes every open `rheostream-doctor:` ticket whose check no longer fails. When
   doctor itself could not run, no other check was evaluated, so nothing is closed.

Exit codes: 0 all ok, 1 at least one FAIL paged, 2 the pager could not do its job.
Log lines go to syslog under the tag `rheostream-doctor`
(`journalctl -t rheostream-doctor`). `--dry-run` prints what it would file or
close and writes nothing. `tests/test_doctor_alert.py` covers every path against
a stub `docker` and a stand-in ticket service.

#### Install or update on the host

Run as root. Pin `SHA` to the `main` commit that carries the version you want, and
compare the checksum with the same file in your own checkout.

```sh
set -euo pipefail
SHA=<main commit sha>
curl -fsSL -o /root/rheostream-doctor-alert.new \
  "https://raw.githubusercontent.com/rheos/rheostream/$SHA/deploy/doctor/rheostream-doctor-alert.py"
sha256sum /root/rheostream-doctor-alert.new      # must match your checkout
install -m 0755 -o root -g root /root/rheostream-doctor-alert.new /usr/local/sbin/rheostream-doctor-alert
rm /root/rheostream-doctor-alert.new
# The ticket service key, once: root-only.
install -d -m 0700 /root/.config
umask 077; printf 'TICKETS_API_KEY=%s\n' '<key>' > /root/.config/rheostream-doctor.env
cat > /etc/cron.d/rheostream-doctor <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RHEO_DOCTOR_COMPOSE_PROJECT=<compose project label>
RHEO_DOCTOR_TICKETS_URL=<ticket service API base>
RHEO_DOCTOR_TICKET_FIELDS=<JSON object of service-specific fields, or {}>
7 * * * * root /usr/local/sbin/rheostream-doctor-alert
CRON
# Check it without filing anything (same variables as the cron file):
RHEO_DOCTOR_COMPOSE_PROJECT=... RHEO_DOCTOR_TICKETS_URL=... /usr/local/sbin/rheostream-doctor-alert --dry-run
```

To stop paging, delete `/etc/cron.d/rheostream-doctor`. Any tickets it filed stay
open in the service until closed by hand.

### Signed website receiver migration

The signed website intake release adds control revision `0004_connector_locator`
automatically at startup. It is additive, but migrations are forward-only: an older
image that knows only `0003_oauth` refuses to start against that control database.
Keep a verified pre-migration backup; recover by rolling forward, or restore that
backup if intentionally crossing the migration boundary (with the write-loss
consequences described under rollback). Deployment does not create website keys or
connect source traffic. Owners create connections and the host operator hands a key
to the website server using the [private-file export](../modules/leads/README.md#signed-website-intake).

## Job feed collector

The Leads module can collect job postings from a job feed provider's remote MCP server
into a Job search pipeline. Each connected workspace has at most one collector
connection. It signs in to the provider as an OAuth client, with an owner's one-time
consent, and the worker polls the provider every two hours with the access that consent
grants. Nothing reaches the provider until the operator turns the client on and an
owner connects. Owner-facing behaviour is summarized in the
[Leads README](../modules/leads/README.md#job-feed).

### Settings

All four are deployment settings. Set them in the environment of both `core` (connect,
reconnect, the callback) and `worker` (collection); a Coolify deployment attaches its
`.env` to every service. Values below are placeholders.

```
RHEO__connectors__oauth_client__enabled=true
RHEO__connectors__oauth_client__client_name=Rheo Stream
RHEO__leads__jobfeed__resource_url=https://mcp.example.com/mcp
RHEO__leads__jobfeed__job_hosts=jobs.example.com
```

- `connectors.oauth_client.enabled` (default `false`) is the operator switch. While it
  is off, or while `routing.identity.path` is anything but the default `/auth`, client
  registration, connect, reconnect and the callback are all closed: connect and
  reconnect refuse `endpoint_misconfigured` before any request, and the callback route
  answers 404. A deployment that has not opted in sends nothing to any registration
  endpoint.
- `connectors.oauth_client.client_name` (default `Rheo Stream`) is the `client_name`
  sent in the registration request, and is used nowhere else.
- `leads.jobfeed.resource_url` is the provider's MCP resource URL. Empty (the default)
  leaves the job feed unconfigured: connect and reconnect refuse
  `endpoint_misconfigured`, and a scheduled run returns without writing anything.
- `leads.jobfeed.job_hosts` is a comma-separated list of host names that posting URLs
  must use. A row whose URL is not `https` on one of these hosts is counted as an
  error and dropped; stored posting URLs are rebuilt on the first host in the list.
  Empty (the default) also stops a run before it starts.

Environment changes take effect when `core` and `worker` restart. The connection pins
the endpoints it discovered at connect time, so a changed `resource_url` takes effect
for an existing connection only when the owner reconnects (see below).

### The callback and the redirect URI

There is one route, on the identity host: `GET /auth/connector/leads/callback`. The
redirect URI registered with the provider is built from the routing settings, never
from request data:

- subdomain mode: `https://auth.example.com/auth/connector/leads/callback`, with your
  `routing.base_host` in place of `example.com`;
- path mode: `https://example.com/auth/connector/leads/callback`, on the one public host.

The route reads only `state`, `code`, `error` and `iss`, each at most 2048 characters
and present at most once, and authenticates only by the owner's existing session cookie
on that host. It answers one of two fixed pages, "Job feed connected" or "Authorization
could not be completed; start again from Leads", and never echoes provider text. There
is no client metadata document to host: the client identity comes from dynamic
registration, and nothing else is served under `/auth/connector`.

### Client registration

The provider's authorization server must advertise a registration endpoint. Discovery
reads the protected-resource metadata at the configured URL, its first authorization
server, and that server's metadata, which must support S256 PKCE, the
`authorization_code` and `refresh_token` grants, token endpoint auth method `none`, and
registration. A missing revocation endpoint is accepted.

- **When it registers.** Only inside an owner's connect or reconnect, after discovery
  and before the authorization URL is built. One request per owner action, never
  retried, and never from a worker run. A first connect always registers.
- **The request.** One JSON `POST` with no `Authorization` header and exactly five
  fields: the one redirect URI, the two grant types, `response_types` `code`,
  `token_endpoint_auth_method` `none` and `client_name`.
- **Reuse.** A reconnect reuses the stored client while the freshly discovered issuer,
  the registration endpoint and the current redirect URI all equal the stored ones.
  Otherwise it registers a new client. Changing the identity host (for example a new
  `routing.base_host`) therefore registers a new client at the owner's next reconnect.
  The identity path cannot move: any value other than `/auth` turns the whole client
  off.
- **Authentication method.** The registration response decides it: its
  `token_endpoint_auth_method` when present, else `none` without a client secret and
  `client_secret_basic` with one. `none`, `client_secret_basic` and
  `client_secret_post` are supported. A client secret and a registration access token,
  when issued, are stored as files (see Revoking); the registration access token is
  never used.
- **A rejected client.** An `invalid_client` answer from the token endpoint to the
  stored client, at a refresh or a reconnect's code exchange, or a stored client secret whose expiry has passed or which cannot be read, makes the
  connection terminal (`authorization_required`) and clears the stored client. Nothing
  registers automatically after that. The owner's next reconnect registers exactly once.
- **Restore.** An export never carries the registered client, so a restored connection
  has none and the owner's reconnect registers a new one.
- **Accumulation.** This code never deletes a registration at the provider. Replaced
  clients, clients from abandoned connect attempts and clients left behind by a restore
  stay registered there, unused.

Refusals an owner can see from connect or reconnect:

| Code | Meaning |
| --- | --- |
| `registration_failed` | The provider did not accept the registration: a status other than 200 or 201, a timeout, a body over 16 KiB, a malformed body or `client_id`, an echoed redirect URI that differs, or echoed grant types missing one of the two. Nothing is stored. |
| `registration_unsupported` | The provider issued a client this code cannot use: an authentication method outside the three supported ones, or a secret-based method with no secret. Nothing is stored. |
| `endpoint_misconfigured` | The client is off, the resource URL is unset, discovery failed, or the registration endpoint redirected or does not resolve to a public `https` address. In the first three cases nothing is sent to the registration endpoint. |

`client_rejected` is not a registration refusal. It is the provider rejecting an
already registered client (an `invalid_client` answer, or a stored secret that has
expired or cannot be read). A stored client rejected at the code exchange or at a
scheduled refresh is cleared and the connection goes terminal; one rejected at a revoke
is cleared and the connection stays revoked; a client the same connect or reconnect had
just registered is discarded. A failed registration on a
reconnect records `registration_failed` on the connection's health and changes nothing
else: an existing authorization keeps collecting.

### Connecting for the first time

1. Deploy with the settings above, then run the Leads module upgrade that adds
   `0009_jobfeed_collector` (see [Upgrading installed modules](#upgrading-installed-modules)).
   Deployment registers nothing and connects nothing.
2. An owner turns the workspace capability on: `core.settings.set` with
   `leads.capabilities.jobsearch` `true`. An operator value of `false` vetoes it for
   every workspace.
3. The owner creates a pipeline from the Job search preset, opens **Connections** in
   Leads, chooses that pipeline and selects **Connect job feed**.
4. The owner signs in at the provider and consents. This one-time consent is the owner's
   own action at the provider's site; no part of this deployment performs it.

The authorization request expires after 10 minutes and its state is single-use and
bound to the owner's account. A first connect that fails at any step leaves no
connection, no health row, no pending authorization and no secret file.

Check these on the first real connect, because no test can:

- the provider accepted the registration (no `registration_failed` or
  `registration_unsupported`) and the redirect URI it was given;
- the browser came back to the callback and showed "Job feed connected";
- which authentication method the provider issued, and whether it issued a client
  secret or an expiring client. The method is the `client_auth_method` column of
  `leads.jobfeed_connection` in the workspace database (one row per workspace); the
  secret itself is never shown.

### Choosing an account

At the start of every run the collector asks the provider which accounts the login can
act as and stores that list as the account choices. It then selects the account
without any fallback:

- with an account stored, it uses that account only when it is listed exactly once and
  is a freelancer account;
- with none stored, it uses the only freelancer account, and stores it;
- otherwise the run ends with `account_selection_required` and makes no search.

A stored account that is missing from the list, listed twice, or no longer a freelancer
account stops the run; it is never swapped for another account. The health code is
written alone, with no backoff. The owner picks an account from the stored choices on
**Connections**; that calls nothing at the provider and makes the next run due no
earlier than the last run's start plus 60 minutes. The selected account id and the
choices are visible only to the owner's browser session.

### Reconnecting

**Reconnect** on **Connections** starts a new authorization for the existing
connection. On completion it re-pins the endpoints from fresh discovery, stores the new
refresh token, resets the backoff and clears the collector's health codes, and makes
the next run due no earlier than the last run's start plus 60 minutes. It is the only
way to pick up a changed `leads.jobfeed.resource_url`: until then each scheduled run
sees that the setting no longer matches the pinned URL, records
`endpoint_misconfigured` on health with no backoff, and does not run.

If the provider returns an error on a reconnect's callback (the owner declined, for
example), the connection is left as it was. A connected feed keeps collecting with its
existing authorization, and health shows `authorization_required` as a note that the
reconnect did not finish.

### Revoking

**Revoke access** on **Connections** ends the connection locally first: the token
reference is cleared, the state becomes `revoked`, the schedule is turned off and
health records `authorization_required`. Then it revokes that refresh token at the
provider's revocation endpoint, as the stored client, and deletes the token file. The
result reports `revoked`, `revocation_failed`, or `revocation_unavailable` when the
provider advertised no revocation endpoint (nothing is sent). The local end stands in
every case. If the provider rejects the client, the stored client is cleared so the
next reconnect registers a new one. Revoke stays available while the connection needs authorization. Opportunities
already collected are kept.

The owner's revoke needs the workspace capability on; with it off, revoke is refused
`capability_disabled`. To cut access while the capability is off, the host operator
removes the refresh-token file. Secret files live under the data root at
`secrets/ws/<workspace>/connection/<connection>/oauth/`. The token file is the leaf
named in `token_ref` of `leads.jobfeed_connection`; a client secret and a registration
access token, if the provider issued them, are other files in the same directory.
Removing the file revokes nothing at the provider. What happens next:

- when the capability is turned back on, the next run finds the file gone, makes no
  request, and moves the connection to `authorization_required` with its schedule off;
- an in-app revoke after that still ends the connection locally and reports
  `revocation_failed`, because there is no token left to send (or
  `revocation_unavailable` when no revocation endpoint is pinned);
- the owner reconnects to collect again.

### Health and the run report

The owner reads the connection through `leads.connection.health`, which the
**Connections** panel shows. Besides intake's own fields it carries a `collector`
object for the job feed connection: `authorization_state` (`connected`,
`authorization_required` or `revoked`), `last_completed_at`, `next_run_not_before`, the
selected account and the account choices, and the last completed run's nine counts:

| Count | What it counts |
| --- | --- |
| `fetched` | Search rows received, valid or not, before overlapping sightings are merged. |
| `imported` | Deliveries accepted for postings no opportunity tracks yet. Opportunities are created when the worker processes them. |
| `refreshed` | Deliveries accepted for postings an opportunity already tracks. |
| `skipped_dup` | Sightings with no change since the last delivery, plus deliveries intake already held. |
| `filtered` | Sightings marked applied or with more than 50 proposals. They still refresh an opportunity that tracks them. |
| `skipped_untracked` | Sightings that are not new discoveries and that no opportunity tracks. |
| `errors` | Invalid rows, discovery postings whose detail read failed or was not made, and deliveries intake refused or recorded as conflicts. |
| `detail_reads` | Detail requests made, at most 60 a run. |
| `pages` | Search pages requested. |

`next_run_not_before` is the later of the backoff's earliest start and the last run's
start plus 60 minutes, or empty when neither applies.

The collector writes eight health codes to `last_error`:

| Code | Meaning |
| --- | --- |
| `authorization_required` | The connection needs a reconnect: the refresh token was refused or is missing, the provider refused the access token, the client was rejected, or the owner revoked. On a connected feed it means only that a reconnect did not finish. |
| `token_refresh_failed` | The refresh failed in a way that may pass. Backoff applies. |
| `account_selection_required` | The owner must choose an account. No backoff. |
| `upstream_unavailable` | The provider was unreachable, failed, or answered in an unexpected shape. Backoff applies. |
| `upstream_rate_limited` | The provider rate-limited the run. Backoff applies, at least as long as its `Retry-After` up to the 24-hour cap. |
| `collection_partial` | The run finished, but a page, a detail read or a delivery failed, or the deadline cut it short. Also written alone when the deadline cut a run before collection began. |
| `endpoint_misconfigured` | An endpoint redirected or does not resolve to a public `https` address (backoff applies), or the configured resource URL no longer matches the pinned one (no backoff until the owner reconnects). |
| `registration_failed` | A reconnect's registration failed. Collection continues if the connection is connected. |

A completed run without a partial result clears the collector's own codes and leaves
intake's alone.

### Schedule, limits and backoff

- **Schedule.** The collector's schedule runs every two hours, on the hour. A completed
  authorization turns it on; a terminal loss or a revoke turns it off. The ticker
  enqueues a run only for a workspace whose capability is on.
- **Claim spacing.** Each run claims the connection before it calls the provider. No
  run starts earlier than the previous claim plus exactly 60 minutes, so a retried or
  duplicated job claims once.
- **One run.** One token refresh, never retried; the MCP session set-up; the account
  check; three searches (one page of best matches, up to five pages of the newest
  postings, one page of postings above 50 proposals), at most 80 rows in all; one
  detail read for each discovered posting that has 50 or fewer proposals and is not
  marked applied, at most 60; then the deliveries and the report, committed together.
- **Deadline and pacing.** A run has 420 seconds from its claim. Search and detail
  requests start at least 5.3 seconds apart, and none starts with too little of the
  deadline left; a cut run completes as `collection_partial`. Each MCP request has a
  50-second budget and each token, revocation or discovery request 15 seconds; a
  stalled read can add 5 seconds to either. Discovery as a whole is cut at 30 seconds.
- **Retries.** A search or detail request that the provider refuses with its
  sensitive-rate-limit tool error is retried once, at least 5.5 seconds later. A second
  refusal counts as rate limited. Nothing else is retried within a run.
- **Backoff.** A run that fails before any search page succeeds (the refresh, the
  session set-up, the account list, or every search request) records the failure code
  and waits: 2 hours after the first consecutive failure, doubling to a 24-hour cap. A
  provider `Retry-After` in seconds is honoured up to the same cap. Search pages that
  arrive but fail the collector's own checks do not back off; the run still completes
  as `collection_partial`. A completed run resets the backoff.
- **The capability turned off.** The ticker stops enqueuing, a run that starts finds it
  off and writes nothing, and a running run re-reads it between search pages (not
  between detail reads or deliveries) and, finding it off, stops and writes nothing
  more. Turning it back on resumes at the next scheduled tick; the connection and its
  authorization are untouched.

### Worker and database requirements

- **`idle_in_transaction_session_timeout`** must be unset (zero) or at least 480
  seconds for the worker's database sessions, whether it is set on the server, the
  database or the role. A run holds the worker's transaction
  open, idle, while it talks to the provider, for up to its 420-second deadline plus
  the final writes.
- **Module upgrades.** A `rheo module upgrade` attempted while a run holds the
  workspace's module execution lock is refused `upgrade_busy`. Retry it after the run.
- **Occupancy.** One worker runs each collection inline: up to about 7 minutes every 2
  hours for each connected workspace. Other jobs and outbox deliveries on that worker
  wait behind it. A core migration that needs the tables the run's readiness check read
  waits for the run to finish. About 16 connected workspaces would keep one worker busy
  full time.
- **Deploys.** A deploy stops the worker. A run still going when the 10-second stop
  grace period ends is killed and its deliveries roll back; the committed claim keeps
  the next run at least 60 minutes after that run started.

### Outbound requests

Every request to the resource URL, the discovery documents, the registration, token
and revocation endpoints goes over `https` only, follows no redirects, and goes only to
a host whose every resolved address is public. The rule applies to configured and
discovered endpoints alike, including the registration endpoint, and is checked before
every request. The check resolves the host and the HTTP client resolves it again when
it connects; that DNS-rebinding window is accepted, because the hosts are configured by
the operator or discovered from them over `https`.

### Accepted residuals

- Two changed sightings of a brand-new posting delivered in different runs before the
  first is processed can create two opportunities, because processing is asynchronous
  and each delivery looks for a tracking opportunity when it is accepted.
- Erasure does not block re-collection. Erasing a collected observation or opportunity
  removes the posting's dedupe memory, so the next sighting, changed or not, is treated
  as never delivered. It is delivered under a fresh sequence number when an opportunity
  still tracks the posting or the posting qualifies as a discovery, and when the
  opportunity is gone a new one is created. Closing an
  opportunity, unlike erasing it, keeps the posting attached: later sightings refresh
  the closed opportunity.
- No MCP tool exists for connect, reconnect, the callback completion, revoke, account
  selection or the health read. They work only for the owner on a browser session.

### Unknown until the first live connection

These depend on the provider and cannot be settled before an owner's first real
consent:

- whether the provider accepts a plain public-client registration (method `none`, no
  initial access token) and which redirect URI rules it applies;
- whether it issues a client secret or an expiring client anyway;
- refresh-token lifetime and whether it rotates on use, which decides how often the
  owner must reconnect;
- whether the `resource` parameter is required or rejected;
- the current shapes of the account-list and job-search responses;
- whether the discovery probe's 401 challenge carries a scope;
- the provider's rate limit values, against which the 5.3-second pacing and the
  420-second deadline are unproven.
