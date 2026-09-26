import { readFileSync } from "node:fs";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The `/internal/v1/routing` client's fail-soft mapping and its request shape.
 *
 * Every failure — a rejected fetch, a non-200, a body of the wrong shape, and
 * **either environment variable unset** — has to degrade to the same
 * "unavailable" sentinel, the way `core-health.test.ts` pins `fetchCoreHealth`.
 * The env-unset cases are the ones a green local suite would not otherwise
 * catch: on a laptop both internal URLs resolve to the same place.
 *
 * `vi.resetModules()` before each import because the module memoises a
 * successful load ("once per process"); without it the first test's config would
 * satisfy every later one and the failure cases would pass vacuously.
 */
const FIXTURE = path.resolve(
  import.meta.dirname,
  "../../../../../tests/fixtures/routing/path-mode.json",
);
const ROUTING_BODY: unknown = JSON.parse(readFileSync(FIXTURE, "utf8"));

const BASE_URL = "http://core.internal:8100";
const SECRET = "internal-secret-value";

const ORIGINAL_ENV = { ...process.env };

async function freshModule() {
  vi.resetModules();
  return import("@/lib/routing/load");
}

function okResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

beforeEach(() => {
  process.env.RHEO_CORE_INTERNAL_API_URL = BASE_URL;
  process.env.RHEO_INTERNAL_SECRET = SECRET;
});

afterEach(() => {
  process.env = { ...ORIGINAL_ENV };
  vi.unstubAllGlobals();
  // The timeout tests spy on AbortSignal.timeout and console.warn; restoring here
  // rather than inline keeps a failing one from leaking its spies into the next.
  vi.restoreAllMocks();
});

describe("loadRoutingConfig", () => {
  it("returns the configuration and sends the internal secret header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();
    const result = await loadRoutingConfig();

    expect(result).toEqual({ state: "ok", config: ROUTING_BODY });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE_URL}/internal/v1/routing`);
    expect(init.cache).toBe("no-store");
    expect(init.headers).toEqual({ "X-Rheo-Internal": SECRET });
  });

  it("reads the internal listener, never the public one", async () => {
    // RHEO_CORE_INTERNAL_URL is 0a's variable for the public listener on 8000 and
    // stays pointed there for page.tsx's /healthz read. Confusing the two passes
    // locally, where both resolve to the same place, and fails only in the
    // container network.
    process.env.RHEO_CORE_INTERNAL_URL = "http://core.internal:8000";
    delete process.env.RHEO_CORE_INTERNAL_API_URL;
    const fetchMock = vi.fn().mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches once per process and reuses the result", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();
    await loadRoutingConfig();
    await loadRoutingConfig();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("degrades a fetch rejection to unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades a non-200 response to unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }),
    );

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades a mis-shaped body to unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse({ mode: "path", scheme: "https" })),
    );

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades an unset RHEO_CORE_INTERNAL_API_URL to unavailable", async () => {
    delete process.env.RHEO_CORE_INTERNAL_API_URL;
    const fetchMock = vi.fn().mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("degrades an unset RHEO_INTERNAL_SECRET to unavailable", async () => {
    delete process.env.RHEO_INTERNAL_SECRET;
    const fetchMock = vi.fn().mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  /**
   * A fetch that never settles on its own: it rejects only when its signal aborts,
   * the way a real fetch to a hung `core` behaves. Without a timeout on the request
   * this would never resolve and the test would fail on vitest's own timeout.
   */
  function hangingFetch() {
    return vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          if (!signal) {
            return; // no signal: hang forever, which is the bug under test
          }
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
    );
  }

  it("bounds the fetch with a timeout and degrades a hang to unavailable", async () => {
    const { loadRoutingConfig, ROUTING_FETCH_TIMEOUT_MS } = await freshModule();
    // Shorten the real timer so the test takes milliseconds, while still checking
    // the production value is what the module asked for.
    const realTimeout = AbortSignal.timeout.bind(AbortSignal);
    const timeoutSpy = vi
      .spyOn(AbortSignal, "timeout")
      .mockImplementation(() => realTimeout(5));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchMock = hangingFetch();
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });

    expect(timeoutSpy).toHaveBeenCalledWith(ROUTING_FETCH_TIMEOUT_MS);
    expect(ROUTING_FETCH_TIMEOUT_MS).toBeGreaterThan(0);
    expect(ROUTING_FETCH_TIMEOUT_MS).toBeLessThanOrEqual(5000);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain("timed out");
    // Neither the internal URL nor the secret reaches the log line.
    expect(String(warn.mock.calls[0][0])).not.toContain(BASE_URL);
    expect(String(warn.mock.calls[0][0])).not.toContain(SECRET);

  });

  it("does not memoise a timeout, so a recovered core is picked up", async () => {
    const { loadRoutingConfig } = await freshModule();
    const realTimeout = AbortSignal.timeout.bind(AbortSignal);
    vi.spyOn(AbortSignal, "timeout").mockImplementation(() => realTimeout(5));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const hanging = hangingFetch();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(hanging)
      .mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    await expect(loadRoutingConfig()).resolves.toEqual({
      state: "ok",
      config: ROUTING_BODY,
    });

  });

  it("logs nothing for an ordinary failure", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    expect(warn).not.toHaveBeenCalled();
  });

  it("does not memoise a failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("ECONNREFUSED"))
      .mockResolvedValue(okResponse(ROUTING_BODY));
    vi.stubGlobal("fetch", fetchMock);

    const { loadRoutingConfig } = await freshModule();

    await expect(loadRoutingConfig()).resolves.toEqual({ state: "unavailable" });
    await expect(loadRoutingConfig()).resolves.toEqual({
      state: "ok",
      config: ROUTING_BODY,
    });
  });
});
