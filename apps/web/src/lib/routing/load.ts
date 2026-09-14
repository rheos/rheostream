import { isRoutingConfig, type RoutingConfig } from "@/lib/routing/config";

/**
 * The internal-API client for the routing configuration (C9).
 *
 * Mirrors `core-health.ts:41`'s `fetchCoreHealth` exactly: one `try`, a narrowing
 * guard, and a single "unavailable" sentinel for every failure — a rejected fetch,
 * a non-200, an unparseable or mis-shaped body, **and an unset environment
 * variable**. It never throws, so no page has to catch.
 *
 * Two environment variables, both required, neither interchangeable:
 *
 * - `RHEO_CORE_INTERNAL_API_URL` — the internal listener (port 8100,
 *   container-network only). **Not `RHEO_CORE_INTERNAL_URL`**, which is 0a's
 *   variable for the *public* listener on 8000 and stays pointed there for
 *   `page.tsx`'s `/healthz` read. The two resolve to the same place on a
 *   developer's laptop and to different ones inside the container network, which
 *   is exactly the kind of confusion a green local suite would not catch.
 * - `RHEO_INTERNAL_SECRET` — the shared secret every internal request presents in
 *   `X-Rheo-Internal`; the listener refuses before either endpoint runs without it.
 *
 * Fetched once per process (`spec.md` § Web) and memoised on success only: the
 * routing table is deployment configuration, and the middleware would otherwise
 * make an extra round trip on every navigation. Failures are never memoised, so a
 * shell that started before `core` recovers on its own. `cache: "no-store"` keeps
 * Next's own fetch cache out of that one request.
 */

/**
 * The internal listener's two endpoints.
 *
 * They live here — rather than each beside its caller — so that no file under
 * `apps/web/src` outside this `routing/` package contains a route literal at all,
 * which lets `scripts/check_routing_literals.py` (11's) scan for one without an
 * allowlist. Note what they are not: neither is a routing-table surface. They are
 * served by the internal listener, never reachable from a browser, and identical
 * in both topologies, so they are constants rather than `urlFor` calls.
 */
export const INTERNAL_ROUTING_PATH = "/internal/v1/routing";
export const INTERNAL_SESSION_PATH = "/internal/v1/session";

/**
 * The internal listener's operations route, as a prefix (0v).
 *
 * A prefix rather than a whole path because the route is
 * `POST /internal/v1/operations/{name}` and the operation name is the caller's:
 * a client appends one encoded segment and spells no route itself.
 * The third internal endpoint, and the same kind of constant as the two above —
 * served by the internal listener, never reachable from a browser, identical in
 * both topologies, so a constant rather than an `urlFor` call.
 *
 * Note what it is **not**: the `/api/v1/operations/<name>` key the generated
 * document (and therefore `generated/api-types.ts`) is indexed by. Those keys
 * describe the operation contract both listeners share, under the bearer `api`
 * surface's own prefix; this is the path the web tier actually posts to. The
 * asymmetry is deliberate and is run 0v's finding F20.
 */
export const INTERNAL_OPERATIONS_PATH = "/internal/v1/operations";

/**
 * The generated document's own path-key template (0v).
 *
 * `rheo openapi` emits one concrete path per registered operation, keyed by the
 * bearer `api` surface's prefix — so `core.workspace.status` is the literal key
 * `/api/v1/operations/core.workspace.status` — and `openapi-typescript` turns
 * those keys into `paths` in `src/generated/api-types.ts`. A typed client indexes
 * them.
 *
 * It is a **type**, not a value, and it is deliberately here rather than beside
 * the client that uses it. `scripts/check_routing_literals.py` matches `/api/` in
 * any `.ts` under `apps/web/src` outside this package and the generated
 * directory, and a type-level template literal is a route literal as far as that
 * scan is concerned — rightly so, since it is the same string. Keeping it here is
 * the file's own stated discipline: no file outside `routing/` spells a route,
 * which is what lets that scan run without a widening allowlist.
 *
 * Note again what it is not: `INTERNAL_OPERATIONS_PATH` above is where requests
 * actually go. These keys describe the operation contract both listeners share
 * (finding F20).
 */
export type OperationPathKey<Name extends string> = `/api/v1/operations/${Name}`;

/** The shared-secret header every internal-listener request carries. */
export const INTERNAL_SECRET_HEADER = "X-Rheo-Internal";

export type RoutingConfigResult =
  | { state: "ok"; config: RoutingConfig }
  | { state: "unavailable" };

/** The internal listener's base URL and shared secret, or `null` if either is unset. */
export function internalApiCredentials(): { baseUrl: string; secret: string } | null {
  const baseUrl = process.env.RHEO_CORE_INTERNAL_API_URL;
  const secret = process.env.RHEO_INTERNAL_SECRET;
  if (!baseUrl || !secret) {
    return null;
  }
  return { baseUrl, secret };
}

/**
 * `core`'s **public** listener base URL (`RHEO_CORE_PUBLIC_URL`), or `null` when
 * unset — the same null-on-unset shape as `internalApiCredentials()` above (0v).
 *
 * A third base URL, and none of the three is interchangeable with another:
 * `RHEO_CORE_INTERNAL_API_URL` is the internal listener (8100),
 * `RHEO_CORE_INTERNAL_URL` is 0a's name for the public listener's `/healthz`, and
 * this is the public listener (8000) as a **forward target** for `/auth/*`. A
 * route handler that forwards is its only kind of caller: `/auth/*` is served by
 * `core`'s public listener while the browser's origin is Next.js, so a relative
 * `/auth/*` post from the browser reaches Next and 404s (finding F16) and only a
 * server-side forward can get there.
 *
 * Its authority is load-bearing rather than cosmetic. The forwarded request's
 * `Host` header is whatever this URL says, and `/auth/session/workspace` binds the
 * session secret to that host — so `http://localhost:8000` resolves a secret minted
 * for `localhost` and `http://127.0.0.1:8000` would refuse `session_missing` for
 * the very same session. It lives here, beside the other two, because this package
 * is the only place in `apps/web/src` that may spell a route or a route base at
 * all (`scripts/check_routing_literals.py`).
 */
export function corePublicBaseUrl(): string | null {
  return process.env.RHEO_CORE_PUBLIC_URL || null;
}

let cached: RoutingConfig | null = null;

export async function loadRoutingConfig(): Promise<RoutingConfigResult> {
  if (cached !== null) {
    return { state: "ok", config: cached };
  }
  const credentials = internalApiCredentials();
  if (credentials === null) {
    return { state: "unavailable" };
  }
  try {
    const response = await fetch(
      `${credentials.baseUrl}${INTERNAL_ROUTING_PATH}`,
      {
        cache: "no-store",
        headers: { [INTERNAL_SECRET_HEADER]: credentials.secret },
      },
    );
    if (!response.ok) {
      return { state: "unavailable" };
    }
    const body: unknown = await response.json();
    if (!isRoutingConfig(body)) {
      return { state: "unavailable" };
    }
    cached = body;
    return { state: "ok", config: body };
  } catch {
    return { state: "unavailable" };
  }
}
