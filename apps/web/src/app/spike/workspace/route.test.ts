import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";

/**
 * The workspace handler's response shape (0v, C4).
 *
 * A Next.js route handler is a plain async function, so this calls the exported
 * `POST` directly against a form-encoded `NextRequest` — no Next test harness.
 * `global.fetch` is stubbed because that is what both of this handler's outbound
 * calls resolve to: `loadRoutingConfig`'s read of the internal listener, and the
 * `/auth/session/workspace` forward itself. The stub dispatches on the URL so the
 * real `loadRoutingConfig` runs rather than being mocked away.
 *
 * `vi.resetModules()` per test because `loadRoutingConfig` memoises a successful
 * load in module state; without it the second test would reuse the first's
 * configuration and the `unavailable` case could not be expressed at all.
 *
 * The assertion that matters is the **outcome parameter**. A bare 303 would make
 * a refused switch and a successful one the same response, and `make spike` would
 * print PASS for a refusal — so every case below asserts the state by name, not
 * just the status.
 */

const SURFACE: SurfaceConfig = { host: "circuit" };
const CONFIG: RoutingConfig = {
  mode: "path",
  scheme: "http",
  base_host: "localhost",
  surfaces: {
    shell: { host: "circuit", path: "/" },
    identity: { host: "auth", path: "/auth", fixed_path: true },
    api: SURFACE,
    mcp: SURFACE,
    docs: SURFACE,
    integration: SURFACE,
    modules: { spike: { host: "spike", path: "/spike" } },
  },
};

