import { IDENTITY, SHELL, type RoutingConfig } from "@/lib/routing/config";
import { identityPath, joinPrefix, urlFor } from "@/lib/routing/url-for";

/**
 * Every link, form action and redirect target the shell produces (C9, B10).
 *
 * This file is the only place in `apps/web/src` outside this `routing/` package's
 * own modules that may spell a route, which is what `scripts/check_routing_literals.py`
 * (11's) enforces: every caller elsewhere imports a builder from here rather than
 * writing `/auth/login` into a component. That is not style — a literal is correct
 * in exactly one of the two topologies and silently wrong in the other.
 *
 * The split between `urlFor` and `identityPath` is load-bearing and is not a
 * preference:
 *
 * - `loginHref` and `callbackUrl` **cross hosts** in subdomain mode (the browser
 *   has to arrive at the identity host), so they are absolute `urlFor` URLs.
 * - `switcherAction` and `logoutAction` must **stay on the current host**: they are
 *   POSTs authenticated by the host-only `rheo_session` cookie, and `/auth/*` is
 *   served on every application host in both modes. An absolute URL here would
 *   send a subdomain-mode POST to the identity host, where the shell host's cookie
 *   does not exist — a request that authenticates nothing and refuses
 *   `session_missing`.
 */

/** Where a signed-in account lands: the shell's root, absolute, in both modes. */
export function postLoginReturn(config: RoutingConfig): string {
  return urlFor(config, SHELL, "/");
}

/**
 * The sign-in entry point: `/auth/login` on the identity surface, carrying the
 * shell root as its `return`. The host of that `return` is validated against
 * `application_hosts()` by the route itself, so this builder can never widen it.
 */
export function loginHref(config: RoutingConfig): string {
  const query = new URLSearchParams({ return: postLoginReturn(config) });
  return `${urlFor(config, IDENTITY, "/login")}?${query.toString()}`;
}

/**
 * The OAuth callback URL an operator registers with the identity provider — the
 * same value `rheo routing hosts` prints and `/auth/login` builds server-side.
 */
export function callbackUrl(config: RoutingConfig): string {
  return urlFor(config, IDENTITY, "/callback");
}

/** The workspace switcher's form action: same host, `POST /auth/session/workspace`. */
export function switcherAction(config: RoutingConfig): string {
  return identityPath(config, "/session/workspace");
}

/** The logout form's action: same host, `POST /auth/logout`. */
export function logoutAction(config: RoutingConfig): string {
  return identityPath(config, "/logout");
}

/**
 * The cross-host continue hop the middleware redirects an unauthenticated request
 * to: `/auth/continue` on the **identity** surface, carrying the absolute return
 * target and the nonce bound to this host's `rheo_continue` cookie.
 *
 * A sixth builder beyond the five `spec.md` names, and deliberately so: the
 * middleware's redirect is built in `lib/continue-redirect.ts`, which lives
 * outside this `routing/` package and therefore may not contain the `/continue`
 * literal itself.
 */
export function continueHref(
  config: RoutingConfig,
  options: { returnTarget: string; nonce: string },
): string {
  const query = new URLSearchParams({
    return: options.returnTarget,
    nonce: options.nonce,
  });
  return urlFor(config, IDENTITY, `/continue?${query.toString()}`);
}

/** The module surface run 0v's throwaway spike declares in its own manifest. */
const SPIKE = "spike";

/**
 * The spike page's own path, **relative**, under its module surface (0v).
 *
 * Deleted with the rest of the spike at 0c0's branch cut.
 *
 * Built the way `identityPath` is — `joinPrefix` over the surface's own prefix —
 * and deliberately **not** with `urlFor`. `urlFor` is absolute in both modes, and
 * this run's driver sets a port-free `RHEO__routing__base_host=localhost` (D-37)
 * while `web` listens on 3000, so an absolute spike URL evaluates to
 * `http://localhost/spike`: port 80, where nothing listens. A 303 to that is a
 * dead redirect and the driver's `curl -L` fails to connect. There is no
 * `spikeHref` for the same reason.
 *
 * One difference from `identityPath`, and it is the point of the function rather
 * than an edge case: the `identity` surface is always present in a
 * `RoutingConfig`, while a **module** surface is present only because D-6
 * populated it from a loaded manifest. So an absent `spike` surface **throws**,
 * the way `urlFor` already throws for an unknown surface, and never falls back to
 * an empty prefix. A silent fallback would redirect to `/` — the shell root —
 * while still carrying the `?workspace=…` query the driver asserts on, so the
 * driver would print PASS for a page that never rendered. An absent surface means
 * D-6's wiring is broken, and it should say so.
 */
export function spikePath(config: RoutingConfig, path: string): string {
  const surface = config.surfaces.modules[SPIKE];
  if (surface === undefined) {
    throw new Error(`unknown routing surface '${SPIKE}'`);
  }
  return joinPrefix(surface.path ?? "", path);
}
