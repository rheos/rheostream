import {
  API,
  DOCS,
  IDENTITY,
  INTEGRATION,
  MCP,
  SHELL,
  type RoutingConfig,
  type SurfaceConfig,
} from "@/lib/routing/config";

/**
 * `urlFor` and friends: the only way the web tier names a URL (C9, B10).
 *
 * The TypeScript half of `packages/core/src/rheo_core/routing/url_for.py`. The two
 * implementations are mirrored deliberately rather than shared — there is no
 * Python-to-TypeScript import — so the guarantee that they agree comes entirely
 * from both being asserted against `tests/fixtures/routing/expected-urls.json`.
 * That is why the join below is the *normalised* one and not the naive
 * concatenation: two implementations agreeing on a contract neither honours would
 * satisfy a byte-identical check with two matching wrongs.
 */

/** Prefixes that contribute nothing: the empty prefix and the bare root. */
const ROOT_PREFIXES: ReadonlySet<string> = new Set(["", "/"]);

/**
 * Join a surface prefix to a path with exactly one `/` between them.
 *
 * The same three rules `_join_prefix` states in Python, in the same order:
 *
 * 1. `path` is normalised to begin with a `/`, so `""` becomes `"/"` and
 *    `"v1/ops"` becomes `"/v1/ops"`. Without this, `"/api" + "v1/ops"` is
 *    `/apiv1/ops` — a silently wrong URL rather than a loud failure.
 * 2. A root prefix (`""` or `"/"`) then contributes nothing, which is what stops
 *    a root surface path (`routing.shell.path` defaults to `"/"`) doubling the
 *    slash into `https://example.test//login`.
 * 3. Any other prefix (`/auth`) carries its own leading slash and never a
 *    trailing one, so concatenation is correct there.
 */
export function joinPrefix(prefix: string, path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (ROOT_PREFIXES.has(prefix)) {
    return normalized;
  }
  return prefix + normalized;
}

/**
 * The surface called `surface`, or `undefined`.
 *
 * Mirrors `Surfaces.named()` / `Surfaces.get()`: a named surface wins over a
 * module that happens to share its name, and `modules` — a mapping of surfaces,
 * not a surface — is never reachable as one.
 */
function surfaceFor(
  config: RoutingConfig,
  surface: string,
): SurfaceConfig | undefined {
  const surfaces = config.surfaces;
  const named: Record<string, SurfaceConfig> = {
    [SHELL]: surfaces.shell,
    [IDENTITY]: surfaces.identity,
    [API]: surfaces.api,
    [MCP]: surfaces.mcp,
    [DOCS]: surfaces.docs,
    [INTEGRATION]: surfaces.integration,
  };
  return named[surface] ?? surfaces.modules[surface];
}

/**
 * The absolute URL for `path` on `surface`, under this deployment's topology.
 *
 * Throws for an unknown surface, and for `docs`/`integration`: those are marked
 * `external`/`reserved` because this application never links to them.
 */
export function urlFor(
  config: RoutingConfig,
  surface: string,
  path: string,
): string {
  const spec = surfaceFor(config, surface);
  if (spec === undefined) {
    throw new Error(`unknown routing surface '${surface}'`);
  }
  if (spec.external === true || spec.reserved === true) {
    throw new Error(`routing surface '${surface}' is not served by this application`);
  }
  const surfacePath = spec.path ?? "";
  if (config.mode === "path") {
    return `${config.scheme}://${config.public_host}${joinPrefix(surfacePath, path)}`;
  }
  // Subdomain mode: every surface drops its prefix once it has its own host —
  // except `identity`, whose `fixed_path` holds `/auth/*` on every application
  // host in both modes.
  const prefix = spec.fixed_path === true ? surfacePath : "";
  const host = `${spec.host}.${config.public_host}`;
  return `${config.scheme}://${host}${joinPrefix(prefix, path)}`;
}

/**
 * The same-host path for an identity endpoint (`/continue`, `/logout`,
 * `/session/workspace`).
 *
 * Not `urlFor`: these are served by every application host, so the caller stays on
 * the host it is already on and this never crosses one. That matters beyond
 * tidiness — the `rheo_session` cookie is host-only, so a POST sent to a
 * *different* application host would arrive with no session at all.
 */
export function identityPath(config: RoutingConfig, path: string): string {
  return joinPrefix(config.surfaces[IDENTITY].path ?? "", path);
}

/**
 * Every host this application serves, sorted.
 *
 * Subdomain mode: the shell host, the identity host, and every enabled module
 * host, each qualified by the base host. Path mode: the single base host. Sorted
 * and de-duplicated so the value is comparable; the Python side returns a
 * `frozenset` for the same reason.
 */
export function applicationHosts(config: RoutingConfig): string[] {
  if (config.mode === "path") {
    return [config.base_host];
  }
  const labels = [
    config.surfaces.shell.host,
    config.surfaces.identity.host,
    ...Object.values(config.surfaces.modules).map((module) => module.host),
  ];
  const hosts = new Set(labels.map((label) => `${label}.${config.base_host}`));
  return [...hosts].sort();
}
