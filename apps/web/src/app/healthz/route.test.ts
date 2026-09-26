import { describe, expect, it } from "vitest";

import { config } from "@/middleware";

import { GET } from "./route";

/**
 * The web liveness probe (#156). The compose health check fetches `/healthz`
 * with no session cookie, so the route must answer 200 by itself and the
 * middleware must leave the path alone; a 307 to sign-in would read as healthy
 * to nothing and unhealthy to the check.
 */
describe("GET /healthz", () => {
  it("answers 200 with a fixed, uncached body and calls nothing", async () => {
    const response = GET();
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ status: "ok" });
  });
});

describe("the middleware matcher", () => {
  // Next.js anchors a matcher source at both ends; plain RegExp is close enough
  // for the negative lookahead this file pins.
  const matches = (pathname: string) =>
    config.matcher.some((source) => new RegExp(`^${source}$`).test(pathname));

  it("skips /healthz so the probe is never redirected to sign in", () => {
    expect(matches("/healthz")).toBe(false);
  });

  it("still guards paths that only start with healthz", () => {
    expect(matches("/healthzx")).toBe(true);
    expect(matches("/healthz/extra")).toBe(true);
    expect(matches("/recallatron")).toBe(true);
    expect(matches("/")).toBe(true);
  });
});
