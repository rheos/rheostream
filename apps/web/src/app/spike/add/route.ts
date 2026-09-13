import type { NextRequest } from "next/server";

import { spikePath } from "@/lib/routing/links";
import { loadRoutingConfig } from "@/lib/routing/load";
import { SESSION_COOKIE } from "@/lib/session";
import { runOperation } from "@/lib/spike/client";

/**
 * The spike's add form target (0v, C4) — deleted at 0c0's branch cut.
 *
 * Calls `spike.note.add` through the typed client and answers with **the same
 * shape as `workspace/route.ts`**: 303 See Other, relative `Location`, outcome in
 * the query string. The two handlers must not answer differently, and what keeps
 * them identical is that each asserts the shape in its own sibling test — this
 * run's file map gives the spike's web surface no shared module beyond the typed
 * client, so the six-line `seeOther` below is deliberately duplicated rather than
 * hoisted into `lib/spike/client.ts`, whose remit is the generated types.
 *
 * **It returns no JSON body, and that is a decision rather than an omission.** A
 * 303 carries none, and the page posts a plain `<form method="post">` from a
 * server component, so a JSON response would land a real browser on a raw JSON
 * body. The envelope assertion for this exact call already exists one layer down,
 * in `tests/postgres/test_internal_operations.py::test_add_note_through_the_internal_boundary`,
 * which reads the real envelope off the internal route. The browser-facing
 * assertion is the `Location` header.
 *
 * **The outcome parameter is what makes the driver's assertion real.** `state` is
 * the operation envelope's own (`succeeded`, `input_invalid`, `module_disabled`,
 * `role_not_permitted`, …), or `unavailable` when the client never reached the
 * listener. Without it a refused add and a successful one would be the same 303
 * and `make spike` would print PASS for a refusal.
 *
 * Unlike the workspace handler this one never touches the network itself: the
 * client owns the request, its three headers and its fail-closed narrowing.
 */

export const dynamic = "force-dynamic";

/** The outcome parameter the spike page reads (`page.tsx`) and the driver asserts on. */
const OUTCOME = "add";

/** The form field the page's add form posts. */
const BODY_FIELD = "body";

export async function POST(request: NextRequest): Promise<Response> {
  const routing = await loadRoutingConfig();
  if (routing.state !== "ok") {
    // Not a 303, for the reason `workspace/route.ts` states: with no routing
    // configuration there is no spike path to redirect to.
    return new Response("routing configuration unavailable\n", { status: 503 });
  }
  const target = spikePath(routing.config, "/");

  const form = await request.formData();
  const outcome = await runOperation({
    name: "spike.note.add",
    input: { body: String(form.get(BODY_FIELD) ?? "") },
    sessionSecret: request.cookies.get(SESSION_COOKIE)?.value,
    host:
      request.headers.get("x-forwarded-host") ??
      request.headers.get("host") ??
      undefined,
  });
  return seeOther(target, outcome.state);
}

/** A 303 back to the spike page, carrying the outcome. Relative, always. */
function seeOther(target: string, state: string): Response {
  const query = new URLSearchParams({ [OUTCOME]: state });
  return new Response(null, {
    status: 303,
    headers: { Location: `${target}?${query.toString()}` },
  });
}
