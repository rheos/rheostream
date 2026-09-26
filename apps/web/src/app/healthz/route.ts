/**
 * The web tier's liveness probe (#156): `GET /healthz` answers 200 with a fixed
 * body, so the flagship compose health check can tell a serving `next start` from
 * a stuck one.
 *
 * Liveness only, on purpose. It calls nothing: not `core`, not the routing
 * configuration, not the session. A web container whose `core` is down is still
 * alive, and the home page already renders that state for a person to see. The
 * middleware matcher excludes this path, because otherwise a request with no
 * session cookie is redirected to the identity host and never reaches here.
 */
export function GET(): Response {
  return Response.json(
    { status: "ok" },
    { headers: { "cache-control": "no-store" } },
  );
}
