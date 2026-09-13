import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";

/**
 * The add handler's response shape (0v, C4).
 *
 * Same method as the workspace handler's sibling test — call the exported `POST`
 * directly against a form-encoded `NextRequest` — with one difference: this
 * handler never touches the network itself, so `@/lib/spike/client` is mocked
 * rather than `fetch`. `fetch` is still stubbed, because `loadRoutingConfig` uses
 * it and the spike path comes from the configuration it returns.
 *
 * Two mutants live here. **JSON instead of a 303**: a handler that answered with
 * the envelope would land a real browser, which posts a plain
 * `<form method="post">`, on a raw JSON body — so every case asserts the status
 * and an empty body, not just the state. **A dropped outcome parameter**: without
 * it a refused add and a successful one are the same 303 and `make spike` prints
 * PASS for a refusal, so every case asserts the state by name.
 */

const runOperation = vi.hoisted(() => vi.fn());
vi.mock("@/lib/spike/client", () => ({ runOperation }));

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
const SPIKE_ADD_URL = "http://localhost:3000/spike/add";
const SESSION = "abc123";

async function postAdd(body = "a note") {
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(CONFIG), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
  const { POST } = await import("@/app/spike/add/route");
  const request = new NextRequest(SPIKE_ADD_URL, {
    method: "POST",
    headers: new Headers({
      "Content-Type": "application/x-www-form-urlencoded",
      host: "localhost:3000",
      cookie: `rheo_session=${SESSION}`,
    }),
    body: new URLSearchParams({ body }).toString(),
  });
  return POST(request);
}

beforeEach(() => {
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", INTERNAL_BASE);
  vi.stubEnv("RHEO_INTERNAL_SECRET", "internal-secret");
  runOperation.mockReset();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("the spike's add handler", () => {
  it("redirects with add=succeeded on a successful dispatch", async () => {
    runOperation.mockResolvedValue({
      state: "succeeded",
      result: { ref: "spike:note:1", body: "a note", created_at: "", revision: 1 },
    });

    const response = await postAdd();

    expect(response.status).toBe(303);
    expect(response.headers.get("Location")).toBe("/spike/?add=succeeded");
  });

  it.each(["input_invalid", "module_disabled", "role_not_permitted", "failed"])(
    "redirects with the envelope's own %s state",
    async (state) => {
      runOperation.mockResolvedValue({ state, error: null });

      const response = await postAdd("");

      expect(response.status).toBe(303);
      expect(response.headers.get("Location")).toBe(`/spike/?add=${state}`);
      expect(response.headers.get("Location")).not.toContain("succeeded");
    },
  );

  it("redirects with add=unavailable when the client never reached the listener", async () => {
    runOperation.mockResolvedValue({ state: "unavailable" });

    const response = await postAdd();

    expect(response.status).toBe(303);
    expect(response.headers.get("Location")).toBe("/spike/?add=unavailable");
  });

  it("never answers with a JSON body", async () => {
    runOperation.mockResolvedValue({
      state: "succeeded",
      result: { ref: "spike:note:1", body: "a note", created_at: "", revision: 1 },
    });

    const response = await postAdd();

    expect(response.status).toBe(303);
    expect(await response.text()).toBe("");
    expect(response.headers.get("Content-Type") ?? "").not.toContain("json");
  });

  it("keeps the Location relative", async () => {
    runOperation.mockResolvedValue({ state: "succeeded", result: {} });

    const location = (await postAdd()).headers.get("Location") ?? "";

    expect(location.startsWith("/")).toBe(true);
    expect(location.startsWith("//")).toBe(false);
    expect(location).not.toMatch(/^https?:/);
  });

  it("passes the form's body, the session cookie and the host to the client", async () => {
    runOperation.mockResolvedValue({ state: "succeeded", result: {} });

    await postAdd("hello");

    expect(runOperation).toHaveBeenCalledTimes(1);
    expect(runOperation).toHaveBeenCalledWith({
      name: "spike.note.add",
      input: { body: "hello" },
      sessionSecret: SESSION,
      host: "localhost:3000",
    });
  });
});
