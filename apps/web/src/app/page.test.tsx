import { readFileSync } from "node:fs";
import path from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CoreHealthResult } from "@/lib/core-health";
import type { RoutingConfig } from "@/lib/routing/config";
import { loginHref, logoutAction, switcherAction } from "@/lib/routing/links";
import type { RoutingConfigResult } from "@/lib/routing/load";
import type { SessionResult } from "@/lib/session";

/**
 * Behavior regression tests for the shell home page after its restyle onto
 * `ShellFrame`: the three seams (core health, routing, session) still fail
 * independently, the guard order is still routing first and session second, and
 * every link and form action still comes from the routing builders.
 *
 * The routing configuration is the shared fixture for the mode under test, so the
 * two CI runs (`RHEO_ROUTING_MODE=path`, `=subdomain`) check the links in both
 * topologies.
 */

const FIXTURES = path.resolve(import.meta.dirname, "../../../../tests/fixtures/routing");
const MODE = process.env.RHEO_ROUTING_MODE === "subdomain" ? "subdomain" : "path";
const CONFIG = JSON.parse(
  readFileSync(path.join(FIXTURES, `${MODE}-mode.json`), "utf8"),
) as RoutingConfig;

const seams = vi.hoisted(() => ({
  health: { status: "unavailable" } as CoreHealthResult,
  routing: { state: "unavailable" } as RoutingConfigResult,
  session: { state: "unavailable" } as SessionResult,
  cookie: "synthetic-session-secret" as string | undefined,
  fetchSessionCalls: [] as Array<{ sessionSecret: string | undefined; host: string | undefined }>,
}));

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) =>
      name === "rheo_session" && seams.cookie !== undefined ? { value: seams.cookie } : undefined,
  }),
  headers: async () => new Headers({ host: "circuit.example.test" }),
}));

vi.mock("@/lib/core-health", () => ({
  fetchCoreHealth: async () => seams.health,
}));

vi.mock("@/lib/routing/load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/routing/load")>()),
  loadRoutingConfig: async () => seams.routing,
}));

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  fetchSession: async (options: { sessionSecret: string | undefined; host: string | undefined }) => {
    seams.fetchSessionCalls.push(options);
    return seams.session;
  },
}));

// The real switcher posts its action with `fetch`, so the action never reaches
// the markup. This stand-in puts the props the page passed on the element.
vi.mock("@/components/workspace-switcher", async () => {
  const { createElement } = await import("react");
  return {
    WorkspaceSwitcher: (props: { action: string; activeWorkspaceId: string }) =>
      createElement("form", {
        "data-switcher-action": props.action,
        "data-active-workspace": props.activeWorkspaceId,
      }),
  };
});

const { default: Page, dynamic } = await import("./page");

const SIGNED_IN: SessionResult = {
  state: "ok",
  actor: { kind: "account", id: "acct-synthetic" },
  activeWorkspaceId: "ws-alpha",
  role: "owner",
  memberships: [{ workspace_id: "ws-alpha", role: "owner" }],
};

async function render(): Promise<string> {
  return renderToStaticMarkup(await Page());
}

beforeEach(() => {
  seams.health = { status: "ok", contractVersion: 3 };
  seams.routing = { state: "ok", config: CONFIG };
  seams.session = SIGNED_IN;
  seams.cookie = "synthetic-session-secret";
  seams.fetchSessionCalls.length = 0;
  vi.stubEnv("RHEO_CORE_INTERNAL_URL", "http://core.internal.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("shell home page", () => {
  it("stays request-time rendered", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("renders the healthy core seam with the literal make demo greps for", async () => {
    expect(await render()).toContain("core: ok (contract v3)");
  });

  it("reports core unavailable when the health URL is unset, and still renders the session", async () => {
    vi.stubEnv("RHEO_CORE_INTERNAL_URL", "");
    const html = await render();
    expect(html).toContain("core: unavailable");
    expect(html).toContain("account: account acct-synthetic");
  });

  it("checks routing before the session: routing down hides a valid session", async () => {
    seams.routing = { state: "unavailable" };
    const html = await render();
    expect(html).toContain("routing: unavailable");
    expect(html).not.toContain("account:");
    expect(html).not.toContain("data-switcher-action");
    expect(html).not.toContain("Sign out");
    expect(html).toContain("core: ok (contract v3)");
  });

  it("renders session unavailable as its own state", async () => {
    seams.session = { state: "unavailable" };
    const html = await render();
    expect(html).toContain("session: unavailable");
    expect(html).not.toContain("Sign in");
    expect(html).not.toContain("Sign out");
  });

  it("renders a signed-out session with the built sign-in link and no account controls", async () => {
    seams.session = { state: "unauthenticated", refusal: "session_expired" };
    const html = await render();
    expect(html).toContain("session: signed out (session_expired)");
    const href = loginHref(CONFIG).replaceAll("&", "&amp;");
    expect(html).toMatch(new RegExp(`<a [^>]*href="${escapeRegExp(href)}"[^>]*>Sign in</a>`));
    expect(html).not.toContain("data-switcher-action");
    expect(html).not.toContain("Sign out");
  });

  it("renders a signed-in account with the switcher and logout on the built actions", async () => {
    const html = await render();
    expect(html).toContain("account: account acct-synthetic");
    expect(html).toContain("workspace: ws-alpha (owner)");
    expect(html).toContain(`data-switcher-action="${switcherAction(CONFIG)}"`);
    expect(html).toContain('data-active-workspace="ws-alpha"');
    const logout = new RegExp(`<form[^>]* action="${escapeRegExp(logoutAction(CONFIG))}"[^>]*>`);
    expect(logout.exec(html)?.[0]).toContain('method="post"');
  });

  it("resolves the session from the request cookie and forwarded host", async () => {
    await render();
    expect(seams.fetchSessionCalls).toEqual([
      { sessionSecret: "synthetic-session-secret", host: "circuit.example.test" },
    ]);
  });
});

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
