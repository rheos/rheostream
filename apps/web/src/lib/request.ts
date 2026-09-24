/**
 * The host the browser asked for: `x-forwarded-host` first (a proxy rewrites `host`
 * to its upstream), then `host`. Never a configured name, because both callers need
 * the host this request actually arrived on: the middleware to send the browser back
 * where it started, and the operation client and route files because the session is
 * bound to that host.
 *
 * Pure and runtime-neutral, so the edge middleware and the server components import
 * the same rule.
 */
export function requestHost(headerList: Pick<Headers, "get">): string | undefined {
  return headerList.get("x-forwarded-host") ?? headerList.get("host") ?? undefined;
}
