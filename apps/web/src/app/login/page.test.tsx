import { readFileSync } from "node:fs";
import path from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RoutingConfig } from "@/lib/routing/config";
import { loginHref } from "@/lib/routing/links";
import type { RoutingConfigResult } from "@/lib/routing/load";

/** Behavior regression tests for the sign-in page after its restyle. */

const FIXTURES = path.resolve(import.meta.dirname, "../../../../../tests/fixtures/routing");
const MODE = process.env.RHEO_ROUTING_MODE === "subdomain" ? "subdomain" : "path";
const CONFIG = JSON.parse(
  readFileSync(path.join(FIXTURES, `${MODE}-mode.json`), "utf8"),
) as RoutingConfig;

const seams = vi.hoisted(() => ({
  routing: { state: "unavailable" } as RoutingConfigResult,
}));

vi.mock("@/lib/routing/load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/routing/load")>()),
  loadRoutingConfig: async () => seams.routing,
}));

const { default: LoginPage, dynamic } = await import("./page");

async function render(): Promise<string> {
  return renderToStaticMarkup(await LoginPage());
}

beforeEach(() => {
  seams.routing = { state: "ok", config: CONFIG };
});

describe("sign-in page", () => {
  it("stays request-time rendered", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("links to the built sign-in URL for the mode under test", async () => {
    const html = await render();
    const href = loginHref(CONFIG).replaceAll("&", "&amp;");
    expect(html).toContain(`href="${href}"`);
    expect(html).toContain(">Sign in</a>");
  });

  it("says sign-in is unavailable, with no link, when routing is down", async () => {
    seams.routing = { state: "unavailable" };
    const html = await render();
    expect(html).toContain("sign-in: unavailable");
    expect(html).not.toContain("<a ");
  });
});
