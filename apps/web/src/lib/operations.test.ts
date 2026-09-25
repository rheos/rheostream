import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { callOperation, outcomeOf } from "@/lib/operations";

/**
 * The operation client's envelope narrowing and its request shape. The listener's
 * envelope is `api_routes.envelope`'s; a refusal arrives on a non-2xx status with
 * the same body, so the status is never what decides the outcome.
 */

const IDENTITY = { sessionSecret: "ab12", host: "circuit.example.test" };

function jsonResponse(body: unknown, statusCode: number): Response {
  return new Response(JSON.stringify(body), {
    status: statusCode,
    headers: { "content-type": "application/json" },
  });
}

describe("outcomeOf", () => {
  it("maps a succeeded envelope to ok with its result", () => {
    expect(
      outcomeOf({ state: "succeeded", operation_id: null, result: { items: [] } }),
    ).toEqual({ state: "ok", result: { items: [] } });
  });

  it("maps an error envelope to refused with its code and text", () => {
    expect(
      outcomeOf({
        state: "not_found",
        operation_id: null,
        error: { error_code: "not_found", error_text: "no such memory" },
      }),
    ).toEqual({ state: "refused", code: "not_found", text: "no such memory" });
  });

  it("keeps a refusal with a null error_text as refused, with empty text", () => {
    expect(
      outcomeOf({ state: "x", operation_id: null, error: { error_code: "x", error_text: null } }),
    ).toEqual({ state: "refused", code: "x", text: "" });
  });

  it("maps a succeeded envelope with no result key to ok with a null result", () => {
    // `api_routes.envelope` omits `result` when the operation's result is None.
    expect(outcomeOf({ state: "succeeded", operation_id: null })).toEqual({
      state: "ok",
      result: null,
    });
  });

  it.each([
    ["a pending answer", { state: "pending", operation_id: "op-1", result: null }],
    ["a pending answer with no result key", { state: "pending", operation_id: "op-1" }],
    ["an error with no code", { state: "x", operation_id: null, error: {} }],
    ["a body with no state", { result: {} }],
    ["the listener's own 401 detail", { detail: "internal secret required" }],
    ["a non-object", "succeeded"],
  ])("maps %s to unavailable", (_label, body) => {
    expect(outcomeOf(body)).toEqual({ state: "unavailable" });
  });
});

describe("callOperation", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", "http://core-internal.example.test");
    vi.stubEnv("RHEO_INTERNAL_SECRET", "synthetic-internal-secret");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts the input to the internal operations route with the three headers", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ state: "succeeded", operation_id: null, result: { ok: 1 } }, 200),
    );
    const outcome = await callOperation("module.thing.read", { limit: 3 }, IDENTITY);
    expect(outcome).toEqual({ state: "ok", result: { ok: 1 } });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://core-internal.example.test/internal/v1/operations/module.thing.read",
    );
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ limit: 3 }));
    expect(init?.headers).toMatchObject({
      "X-Rheo-Internal": "synthetic-internal-secret",
      "X-Rheo-Session": "ab12",
      "X-Rheo-Host": "circuit.example.test",
    });
  });

  it("encodes the operation name as one path segment", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ state: "succeeded", result: {} }, 200));
    await callOperation("a/b", {}, IDENTITY);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://core-internal.example.test/internal/v1/operations/a%2Fb",
    );
  });

  it("reads a refusal's body whatever its status", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          state: "role_not_permitted",
          operation_id: null,
          error: { error_code: "role_not_permitted", error_text: "owner only" },
        },
        403,
      ),
    );
    expect(await callOperation("x.y.z", {}, IDENTITY)).toEqual({
      state: "refused",
      code: "role_not_permitted",
      text: "owner only",
    });
  });

  it("answers session_missing without a round trip when the cookie is absent", async () => {
    const outcome = await callOperation("x.y.z", {}, { sessionSecret: undefined, host: "h" });
    expect(outcome).toEqual({ state: "refused", code: "session_missing", text: "" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is unavailable with the internal API unconfigured", async () => {
    vi.stubEnv("RHEO_INTERNAL_SECRET", "");
    expect(await callOperation("x.y.z", {}, IDENTITY)).toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is unavailable when the fetch rejects or the body is not JSON", async () => {
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));
    expect(await callOperation("x.y.z", {}, IDENTITY)).toEqual({ state: "unavailable" });
    fetchMock.mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 502 }));
    expect(await callOperation("x.y.z", {}, IDENTITY)).toEqual({ state: "unavailable" });
  });
});
