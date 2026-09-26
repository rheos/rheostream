import { NextResponse, type NextRequest } from "next/server";

import {
  CONTINUE_COOKIE,
  continueRedirect,
  mintNonce,
} from "@/lib/continue-redirect";
import { requestHost } from "@/lib/request";
import type { RoutingConfig } from "@/lib/routing/config";
import { loadRoutingConfig } from "@/lib/routing/load";
import { identityPath } from "@/lib/routing/url-for";
import { SESSION_COOKIE } from "@/lib/session";

/**
 * Whether `pathname` is the identity path itself or anything under it. The prefix
 * is operator-configurable, so it is derived from the routing configuration, never
 * written here. A plain prefix match would also catch a sibling such as `/authors`.
 *
 * A root prefix (`routing.identity.path` of `"/"` or `""`, which the settings
 * registry does not refuse and `joinPrefix` treats as contributing nothing) leaves
 * no prefix to tell identity routes from application routes, so nothing matches:
 * 404-ing every path would take the whole application down.
 */
function isIdentityPath(config: RoutingConfig, pathname: string): boolean {
  const bare = identityPath(config, "/").replace(/\/$/, "");
  if (bare === "") {
    return false;
  }
  return pathname === bare || pathname.startsWith(`${bare}/`);
}

/**
 * Send an unauthenticated request to the identity host, carrying the nonce that
 * binds the grant it will come back with (C9, B18).
 *
 * **This file declares no `runtime` export, on purpose.** Next.js middleware
 * already runs on the edge by default with no declaration at all, and
 * `scripts/check_web_platform.py` — a required CI step — string-matches an
 * explicit edge-runtime declaration in every `.ts`/`.tsx` under `apps/web`. Adding
 * the "correct", explicit line would fail the criterion-23 gate.
 *
 * **This is a redirect, not an authorisation boundary**, and the distinction is
 * deliberate. It looks only for the *presence* of the host-only `rheo_session`
 * cookie, because deciding whether a session is genuinely valid is `core`'s job
 * and costs a round trip on every navigation. Nothing downstream trusts this
 * decision: `decideSurface` (`shell/render-surface.tsx`, behind both route files)
 * reads `/internal/v1/session` itself and fails closed, so a stale or forged cookie
 * gets past this function and is then shown the signed-out panel, with no module
 * screen. The convenience of not redirecting a request that merely *looks* signed in
 * is all this buys.
 *
 * **The order of checks.** Routing loads first (`loadRoutingConfig` memoises a
 * successful load, so a signed-in navigation does not pay a fetch each time). Then
 * any request at or under the identity path gets a plain 404 with no `Location`
 * and no cookie, session or not: that path is `core`'s, so reaching the web tier
 * means the proxy is misrouted, and redirecting would loop through `/continue`
 * forever (#119). Then a request carrying the session cookie passes through, and
 * everything else is redirected.
 *
 * When the routing configuration is unavailable the request is passed through
 * rather than redirected, because there is no correct cross-host URL to redirect
 * to — guessing one would be worse than rendering. `make demo` exercises exactly
 * that path: it leaves `RHEO_CORE_INTERNAL_API_URL` unset by design (the internal
 * listener is never published to the host), so the shell renders its unavailable
 * state instead of 302-ing a `curl` that has no session. Nothing is exposed by
 * passing through — the page still shows the signed-out state.
 */
export async function middleware(request: NextRequest): Promise<NextResponse> {
  const routing = await loadRoutingConfig();
  if (routing.state !== "ok") {
    return NextResponse.next();
  }
  const routingConfig = routing.config;

  // The identity path belongs to `core`. Reaching it here means the proxy is
  // misrouted, and redirecting would loop (the redirect target is itself on the
  // identity path), so answer a plain 404 whether or not a session cookie is present.
  if (isIdentityPath(routingConfig, request.nextUrl.pathname)) {
    return new NextResponse(null, { status: 404 });
  }

  if (request.cookies.get(SESSION_COOKIE)?.value) {
    return NextResponse.next();
  }

  // The host the browser actually asked for (`lib/request.ts`, the rule the server
  // components share). Never a configured name: the return target has to bring the
  // browser back where it started.
  const host = requestHost(request.headers);
  if (!host) {
    return NextResponse.next();
  }

  const nonce = mintNonce();
  const target = continueRedirect(routingConfig, {
    host,
    path: `${request.nextUrl.pathname}${request.nextUrl.search}`,
    nonce,
  });

  const response = NextResponse.redirect(target);
  response.cookies.set({
    name: CONTINUE_COOKIE,
    value: nonce,
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    // Five minutes, the lifetime `sessions/cookies.py` gives it server-side.
    maxAge: 300,
    // The same rule `sessions/cookies.py` applies server-side: Secure unless the
    // scheme is plain http *and* this is not a production deployment.
    secure:
      routingConfig.scheme !== "http" ||
      process.env.RHEO_PROFILE === "production",
    // No `domain`, ever. A cookie scoped to the parent domain would be readable
    // on every subdomain, which is the whole reason the session is host-only.
  });
  return response;
}

export const config = {
  // Deny by default: every request is protected except Next's own build output.
  // `login` is excluded because the sign-in page is the one place an unsigned-in
  // browser is meant to land — without the exclusion it would bounce straight
  // through `/auth/continue` to the provider and never render.
  matcher: ["/((?!login(?:/|$)|_next/static|_next/image|favicon.ico).*)"],
};
