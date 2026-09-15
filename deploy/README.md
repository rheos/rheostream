# Deployment templates

The local stack (`deploy/compose.yaml`): `make up` brings up three services, `postgres`
(`pgvector/pgvector:pg16`), `core` (the FastAPI app, built from the repository
root `Dockerfile`), and `worker`. Both `postgres` and `core` are published to the
host — `core` on `RHEO_CORE_PORT` (default `8000`) and `postgres` on `RHEO_PG_PORT`
(default `5432`); override either if its default is already bound by another
project on your machine. Neither container's own port changes: `core` always
answers on `8000` and `postgres` on `5432` inside the compose network regardless
of a host-side remap. `make down` tears the stack down.

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
`RHEO_DATA_ROOT` is unset.

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
never match and `/auth/*` refuses every request. The `localhost:3000` this
directory's own `compose.yaml` sets is a separate open issue and is deliberately
left alone here. `RHEO__routing__scheme` is the routing key most worth setting
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
package needs its own content review. No deployment runs from the initial CI.
