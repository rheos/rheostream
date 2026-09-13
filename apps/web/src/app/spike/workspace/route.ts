import type { NextRequest } from "next/server";

import { spikePath, switcherAction } from "@/lib/routing/links";
import { corePublicBaseUrl, loadRoutingConfig } from "@/lib/routing/load";
import { SESSION_COOKIE } from "@/lib/session";

/**
 * The spike's workspace form target (0v, C4) — deleted at 0c0's branch cut.
 *
 * **One handler for both cases**: the first-run selection a session needs before
 * it has any workspace at all, and every later switch. The page renders the same
 * form in both states (D-CF1), so there is one route rather than two that could
 * drift.
 *
 * **Why a server-side forward exists at all.** `POST /auth/session/workspace` is
 * served by `core`'s **public** listener, while the browser's origin is Next.js:
 * `apps/web/src/app` has no `auth` route, `next.config.ts` declares no rewrites,
 * and `deploy/` ships no reverse proxy — so the browser's own relative `/auth/*`
 * post reaches Next and 404s. That is finding F16, and it is why the shipped
 * `WorkspaceSwitcher` component cannot be used here. Only a server-side forward
 * reaches the route.
 *
 * **The four things the target route reads, and where each comes from** — this is
 * D-CF1's table, and getting any one of them wrong produces a plausible-looking
 * refusal rather than an error:
 *
 * - `Origin` — the browser's own. Node's `fetch` sends none by itself, and
 *   `_check_origin` answers 403 `origin_not_allowed` when it is absent. This
 *   works only because undici deliberately does not enforce the Fetch standard's
 *   forbidden-header list, which includes `Origin` (spec.md Technical Risk 7). If
 *   a future Node starts enforcing it, the symptom is a 403 `origin_not_allowed`
 *   at driver step 8 — a runtime-policy change, not a bug here — and the remedy
 *   is one line: build this forward with `undici.request` or `node:http`.
 * - `Host` — **not set explicitly**. It is the base URL's own authority, which is
 *   why `RHEO_CORE_PUBLIC_URL` is spelled `localhost` and not `127.0.0.1`: the
 *   session secret is host-bound, and a secret minted for `localhost` does not
 *   resolve under `127.0.0.1`. `X-Rheo-Host` is deliberately **not** sent either
 *   — nothing under `/auth/*` reads it; it belongs to the internal listener alone.
 * - the `rheo_session` cookie — Node's `fetch` forwards no cookie jar, so it is
 *   copied from the incoming request. Absent, the forward refuses
 *   `session_missing`, which is the honest outcome and is reported as such.
 * - a **JSON** body — the incoming request is form-encoded (a plain
 *   `<form method="post">` in a server component cannot produce JSON), so it is
 *   re-encoded here. The same conversion `workspace-switcher.tsx` does in the
 *   browser, moved to the server.
 *
 * **The response is always a 303 with the outcome in the query string**, never a
 * bare 303 and never an absolute `urlFor` URL — see `spikePath`. A bare 303 would
 * make a refused switch and a successful one the same response, and `make spike`
 * would print PASS for a refusal.
 *
 * No `http://` literal appears in this file: the base URL comes from
 * `corePublicBaseUrl()` and the path from `switcherAction()`, both inside the
 * `routing/` package that `scripts/check_routing_literals.py` allowlists.
 */

export const dynamic = "force-dynamic";

/** The outcome parameter the spike page reads (`page.tsx`) and the driver asserts on. */
const OUTCOME = "workspace";

/** This handler's own word for a 200 from the forward. */
const SELECTED = "selected";

/**
 * The forward never reached the route: no base URL configured, or the request
 * itself failed. Also the fallback for a response this handler cannot name — see
 * `outcomeFor`.
 */
const UNAVAILABLE = "unavailable";

const TARGET_FIELD = "target_workspace_id";

/**
 * The outcome state for one forward response.
 *
 * A 200 is `selected` — deliberately this handler's own word rather than the
 * body's `"ok"`, because the page's query parameter is about the switch, not
 * about the route's own envelope. Anything else reports **that response's own
 * refusal state** (`not_a_member`, `origin_not_allowed`, `session_missing`), so a
 * refusal is never indistinguishable from a success.
 */
async function outcomeFor(response: Response): Promise<string> {
  if (response.ok) {
    return SELECTED;
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return UNAVAILABLE;
  }
  if (
    typeof body === "object" &&
    body !== null &&
    !Array.isArray(body) &&
    typeof (body as Record<string, unknown>).state === "string"
  ) {
    return (body as Record<string, unknown>).state as string;
  }
  // A non-200 whose body is not `{"state": ...}` — in practice FastAPI's own
  // `{"detail": [...]}` 422 when `WorkspaceSwitchInput` refuses a value that is
  // not a UUID, which the page's free-text fallback field makes reachable. The
  // response contract enumerates only the three named refusals and
  // `unavailable`, so `unavailable` is what this reports; that the enumeration
  // has no word for "reached the route and it refused the input" is recorded as
  // a finding rather than patched over with an invented state.
  return UNAVAILABLE;
}

/**
 * The workspace the form actually chose.
 *
 * The page's one form carries the field twice: a radio per option, and the
 * free-text fallback after them (D-CF1). `FormData.get` returns the **first**
 * entry, so a checked radio would always beat a typed id and the fallback could
 * never be used at all in the signed-in case — a field that looks like it works
 * and silently does not. Last non-empty value wins instead: the text input comes
 * after the radios in the form, so typing an id means it, and leaving it blank
 * falls through to the radio.
 */
function chosenWorkspace(form: FormData): string {
  const values = form
    .getAll(TARGET_FIELD)
    .map((value) => String(value).trim())
    .filter((value) => value !== "");
  return values.length === 0 ? "" : values[values.length - 1];
}

/** The browser's own scheme and authority, for the forward's `Origin`. */
function originOf(request: NextRequest): string {
  return request.headers.get("origin") ?? new URL(request.url).origin;
}

export async function POST(request: NextRequest): Promise<Response> {
  const routing = await loadRoutingConfig();
  if (routing.state !== "ok") {
    // Not a 303: with no routing configuration this handler cannot name the page
    // it would redirect to. The spike surface's path is only knowable from the
    // config D-6 populates, and inventing one would be the silent degradation
    // `spikePath` exists to refuse.
    return new Response("routing configuration unavailable\n", { status: 503 });
  }
  const config = routing.config;
  const target = spikePath(config, "/");

  const form = await request.formData();
  const targetWorkspaceId = chosenWorkspace(form);
  const baseUrl = corePublicBaseUrl();
  if (baseUrl === null) {
    return seeOther(target, UNAVAILABLE);
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Origin: originOf(request),
  };
  const session = request.cookies.get(SESSION_COOKIE)?.value;
  if (session !== undefined) {
    headers.Cookie = `${SESSION_COOKIE}=${session}`;
  }

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${switcherAction(config)}`, {
      method: "POST",
      cache: "no-store",
      redirect: "manual",
      headers,
      body: JSON.stringify({ [TARGET_FIELD]: targetWorkspaceId }),
    });
  } catch {
    return seeOther(target, UNAVAILABLE);
  }
  return seeOther(target, await outcomeFor(response));
}

/** A 303 back to the spike page, carrying the outcome. Relative, always. */
function seeOther(target: string, state: string): Response {
  const query = new URLSearchParams({ [OUTCOME]: state });
  return new Response(null, {
    status: 303,
    headers: { Location: `${target}?${query.toString()}` },
  });
}
