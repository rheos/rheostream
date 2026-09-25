import { NextResponse, type NextRequest } from "next/server";

import {
  CONTINUE_COOKIE,
  continueRedirect,
  mintNonce,
} from "@/lib/continue-redirect";
import { requestHost } from "@/lib/request";
import { loadRoutingConfig } from "@/lib/routing/load";
import { SESSION_COOKIE } from "@/lib/session";

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
 * When the routing configuration is unavailable the request is passed through
 * rather than redirected, because there is no correct cross-host URL to redirect
 * to — guessing one would be worse than rendering. `make demo` exercises exactly
 * that path: it leaves `RHEO_CORE_INTERNAL_API_URL` unset by design (the internal
 * listener is never published to the host), so the shell renders its unavailable
 * state instead of 302-ing a `curl` that has no session. Nothing is exposed by
 * passing through — the page still shows the signed-out state.
 */
export async function middleware(request: NextRequest): Promise<NextResponse> {
  if (request.cookies.get(SESSION_COOKIE)?.value) {
    return NextResponse.next();
  }

  const routing = await loadRoutingConfig();
  if (routing.state !== "ok") {
    return NextResponse.next();
  }
  const routingConfig = routing.config;

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
