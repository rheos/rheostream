import type { RoutingConfig } from "@/lib/routing/config";

/**
 * Which surface a request is for: the shell, one module surface (and the path
 * inside it), or nothing this application serves.
 *
 * It lives in the `routing/` package because it is path and host logic, and
 * `scripts/check_routing_literals.py` exempts this package and nothing else in
 * `apps/web/src`.
 *
 * - **Path mode.** The host plays no part. A module surface matches when the
 *   pathname is its `path` or continues it at a segment boundary, so
 *   `/recallatron-extra` never matches the surface path `/recallatron`. The shell
 *   serves its own root and nothing below it.
 * - **Subdomain mode.** The port-stripped host decides: `<surface.host>.<base_host>`
 *   is that module, and the whole pathname is the path inside it, so the module's
 *   `/` route arrives at `app/page.tsx` like the shell's root does. The shell host
 *   serves its root. Any other host is not served here.
 *
 * `base_host`, not `public_host`, is the comparison: `base_host` is the port-free
 * Host-header match (issue #37), `public_host` is only a link authority.
 */

export type ResolvedRequest =
  | { kind: "shell" }
  | { kind: "module"; surface: string; subPath: string }
  | { kind: "not-found" };

const ROOT = "/";

/** Drop a trailing slash (never the root's own), so `/search/` and `/search` agree. */
export function normalizePath(path: string): string {
  if (path === "" || path === ROOT) {
    return ROOT;
  }
  const withLead = path.startsWith(ROOT) ? path : `${ROOT}${path}`;
  return withLead.endsWith(ROOT) ? withLead.slice(0, -1) : withLead;
}

/** The host a request named, without its port and in lower case. */
function hostName(host: string): string {
  const lower = host.trim().toLowerCase();
  // A bracketed IPv6 literal keeps its colons; only a trailing `:port` goes.
  if (lower.startsWith("[")) {
    const end = lower.indexOf("]");
    return end === -1 ? lower : lower.slice(0, end + 1);
  }
  const colon = lower.indexOf(":");
  return colon === -1 ? lower : lower.slice(0, colon);
}

function resolvePath(config: RoutingConfig, pathname: string): ResolvedRequest {
  const path = normalizePath(pathname);
  for (const [surface, spec] of Object.entries(config.surfaces.modules)) {
    if (spec.path === undefined) {
      continue;
    }
    const prefix = normalizePath(spec.path);
    if (prefix === ROOT) {
      // A module mounted at the root would shadow the shell; it is never matched.
      continue;
    }
    if (path === prefix) {
      return { kind: "module", surface, subPath: ROOT };
    }
    if (path.startsWith(`${prefix}${ROOT}`)) {
      return { kind: "module", surface, subPath: path.slice(prefix.length) };
    }
  }
  const shellRoot = normalizePath(config.surfaces.shell.path ?? ROOT);
  return path === shellRoot ? { kind: "shell" } : { kind: "not-found" };
}

function resolveHost(
  config: RoutingConfig,
  host: string,
  pathname: string,
): ResolvedRequest {
  const name = hostName(host);
  const base = config.base_host.toLowerCase();
  for (const [surface, spec] of Object.entries(config.surfaces.modules)) {
    if (name === `${spec.host.toLowerCase()}.${base}`) {
      return { kind: "module", surface, subPath: normalizePath(pathname) };
    }
  }
  if (name === `${config.surfaces.shell.host.toLowerCase()}.${base}`) {
    return normalizePath(pathname) === ROOT ? { kind: "shell" } : { kind: "not-found" };
  }
  return { kind: "not-found" };
}

export function resolveRequest(
  config: RoutingConfig,
  host: string,
  pathname: string,
): ResolvedRequest {
  return config.mode === "path"
    ? resolvePath(config, pathname)
    : resolveHost(config, host, pathname);
}