const INTERNAL_BASE = "http://127.0.0.1:8100";
const PUBLIC_BASE = "http://localhost:8000";
const WEB_ORIGIN = "http://localhost:3000";
const SPIKE_WORKSPACE_URL = `${WEB_ORIGIN}/spike/workspace`;
const SESSION = "abc123";
const WORKSPACE_A = "11111111-1111-4111-8111-111111111111";
const WORKSPACE_B = "22222222-2222-4222-8222-222222222222";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** One `fetch` stub serving both the routing read and the forward. */
function stubFetch(forward: Response | Error) {
  const mock = vi.fn(async (url: string) => {
    if (url.includes("/internal/v1/routing")) {
      return jsonResponse(200, CONFIG);
    }
    if (forward instanceof Error) {
      throw forward;
    }
    return forward;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

async function postWorkspace(
  forward: Response | Error,
  options: { cookie?: string; origin?: string } = {},
) {
  vi.resetModules();
  const fetchMock = stubFetch(forward);
  const { POST } = await import("@/app/spike/workspace/route");

  const headers = new Headers({
    "Content-Type": "application/x-www-form-urlencoded",
  });
  const origin = options.origin ?? WEB_ORIGIN;
  headers.set("origin", origin);
  headers.set("host", "localhost:3000");
  const cookie = options.cookie ?? `rheo_session=${SESSION}`;
  if (cookie !== "") {
    headers.set("cookie", cookie);
  }
  const request = new NextRequest(SPIKE_WORKSPACE_URL, {
    method: "POST",
    headers,
    body: new URLSearchParams({ target_workspace_id: WORKSPACE_A }).toString(),
  });
  return { response: await POST(request), fetchMock };
}

/** The forward call the stub recorded, ignoring the routing read. */
function forwardCall(fetchMock: ReturnType<typeof vi.fn>): [string, RequestInit] {
  const call = fetchMock.mock.calls.find(
    ([url]) => !String(url).includes("/internal/v1/routing"),
  );
  expect(call).toBeDefined();
  return call as [string, RequestInit];
}

beforeEach(() => {
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", INTERNAL_BASE);
  vi.stubEnv("RHEO_INTERNAL_SECRET", "internal-secret");
  vi.stubEnv("RHEO_CORE_PUBLIC_URL", PUBLIC_BASE);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("the spike's workspace handler", () => {
  it("redirects with workspace=selected when the forward succeeds", async () => {
    const { response } = await postWorkspace(
      jsonResponse(200, { state: "ok", active_workspace_id: WORKSPACE_A }),
    );

    expect(response.status).toBe(303);
    const location = response.headers.get("Location");
    expect(location).toBe("/spike/?workspace=selected");
    expect(location).toContain("?workspace=selected");
  });

  it.each([
    ["not_a_member", 403],
    ["origin_not_allowed", 403],
    ["session_missing", 401],
  ])("redirects with the forward's own %s refusal", async (state, status) => {
    const { response } = await postWorkspace(jsonResponse(status, { state }));

    expect(response.status).toBe(303);
    // The whole point of the parameter: a refusal must not look like a success.
    expect(response.headers.get("Location")).toBe(`/spike/?workspace=${state}`);
    expect(response.headers.get("Location")).not.toContain("selected");
  });

  it("redirects with workspace=unavailable when the forward cannot be reached", async () => {
    const { response } = await postWorkspace(new Error("ECONNREFUSED"));

    expect(response.status).toBe(303);
    expect(response.headers.get("Location")).toBe("/spike/?workspace=unavailable");
  });

  it("reports unavailable for a refusal it cannot name", async () => {
    // FastAPI's own 422 when `WorkspaceSwitchInput` refuses a value that is not a
    // UUID — reachable through the page's free-text fallback field. The response
    // contract enumerates no state for it; see the findings note.
    const { response } = await postWorkspace(
      jsonResponse(422, { detail: [{ msg: "not a valid UUID" }] }),
    );

    expect(response.headers.get("Location")).toBe("/spike/?workspace=unavailable");
  });

  it("keeps the Location relative and carries no body", async () => {
    const { response } = await postWorkspace(jsonResponse(200, { state: "ok" }));

    const location = response.headers.get("Location") ?? "";
    expect(location.startsWith("/")).toBe(true);
    expect(location.startsWith("//")).toBe(false);
    expect(location).not.toMatch(/^https?:/);
    expect(await response.text()).toBe("");
  });

  it("forwards to the public listener with the four things the route reads", async () => {
    const { fetchMock } = await postWorkspace(jsonResponse(200, { state: "ok" }));

    const [url, init] = forwardCall(fetchMock);
    expect(url).toBe(`${PUBLIC_BASE}/auth/session/workspace`);
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    // Node's fetch sends no Origin of its own; without it the route answers
    // 403 origin_not_allowed.
    expect(headers.Origin).toBe(WEB_ORIGIN);
    // Node's fetch forwards no cookie jar either.
    expect(headers.Cookie).toBe(`rheo_session=${SESSION}`);
    // A form post cannot produce the JSON body `WorkspaceSwitchInput` needs.
    expect(headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({
      target_workspace_id: WORKSPACE_A,
    });
    // Deliberately not sent: nothing under /auth/* reads X-Rheo-Host, and the
    // Host the route reads is the base URL's own authority.
    expect(headers["X-Rheo-Host"]).toBeUndefined();
    expect(headers.Host).toBeUndefined();
  });

  it("lets the free-text fallback beat a checked radio", async () => {
    // The page's one form carries `target_workspace_id` once per radio and once
    // for the free-text field. `FormData.get` would return the radio's value and
    // the fallback would be dead; the last non-empty value is taken instead.
    vi.resetModules();
    const fetchMock = stubFetch(jsonResponse(200, { state: "ok" }));
    const { POST } = await import("@/app/spike/workspace/route");
    const body = new URLSearchParams();
    body.append("target_workspace_id", WORKSPACE_A);
    body.append("target_workspace_id", WORKSPACE_B);
    const request = new NextRequest(SPIKE_WORKSPACE_URL, {
      method: "POST",
      headers: new Headers({
        "Content-Type": "application/x-www-form-urlencoded",
        origin: WEB_ORIGIN,
        host: "localhost:3000",
        cookie: `rheo_session=${SESSION}`,
      }),
      body: body.toString(),
    });

    await POST(request);

    const [, init] = forwardCall(fetchMock);
    expect(JSON.parse(String(init.body))).toEqual({
      target_workspace_id: WORKSPACE_B,
    });
  });

  it("falls back to the checked radio when the free-text field is blank", async () => {
    vi.resetModules();
    const fetchMock = stubFetch(jsonResponse(200, { state: "ok" }));
    const { POST } = await import("@/app/spike/workspace/route");
    const body = new URLSearchParams();
    body.append("target_workspace_id", WORKSPACE_A);
    body.append("target_workspace_id", "");
    const request = new NextRequest(SPIKE_WORKSPACE_URL, {
      method: "POST",
      headers: new Headers({
        "Content-Type": "application/x-www-form-urlencoded",
        origin: WEB_ORIGIN,
        host: "localhost:3000",
        cookie: `rheo_session=${SESSION}`,
      }),
      body: body.toString(),
    });

    await POST(request);

    const [, init] = forwardCall(fetchMock);
    expect(JSON.parse(String(init.body))).toEqual({
      target_workspace_id: WORKSPACE_A,
    });
  });

  it("forwards no Cookie header when the request carries none", async () => {
    const { fetchMock } = await postWorkspace(
      jsonResponse(401, { state: "session_missing" }),
      { cookie: "" },
    );

    const [, init] = forwardCall(fetchMock);
    expect((init.headers as Record<string, string>).Cookie).toBeUndefined();
  });

  it("redirects with unavailable when no public base URL is configured", async () => {
    vi.stubEnv("RHEO_CORE_PUBLIC_URL", "");

    const { response, fetchMock } = await postWorkspace(
      jsonResponse(200, { state: "ok" }),
    );

    expect(response.headers.get("Location")).toBe("/spike/?workspace=unavailable");
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => !String(url).includes("/internal/v1/routing"),
      ),
    ).toHaveLength(0);
  });
});
