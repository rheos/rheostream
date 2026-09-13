import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  runOperation,
  succeeded,
  type OperationInput,
  type OperationName,
  type OperationOutcome,
  type OperationResult,
} from "@/lib/spike/client";

/**
 * `runOperation`'s fail-closed narrowing (0v, C4).
 *
 * The seam under test is the one `lib/session.ts` names: a 200 whose body fails
 * the guard is **not** a success. Every case below that is not an envelope with
 * `state: "succeeded"` and a result object resolves to `unavailable`, and none of
 * them can produce a result the page would render.
 *
 * The second seam is the one place this client departs from `session.ts` on
 * purpose: it reads the envelope on a **non-2xx** as well, because the status is
 * derived from the state (`input_invalid` is a 422). Treating a 422 as
 * `unavailable` would make a nameable refusal indistinguishable from an
 * unreachable listener, and the spike's `?add=` parameter would say the wrong
 * thing.
 */

const BASE = "http://127.0.0.1:8100";
const SECRET = "internal-secret";
const SESSION = "deadbeef";
const HOST = "localhost";

/** The envelope both listeners answer with, as `api_routes.envelope` builds it. */
function envelope(body: Record<string, unknown>): Record<string, unknown> {
  return { operation_id: null, ...body };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", BASE);
  vi.stubEnv("RHEO_INTERNAL_SECRET", SECRET);
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function addNote(body = "a note") {
  return runOperation({
    name: "spike.note.add",
    input: { body },
    sessionSecret: SESSION,
    host: HOST,
  });
}

describe("the typed operations client", () => {
  it("posts to the internal listener's operations route with its three headers", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        envelope({
          state: "succeeded",
          result: {
            ref: "spike:note:1",
            body: "a note",
            created_at: "2026-09-13T00:00:00Z",
            revision: 1,
          },
        }),
      ),
    );

    await addNote();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // The *internal* path, not the `/api/v1/...` key the generated types are
    // indexed by (finding F20).
    expect(url).toBe(`${BASE}/internal/v1/operations/spike.note.add`);
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Rheo-Internal"]).toBe(SECRET);
    expect(headers["X-Rheo-Session"]).toBe(SESSION);
    expect(headers["X-Rheo-Host"]).toBe(HOST);
    // FR 5: no workspace identifier anywhere in the request.
    const sent = `${url} ${JSON.stringify(init.headers)} ${String(init.body)}`;
    for (const reserved of [
      "workspace_id",
      "database",
      "dsn",
      "connection_string",
      "schema",
    ]) {
      expect(sent).not.toContain(reserved);
    }
  });

  it("reports a well-formed success with its result", async () => {
    const result = {
      ref: "spike:note:1",
      body: "a note",
      created_at: "2026-09-13T00:00:00Z",
      revision: 1,
    };
    fetchMock.mockResolvedValue(
      jsonResponse(200, envelope({ state: "succeeded", result })),
    );

    const outcome = await addNote();

    expect(outcome.state).toBe("succeeded");
    expect(outcome).toEqual({ state: "succeeded", result });
  });

  it("reports a refusal's own state from a non-2xx, rather than collapsing it", async () => {
    // `outcome_status` maps `input_invalid` to 422. If this returned
    // `unavailable`, the spike's add handler would redirect `?add=unavailable`
    // for an operation that refused for a reason the page can name.
    fetchMock.mockResolvedValue(
      jsonResponse(
        422,
        envelope({
          state: "input_invalid",
          error: { error_code: "input_invalid", error_text: "body: too short" },
        }),
      ),
    );

    const outcome = await addNote("");

    expect(outcome.state).toBe("input_invalid");
    expect(outcome).toEqual({
      state: "input_invalid",
      error: { error_code: "input_invalid", error_text: "body: too short" },
    });
  });

  it("reports a refusal that carries no error object", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(403, envelope({ state: "role_not_permitted" })),
    );

    expect(await addNote()).toEqual({
      state: "role_not_permitted",
      error: null,
    });
  });

  it("refuses to call a body claiming success with no result a success", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, envelope({ state: "succeeded" })));

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("refuses to call a non-object result a success", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, envelope({ state: "succeeded", result: "a note" })),
    );

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("rejects a body that is not this envelope at all", async () => {
    // The internal listener's own shared-secret 401 is FastAPI's
    // `{"detail": ...}` — it carries no `state` and no `operation_id`, and says
    // nothing about any operation.
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Unauthorized" }));

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("rejects an envelope with no operation_id field", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { state: "succeeded", result: {} }));

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("treats an unparseable body as unavailable", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>502</html>", {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
    );

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("treats a rejected fetch as unavailable", async () => {
    fetchMock.mockRejectedValue(new Error("ECONNREFUSED"));

    expect(await addNote()).toEqual({ state: "unavailable" });
  });

  it("never calls the listener with no credential configured", async () => {
    vi.stubEnv("RHEO_INTERNAL_SECRET", "");

    expect(await addNote()).toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never calls the listener with no session", async () => {
    const outcome = await runOperation({
      name: "spike.note.add",
      input: { body: "a note" },
      sessionSecret: undefined,
      host: HOST,
    });

    expect(outcome).toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never calls the listener with no host", async () => {
    const outcome = await runOperation({
      name: "spike.note.add",
      input: { body: "a note" },
      sessionSecret: SESSION,
      host: undefined,
    });

    expect(outcome).toEqual({ state: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

/**
 * The shape-5 assertion: these names and fields exist only because
 * `rheo openapi` emitted them and `openapi-typescript` turned them into types.
 * Nothing in `apps/web` restates them. Rename `NoteAddInput.body` in
 * `modules/spike` and this block stops compiling — which is the seam the run
 * exists to prove, and `tsc --noEmit` is the assertion that runs it.
 */
/**
 * The success predicate.
 *
 * It exists because the outcome union is **not** discriminable by `state`: the
 * refusal member's `state` is `string`, which includes the literal `"succeeded"`,
 * so TypeScript narrows nothing on `outcome.state !== "succeeded"`. The
 * discriminant is the presence of `result`.
 */
describe("the success predicate", () => {
  it("accepts only a success carrying a result", () => {
    const ok: OperationOutcome<"spike.note.add"> = {
      state: "succeeded",
      result: {
        ref: "spike:note:1",
        body: "a note",
        created_at: "2026-09-13T00:00:00Z",
        revision: 1,
      },
    };
    expect(succeeded(ok)).toBe(true);

    for (const outcome of [
      { state: "unavailable" },
      { state: "input_invalid", error: null },
      // A state of "succeeded" with no result is not a success: `runOperation`
      // never builds one, and the predicate refuses it too.
      { state: "succeeded", error: null },
    ] as OperationOutcome<"spike.note.add">[]) {
      expect(succeeded(outcome)).toBe(false);
    }
  });
});

describe("the generated types", () => {
  it("carries every registered operation, spike included", () => {
    const names: OperationName[] = [
      "core.workspace.status",
      "spike.note.add",
      "spike.note.list",
      "spike.note.commit_early",
    ];
    expect(names).toHaveLength(4);

    const input: OperationInput<"spike.note.add"> = { body: "a note" };
    expect(input.body).toBe("a note");

    const listed: OperationResult<"spike.note.list"> = { notes: [] };
    expect(listed.notes).toEqual([]);
  });
});
