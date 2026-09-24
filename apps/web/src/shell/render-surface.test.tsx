import { readFileSync } from "node:fs";
import path from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CoreHealthResult } from "@/lib/core-health";
import type { RoutingConfig } from "@/lib/routing/config";
import type { RoutingConfigResult } from "@/lib/routing/load";
import { urlFor } from "@/lib/routing/url-for";
import type { SessionResult } from "@/lib/session";

/**
 * `renderSurface` end to end through the real composition: the committed
 * `MODULES`, the real operation client (over a stubbed `fetch`), and the two
 * `core.workspace.status` fixtures. `ShellFrame` is rendered both ways, enabled
 * and absent, and a module path is served or 404s accordingly, in whichever
 * routing mode this run of the suite is in.
 */

const REPO = path.resolve(import.meta.dirname, "../../../..");
const MODE = process.env.RHEO_ROUTING_MODE === "subdomain" ? "subdomain" : "path";

function readJson(relative: string): unknown {
  return JSON.parse(readFileSync(path.join(REPO, relative), "utf8"));
}

const BASE = readJson(`tests/fixtures/routing/${MODE}-mode.json`) as RoutingConfig;
/** The routing config a deployment that loaded Recallatron serves. */
const ROUTED: RoutingConfig = {
  ...BASE,
  surfaces: {
    ...BASE.surfaces,
    modules: { recallatron: { host: "recallatron", path: "/recallatron" } },
  },
};
const STATUS = {
  enabled: readJson("tests/fixtures/web/workspace-status-enabled.json"),
  absent: readJson("tests/fixtures/web/workspace-status-absent.json"),
};

const seams = vi.hoisted(() => ({
  routing: { state: "unavailable" } as RoutingConfigResult,
  session: { state: "unavailable" } as SessionResult,
  status: null as unknown,
  calls: [] as string[],
}));

vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => ({ value: "ab12" }) }),
  headers: async () => new Headers({ host: "circuit.example.test" }),
}));

vi.mock("@/lib/core-health", () => ({
  fetchCoreHealth: async (): Promise<CoreHealthResult> => ({
    status: "ok",
    contractVersion: 1,
  }),
}));

vi.mock("@/lib/routing/load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/routing/load")>()),
  loadRoutingConfig: async () => seams.routing,
}));

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  fetchSession: async () => seams.session,
}));

const { renderSurface } = await import("./render-surface");

const SIGNED_IN: SessionResult = {
  state: "ok",
  actor: { kind: "account", id: "acct-synthetic" },
  activeWorkspaceId: "ws-alpha",
  role: "member",
  memberships: [{ workspace_id: "ws-alpha", role: "member" }],
};

/** The request for `subPath` on the recallatron surface, in this mode. */
function moduleRequest(subPath: string, query: Record<string, string> = {}) {
  return MODE === "path"
    ? { host: "example.test", pathname: `/recallatron${subPath}`, query }
    : { host: "recallatron.example.test", pathname: subPath, query };
}

const SHELL_REQUEST = {
  host: MODE === "path" ? "example.test" : "circuit.example.test",
  pathname: "/",
  query: {},
};

async function render(request: Parameters<typeof renderSurface>[0]): Promise<string> {
  return renderToStaticMarkup(await renderSurface(request));
}

function answer(body: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(body), { status: statusCode });
}

beforeEach(() => {
  seams.routing = { state: "ok", config: ROUTED };
  seams.session = SIGNED_IN;
  seams.status = STATUS.enabled;
  seams.calls.length = 0;
  vi.stubEnv("RHEO_CORE_INTERNAL_URL", "http://core.example.test");
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", "http://core-internal.example.test");
  vi.stubEnv("RHEO_INTERNAL_SECRET", "synthetic-internal-secret");
  vi.stubGlobal("fetch", async (url: string) => {
    const name = decodeURIComponent(url.split("/").pop() ?? "");
    seams.calls.push(name);
    if (name === "core.workspace.status") {
      return answer({ state: "succeeded", operation_id: null, result: seams.status });
    }
    // Every Recallatron read answers "unavailable" (a non-envelope 500), so a
    // screen renders its own error state; what matters here is that it rendered.
    return new Response("upstream failure", { status: 500 });
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const NAV = /<nav[^>]*aria-label="Workspace"/;

describe("ShellFrame through the real composition", () => {
  it("offers Recallatron's navigation on the home page when it is enabled", async () => {
    const html = await render(SHELL_REQUEST);
    expect(html).toMatch(NAV);
    const memory = urlFor(ROUTED, "recallatron", "/");
    expect(html).toContain(`href="${memory}">Memory</a>`);
    expect(html).toContain(`href="${urlFor(ROUTED, "recallatron", "/search")}">Search memory</a>`);
    expect(html).not.toContain('aria-current="page"');
    expect(html).toContain("core: ok (contract v1)");
  });

  it("offers no navigation at all when it was never installed", async () => {
    seams.status = STATUS.absent;
    const html = await render(SHELL_REQUEST);
    expect(html).not.toMatch(NAV);
    expect(html).not.toContain("Memory");
    expect(html).toContain("workspace: ws-alpha (member)");
  });

  it("offers no navigation when enabled but the deployment does not route it", async () => {
    seams.routing = { state: "ok", config: BASE };
    const html = await render(SHELL_REQUEST);
    expect(html).not.toMatch(NAV);
  });

  it("says modules are unavailable when the status read fails, and still renders", async () => {
    seams.status = { unexpected: true };
    const html = await render(SHELL_REQUEST);
    expect(html).not.toMatch(NAV);
    expect(html).toContain("modules: unavailable");
  });
});

describe("module screens through the real composition", () => {
  it("renders the matched screen in the frame, with its entry marked current", async () => {
    const html = await render(moduleRequest("/search"));
    expect(html).toMatch(NAV);
    expect(html).toMatch(/<a[^>]*aria-current="page"[^>]*>Search memory<\/a>/);
    expect(html).toContain('type="search"');
  });

  it("serves the module's root route", async () => {
    const html = await render(moduleRequest("/"));
    expect(html).toMatch(/<a[^>]*aria-current="page"[^>]*>Memory<\/a>/);
  });

  it("reaches core through the operation client for the screen's reads", async () => {
    await render(moduleRequest("/"));
    expect(seams.calls).toContain("core.workspace.status");
    expect(seams.calls).toContain("recallatron.entity.list");
  });

  it.each([
    ["never installed", () => (seams.status = STATUS.absent)],
    [
      "enabled but not routed",
      () => (seams.routing = { state: "ok", config: BASE }),
    ],
  ])("404s a module path when it is %s", async (_label, arrange) => {
    arrange();
    await expect(renderSurface(moduleRequest("/search"))).rejects.toMatchObject({
      digest: expect.stringContaining("404"),
    });
  });

  it("404s an unknown route inside an enabled module", async () => {
    await expect(renderSurface(moduleRequest("/nope"))).rejects.toMatchObject({
      digest: expect.stringContaining("404"),
    });
  });

  it("shows no screen to a signed-out session", async () => {
    seams.session = { state: "unauthenticated", refusal: "session_expired" };
    const html = await render(moduleRequest("/search"));
    expect(html).toContain("session: signed out (session_expired)");
    expect(html).not.toContain('type="search"');
    expect(html).not.toMatch(NAV);
  });
});
