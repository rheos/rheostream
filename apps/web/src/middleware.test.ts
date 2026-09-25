import { readFileSync } from "node:fs";
import path from "node:path";

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoutingConfig } from "@/lib/routing/config";

/**
 * The middleware's redirect and, above all, the `rheo_continue` cookie's
 * attributes (B18).
 *
 * `continue-redirect.test.ts` pins the URL; nothing there sees a cookie, which is
 * why this file exists. The five properties asserted below are the same five
 * `sessions/cookies.py` enforces server-side, and the absence of `Domain` is the
 * one that matters most: a parent-domain cookie would be readable on every
 * subdomain, which is the whole reason the session is host-only (A11).
 *
 * `vi.resetModules()` per test because `loadRoutingConfig` memoises a successful
 * load; without it the first test's configuration would satisfy the
 * routing-unavailable case and it would pass vacuously.
 */
const FIXTURES = path.resolve(import.meta.dirname, "../../../tests/fixtures/routing");

function readFixture(name: string): RoutingConfig {
  return JSON.parse(readFileSync(path.join(FIXTURES, name), "utf8")) as RoutingConfig;
}

const SUBDOMAIN_CONFIG = readFixture("subdomain-mode.json");
const PATH_CONFIG = readFixture("path-mode.json");

const BASE_URL = "http://core.internal:8100";
const SECRET = "internal-secret-value";
const ORIGINAL_ENV = { ...process.env };

async function middlewareWith(config: RoutingConfig | null) {
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    config === null
      ? vi.fn().mockRejectedValue(new Error("ECONNREFUSED"))
      : vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => config }),
  );
  const loaded = await import("@/middleware");
  return loaded.middleware;
}

function requestFor(
  url: string,
  options: { headers?: Record<string, string>; cookie?: string } = {},
) {
  const headers = new Headers(options.headers ?? {});
  if (options.cookie !== undefined) {
    headers.set("cookie", options.cookie);
  }
  return new NextRequest(url, { headers });
}

/** The attributes of one `Set-Cookie` header, lowercased (they are case-insensitive). */
function cookieAttributes(setCookie: string): Map<string, string> {
  const attributes = new Map<string, string>();
  const [, ...rest] = setCookie.split(";");
  for (const part of rest) {
    const [name, value = ""] = part.split("=");
    attributes.set(name.trim().toLowerCase(), value.trim().toLowerCase());
  }
  return attributes;
}

function continueCookie(headers: Headers): string {
  const found = headers
    .getSetCookie()
    .find((value) => value.startsWith("rheo_continue="));
  expect(found).toBeDefined();
  return found as string;
}

beforeEach(() => {
  process.env.RHEO_CORE_INTERNAL_API_URL = BASE_URL;
  process.env.RHEO_INTERNAL_SECRET = SECRET;
  delete process.env.RHEO_PROFILE;
});

afterEach(() => {
  process.env = { ...ORIGINAL_ENV };
  vi.unstubAllGlobals();
});

describe("an unauthenticated request in subdomain mode", () => {
  it("redirects across to the identity host and sets the nonce it will be asked for", async () => {
    const middleware = await middlewareWith(SUBDOMAIN_CONFIG);
    const response = await middleware(
      requestFor("https://circuit.example.test/reports", {
        headers: { host: "circuit.example.test" },
      }),
    );

    const location = response.headers.get("location");
    expect(location).not.toBeNull();
    const target = new URL(location as string);
    expect(target.host).toBe("auth.example.test");
    expect(target.pathname).toBe("/auth/continue");
    expect(target.searchParams.get("return")).toBe(
      "https://circuit.example.test/reports",
    );

    // The cookie value and the query nonce have to be the same string, or
    // `/auth/continue` cannot bind the grant to this browser.
    const cookie = continueCookie(response.headers);
    const value = cookie.slice("rheo_continue=".length).split(";")[0];
    expect(value).toBe(target.searchParams.get("nonce"));
    expect(value).toMatch(/^[0-9a-f]{32}$/);
  });

  it("gives rheo_continue all five properties and no Domain", async () => {
    const middleware = await middlewareWith(SUBDOMAIN_CONFIG);
    const response = await middleware(
      requestFor("https://circuit.example.test/", {
        headers: { host: "circuit.example.test" },
      }),
    );

    const attributes = cookieAttributes(continueCookie(response.headers));
    expect(attributes.has("httponly")).toBe(true);
    expect(attributes.get("samesite")).toBe("lax");
    expect(attributes.get("path")).toBe("/");
    expect(attributes.has("secure")).toBe(true);
    expect(attributes.has("domain")).toBe(false);
    // Five minutes, matching `build_cookie` server-side (#136).
    expect(attributes.get("max-age")).toBe("300");
  });

  it("prefers the forwarded host over the upstream one", async () => {
    const middleware = await middlewareWith(SUBDOMAIN_CONFIG);
    const response = await middleware(
      requestFor("https://internal.invalid/reports", {
        headers: {
          host: "internal.invalid",
          "x-forwarded-host": "circuit.example.test",
        },
      }),
    );

    const target = new URL(response.headers.get("location") as string);
    expect(target.searchParams.get("return")).toBe(
      "https://circuit.example.test/reports",
    );
  });
});

describe("an unauthenticated request in path mode", () => {
  it("redirects to /auth/continue on the single application host", async () => {
    const middleware = await middlewareWith(PATH_CONFIG);
    const response = await middleware(
      requestFor("https://example.test/", { headers: { host: "example.test" } }),
    );

    const target = new URL(response.headers.get("location") as string);
    expect(target.host).toBe("example.test");
    expect(target.pathname).toBe("/auth/continue");
    expect(continueCookie(response.headers)).toBeTruthy();
  });
});

