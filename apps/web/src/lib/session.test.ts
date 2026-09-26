import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchSession } from "@/lib/session";

/**
 * The `/internal/v1/session` client: exact header construction, and a mapping
 * that fails **closed**.
 *
 * The header names are 08's contract and are asserted as an exact object rather
 * than with `toContain`, so renaming one — `X-Rheo-Session` to `X-Session`, say —
 * fails here instead of silently authenticating nobody in production.
 *
 * Everything that is not a 200 carrying a complete signed-in body resolves to
 * "unauthenticated" or "unavailable". The case worth naming is the body that
 * *claims* `"ok"` without the fields: treating that as a signed-in account with
 * unknown details is precisely the fail-open this mapping exists to prevent, and
 * it is the shape a partially-deployed listener would actually produce.
 */
const BASE_URL = "http://core.internal:8100";
const SECRET = "internal-secret-value";
const SESSION_SECRET = "a1b2c3d4";
const HOST = "circuit.example.test";

const OK_BODY = {
  state: "ok",
  actor: { kind: "account", id: "01912d1e-0000-7000-8000-000000000001" },
  active_workspace_id: "01912d1e-0000-7000-8000-000000000002",
  role: "owner",
  memberships: [
    { workspace_id: "01912d1e-0000-7000-8000-000000000002", role: "owner" },
  ],
};

const ORIGINAL_ENV = { ...process.env };

function okResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

function callFetchSession() {
  return fetchSession({ sessionSecret: SESSION_SECRET, host: HOST });
}

beforeEach(() => {
  process.env.RHEO_CORE_INTERNAL_API_URL = BASE_URL;
  process.env.RHEO_INTERNAL_SECRET = SECRET;
});

afterEach(() => {
  process.env = { ...ORIGINAL_ENV };
  vi.unstubAllGlobals();
});

describe("fetchSession request construction", () => {
  it("sends the three internal headers, by their exact names", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(OK_BODY));
    vi.stubGlobal("fetch", fetchMock);

    await callFetchSession();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE_URL}/internal/v1/session`);
    expect(init.cache).toBe("no-store");
    expect(init.headers).toEqual({
      "X-Rheo-Internal": SECRET,
      "X-Rheo-Session": SESSION_SECRET,
      "X-Rheo-Host": HOST,
    });
  });
});

describe("fetchSession mapping", () => {
  it("maps a complete signed-in body to ok", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse(OK_BODY)));

    await expect(callFetchSession()).resolves.toEqual({
      state: "ok",
      actor: OK_BODY.actor,
      activeWorkspaceId: OK_BODY.active_workspace_id,
      role: OK_BODY.role,
      memberships: OK_BODY.memberships,
    });
  });

  it.each([
    "session_missing",
    "session_expired",
    "session_revoked",
    "membership_missing",
    "workspace_unavailable",
  ])("maps the %s refusal to unauthenticated", async (refusal) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse({ state: refusal })));

    await expect(callFetchSession()).resolves.toEqual({
      state: "unauthenticated",
      refusal,
    });
  });

  it("maps workspace_unselected to no-workspace, a live session carrying no account", async () => {
    // #157: core only reaches this refusal after the session itself checked out,
    // so it is not "signed out". It still carries no actor, role or memberships.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse({ state: "workspace_unselected" })),
    );

    await expect(callFetchSession()).resolves.toEqual({ state: "no-workspace" });
  });

  it("refuses a body that claims ok without the fields", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse({ state: "ok" })));

    await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
  });

  it.each([
    ["a missing actor", { ...OK_BODY, actor: undefined }],
    ["a non-string workspace id", { ...OK_BODY, active_workspace_id: 7 }],
    ["memberships that are not a list", { ...OK_BODY, memberships: {} }],
    ["a membership of the wrong shape", { ...OK_BODY, memberships: [{ role: "owner" }] }],
  ])("refuses %s", async (_label, body) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse(body)));

    await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades the listener's own 401 to unavailable, not to signed out", async () => {
    // A wrong X-Rheo-Internal says nothing about the session.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }),
    );

    await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades a fetch rejection to unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
  });

  it("degrades an unparseable body to unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError("not json");
        },
      }),
    );

    await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
  });
});

describe("fetchSession short circuits", () => {
  it("reports session_missing with no cookie, without a round trip", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(OK_BODY));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchSession({ sessionSecret: undefined, host: HOST }),
    ).resolves.toEqual({ state: "unauthenticated", refusal: "session_missing" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports session_missing with no forwarded host", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(OK_BODY));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchSession({ sessionSecret: SESSION_SECRET, host: undefined }),
    ).resolves.toEqual({ state: "unauthenticated", refusal: "session_missing" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["RHEO_CORE_INTERNAL_API_URL", "RHEO_INTERNAL_SECRET"])(
    "degrades an unset %s to unavailable",
    async (variable) => {
      delete process.env[variable];
      const fetchMock = vi.fn().mockResolvedValue(okResponse(OK_BODY));
      vi.stubGlobal("fetch", fetchMock);

      await expect(callFetchSession()).resolves.toEqual({ state: "unavailable" });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});
