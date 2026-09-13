import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";
import {
  callbackUrl,
  continueHref,
  loginHref,
  logoutAction,
  postLoginReturn,
  spikePath,
  switcherAction,
} from "@/lib/routing/links";

/**
 * The four links B10 names, plus the callback URL and the continue hop, asserted
 * against the shared fixture's own expected strings rather than against literals
 * written here (B10: "the login link, post-login redirect, switcher action, and
 * logout action are built only through the routing functions").
 *
 * The load-bearing assertion in this file is the *shape* one: `switcherAction`
 * and `logoutAction` must be bare same-host paths, and `loginHref`/`callbackUrl`
 * must be absolute. Swapping either pair produces a URL that still looks
 * plausible and still resolves in path mode, and breaks only in subdomain mode
 * against a real second host.
 */
const FIXTURES = path.resolve(
  import.meta.dirname,
  "../../../../../tests/fixtures/routing",
);

function readFixture<T>(name: string): T {
  return JSON.parse(readFileSync(path.join(FIXTURES, name), "utf8")) as T;
}

const MODES = ["path", "subdomain"] as const;
type Mode = (typeof MODES)[number];

const CONFIGS: Record<Mode, RoutingConfig> = {
  path: readFixture<RoutingConfig>("path-mode.json"),
  subdomain: readFixture<RoutingConfig>("subdomain-mode.json"),
};

interface Expectation {
  mode: Mode;
  surface?: string;
  path?: string;
  expected?: string;
}

const EXPECTATIONS = readFixture<Expectation[]>("expected-urls.json");

const MODE = (process.env.RHEO_ROUTING_MODE ?? "path") as Mode;
const CONFIG = CONFIGS[MODE];

/** The fixture's own expected URL for one (surface, path) in the mode under test. */
function expectedUrl(surface: string, forPath: string): string {
  const entry = EXPECTATIONS.find(
    (candidate) =>
      candidate.mode === MODE &&
      candidate.surface === surface &&
      candidate.path === forPath,
  );
  if (entry?.expected === undefined) {
    throw new Error(`fixture has no ${MODE} entry for ${surface} ${forPath}`);
  }
  return entry.expected;
}

describe(`links in ${MODE} mode`, () => {
  it("sends the post-login landing to the shell root", () => {
    expect(postLoginReturn(CONFIG)).toBe(expectedUrl("shell", "/"));
  });

  it("builds the sign-in link on the identity surface, carrying the return", () => {
    const href = loginHref(CONFIG);
    expect(href.startsWith(`${expectedUrl("identity", "/login")}?`)).toBe(true);
    const query = new URL(href).searchParams;
    expect(query.get("return")).toBe(postLoginReturn(CONFIG));
  });

  it("builds the OAuth callback URL the operator registers", () => {
    expect(callbackUrl(CONFIG)).toBe(expectedUrl("identity", "/callback"));
  });

  it("keeps the switcher and logout actions on the current host", () => {
    // Bare paths, not absolute URLs. `/auth/*` is served on every application
    // host in both modes, and the `rheo_session` cookie is host-only: an
    // absolute URL here would send a subdomain-mode POST to the identity host,
    // which holds no cookie for the shell host and would refuse
    // `session_missing`. In path mode both forms happen to work, which is
    // exactly why this is asserted rather than assumed.
    expect(switcherAction(CONFIG)).toBe("/auth/session/workspace");
    expect(logoutAction(CONFIG)).toBe("/auth/logout");
    for (const action of [switcherAction(CONFIG), logoutAction(CONFIG)]) {
      expect(action.startsWith("/")).toBe(true);
      expect(action.startsWith("//")).toBe(false);
      expect(action).not.toMatch(/^https?:/);
    }
  });

  it("builds an absolute continue hop on the identity host", () => {
    const href = continueHref(CONFIG, {
      returnTarget: "https://circuit.example.test/reports",
      nonce: "abc123",
    });
    const url = new URL(href);
    expect(url.protocol).toBe("https:");
    expect(url.pathname).toBe("/auth/continue");
    expect(url.searchParams.get("return")).toBe(
      "https://circuit.example.test/reports",
    );
    expect(url.searchParams.get("nonce")).toBe("abc123");
  });
});

describe("links across both modes", () => {
  it("puts the sign-in link on a different host than the shell in subdomain mode", () => {
    // The whole point of the absolute form: in subdomain mode the browser has to
    // leave the shell host to reach the identity host.
    const subdomain = CONFIGS.subdomain;
    expect(new URL(loginHref(subdomain)).host).toBe("auth.example.test");
    expect(new URL(postLoginReturn(subdomain)).host).toBe("circuit.example.test");

    const pathMode = CONFIGS.path;
    expect(new URL(loginHref(pathMode)).host).toBe("example.test");
    expect(new URL(postLoginReturn(pathMode)).host).toBe("example.test");
  });

  it("keeps the same-host actions identical in both modes", () => {
    for (const mode of MODES) {
      expect(switcherAction(CONFIGS[mode])).toBe("/auth/session/workspace");
      expect(logoutAction(CONFIGS[mode])).toBe("/auth/logout");
    }
  });
});

/**
 * `spikePath` (0v), deleted with the rest of the spike at 0c0's branch cut.
 *
 * Built over object literals rather than the shared `path-mode.json` /
 * `subdomain-mode.json` fixtures on purpose: those live under
 * `tests/fixtures/routing/`, outside this run's file map, and carry **no** module
 * surfaces at all — so neither the present-surface case nor the absent-surface
 * case could be expressed against them, and adding a `spike` entry to a ratified
 * cross-language fixture to test a throwaway module would be the wrong trade.
 */
const SURFACE: SurfaceConfig = { host: "circuit" };
const WITH_SPIKE: RoutingConfig = {
  mode: "path",
  scheme: "https",
  base_host: "example.test",
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
const WITHOUT_SPIKE: RoutingConfig = {
  ...WITH_SPIKE,
  surfaces: { ...WITH_SPIKE.surfaces, modules: {} },
};

describe("the spike surface's own link", () => {
  it("stays relative, so the 303 lands on the origin the browser is already on", () => {
    // The load-bearing assertion, and the home for the `urlFor`-instead-of-
    // `spikePath` mutant: `urlFor` is absolute in both modes, and under the
    // driver's port-free `base_host` (D-37) an absolute spike URL evaluates to
    // `http://localhost/spike` — port 80, nothing listening — so `curl -L`
    // fails to connect on a redirect that still looks like a success.
    const target = spikePath(WITH_SPIKE, "/");
    expect(target).toBe("/spike/");
    expect(target.startsWith("/")).toBe(true);
    expect(target.startsWith("//")).toBe(false);
    expect(target).not.toMatch(/^https?:/);
    expect(() => new URL(target)).toThrow();
  });

  it("carries the query the driver asserts on without an absolute host", () => {
    expect(`${spikePath(WITH_SPIKE, "/")}?workspace=selected`).toBe(
      "/spike/?workspace=selected",
    );
  });

  it("throws when the module surface is absent rather than degrading to the root", () => {
    // D-6 populates `surfaces.modules` from the loaded manifests, so an absent
    // `spike` surface means that wiring is broken. Falling back to an empty
    // prefix would redirect to `/` — the shell root — while still carrying
    // `?workspace=selected`, and the driver would print PASS for a page that
    // never rendered.
    expect(() => spikePath(WITHOUT_SPIKE, "/")).toThrow(
      /unknown routing surface 'spike'/,
    );
  });
});