describe("requests the middleware passes through", () => {
  it("passes a request carrying a session cookie straight through", async () => {
    const middleware = await middlewareWith(SUBDOMAIN_CONFIG);
    const response = await middleware(
      requestFor("https://circuit.example.test/", {
        headers: { host: "circuit.example.test" },
        cookie: "rheo_session=a1b2c3",
      }),
    );

    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("renders rather than guessing when the routing configuration is unavailable", async () => {
    // `make demo` runs exactly this path: it leaves RHEO_CORE_INTERNAL_API_URL
    // unset, so the shell must render its unavailable state instead of 302-ing a
    // curl that has no session.
    const middleware = await middlewareWith(null);
    const response = await middleware(
      requestFor("https://circuit.example.test/", {
        headers: { host: "circuit.example.test" },
      }),
    );

    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("passes through when no host header reaches it", async () => {
    const middleware = await middlewareWith(SUBDOMAIN_CONFIG);
    const request = requestFor("https://circuit.example.test/");
    request.headers.delete("host");
    const response = await middleware(request);

    expect(response.headers.get("location")).toBeNull();
  });
});

/**
 * A misrouted proxy that sends identity-path traffic to the web tier (#119). Each
 * case runs against both fixtures, with the host that mode serves the path on.
 */
const MODES = [
  {
    mode: "subdomain",
    config: SUBDOMAIN_CONFIG,
    identityHost: "auth.example.test",
    appHost: "circuit.example.test",
  },
  {
    mode: "path",
    config: PATH_CONFIG,
    identityHost: "example.test",
    appHost: "example.test",
  },
] as const;

describe.each(MODES)(
  "a request on the identity path in $mode mode",
  ({ config, identityHost, appHost }) => {
    it.each([
      { label: "under it, signed out", pathname: "/auth/continue", cookie: undefined },
      {
        label: "under it, with a session cookie",
        pathname: "/auth/continue",
        cookie: "rheo_session=a1b2c3",
      },
      { label: "the bare path, signed out", pathname: "/auth", cookie: undefined },
      {
        label: "the bare path, with a session cookie",
        pathname: "/auth",
        cookie: "rheo_session=a1b2c3",
      },
    ])("answers a plain 404: $label", async ({ pathname, cookie }) => {
      const middleware = await middlewareWith(config);
      const response = await middleware(
        requestFor(`https://${identityHost}${pathname}`, {
          headers: { host: identityHost },
          cookie,
        }),
      );

      expect(response.status).toBe(404);
      expect(response.headers.get("location")).toBeNull();
      expect(response.headers.getSetCookie()).toEqual([]);
    });

    it("does not catch a sibling path that merely shares the prefix", async () => {
      const middleware = await middlewareWith(config);
      const response = await middleware(
        requestFor(`https://${appHost}/authors`, { headers: { host: appHost } }),
      );

      const target = new URL(response.headers.get("location") as string);
      expect(target.pathname).toBe("/auth/continue");
      expect(target.searchParams.get("return")).toBe(`https://${appHost}/authors`);
      expect(continueCookie(response.headers)).toBeTruthy();
    });
  },
);

/**
 * A root identity prefix leaves nothing to distinguish identity routes by, so the
 * guard must match nothing rather than everything.
 */
describe.each(
  MODES.flatMap((entry) =>
    ["/", ""].map((rootPath) => ({ ...entry, rootPath })),
  ),
)(
  "a root identity prefix ($rootPath) in $mode mode",
  ({ config, appHost, rootPath }) => {
    const rootConfig = {
      ...config,
      surfaces: {
        ...config.surfaces,
        identity: { ...config.surfaces.identity, path: rootPath },
      },
    } as RoutingConfig;

    it("still redirects an ordinary signed-out route instead of 404-ing it", async () => {
      const middleware = await middlewareWith(rootConfig);
      const response = await middleware(
        requestFor(`https://${appHost}/reports`, { headers: { host: appHost } }),
      );

      expect(response.status).not.toBe(404);
      expect(response.headers.get("location")).not.toBeNull();
    });

    it("passes a signed-in ordinary route through", async () => {
      const middleware = await middlewareWith(rootConfig);
      const response = await middleware(
        requestFor(`https://${appHost}/reports`, {
          headers: { host: appHost },
          cookie: "rheo_session=a1b2c3",
        }),
      );

      expect(response.status).toBe(200);
      expect(response.headers.get("location")).toBeNull();
    });
  },
);

describe("the identity-path guard with no routing configuration", () => {
  it("passes an identity-path request through unchanged", async () => {
    const middleware = await middlewareWith(null);
    const response = await middleware(
      requestFor("https://auth.example.test/auth/continue", {
        headers: { host: "auth.example.test" },
      }),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.getSetCookie()).toEqual([]);
  });
});

describe("the Secure attribute follows the server-side rule", () => {
  it("is dropped under http outside production", async () => {
    const middleware = await middlewareWith({ ...PATH_CONFIG, scheme: "http" });
    const response = await middleware(
      requestFor("http://example.test/", { headers: { host: "example.test" } }),
    );

    expect(cookieAttributes(continueCookie(response.headers)).has("secure")).toBe(
      false,
    );
  });

  it("is kept under http when the profile is production", async () => {
    process.env.RHEO_PROFILE = "production";
    const middleware = await middlewareWith({ ...PATH_CONFIG, scheme: "http" });
    const response = await middleware(
      requestFor("http://example.test/", { headers: { host: "example.test" } }),
    );

    expect(cookieAttributes(continueCookie(response.headers)).has("secure")).toBe(
      true,
    );
  });
});
