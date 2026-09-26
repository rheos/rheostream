import { readFileSync } from "node:fs";
import path from "node:path";

import type { ComposedModule, OperationOutcome, ShellApi } from "@rheo-stream/web-contract/screen";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CoreHealthResult } from "@/lib/core-health";
import type { RoutingConfig } from "@/lib/routing/config";
import type { RoutingConfigResult } from "@/lib/routing/load";
import type { SessionResult } from "@/lib/session";

/**
 * #122: a module screen's `shell.call` reaches core only for an operation its own
 * module declares READ-class. The allowlist is the composed module's
 * `readOperations`, which `rheo web compose` writes from the manifest's operation
 * declarations; the screen cannot widen it.
 *
 * The module here is hostile on purpose. Its screen skips every package-level guard
 * (the closed `Operation` union, the ESLint rule) and calls `shell.call` with a
 * mutating operation of its own module, a READ operation of another module, a
 * core mutation, and names that differ from a permitted one only by case,
 * whitespace, a path suffix or an encoded character. Each must come back refused
 * without a single request leaving the web tier, while its own READ operation still
 * reaches core.
 */

const REPO = path.resolve(import.meta.dirname, "../../../..");
const MODE = process.env.RHEO_ROUTING_MODE === "subdomain" ? "subdomain" : "path";

function readJson(relative: string): unknown {
  return JSON.parse(readFileSync(path.join(REPO, relative), "utf8"));
}

const BASE = readJson(`tests/fixtures/routing/${MODE}-mode.json`) as RoutingConfig;
const ROUTED: RoutingConfig = {
  ...BASE,
  surfaces: {
    ...BASE.surfaces,
    modules: { hostile_probe: { host: "hostile", path: "/hostile" } },
  },
};

const PERMITTED = "hostile_probe.note.find";
const REFUSED_NAMES = [
  "hostile_probe.note.add",
  "recallatron.memory.remember",
  "recallatron.memory.read",
  "core.token.issue",
  "core.workspace.status",
  "HOSTILE_PROBE.NOTE.FIND",
  ` ${PERMITTED}`,
  `${PERMITTED}/../add`,
  `${PERMITTED}%2F..%2Fadd`,
  "",
  "__proto__",
  "constructor",
];

const seams = vi.hoisted(() => ({
  routing: { state: "unavailable" } as RoutingConfigResult,
  session: { state: "unavailable" } as SessionResult,
  calls: [] as string[],
  outcomes: new Map<string, OperationOutcome>(),
  shell: null as ShellApi | null,
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

vi.mock("@/modules.generated", () => {
  const screen = async ({ shell }: { shell: ShellApi }) => {
    seams.shell = shell;
    for (const name of [PERMITTED, ...REFUSED_NAMES]) {
      seams.outcomes.set(name, await shell.call(name, {}));
    }
    return null;
  };
  const hostile = {
    id: "hostile_probe",
    surface: "hostile_probe",
    navigation: [],
    routes: [{ id: "home", path: "/", screen }],
    recordViews: [],
    forms: [],
    searchProviders: [],
    readOperations: [PERMITTED],
  } satisfies ComposedModule;
  return { MODULES: [hostile] };
});

const { renderSurface } = await import("./render-surface");

const SIGNED_IN: SessionResult = {
  state: "ok",
  actor: { kind: "account", id: "acct-synthetic" },
  activeWorkspaceId: "ws-alpha",
  role: "owner",
  memberships: [{ workspace_id: "ws-alpha", role: "owner" }],
};

const STATUS = {
  core_version: "0.0.0",
  core_contract_version: 1,
  modules: [
    {
      module_id: "hostile_probe",
      package_version: "0",
      state: "enabled",
      schema_version: null,
    },
  ],
};

function moduleRequest() {
  return MODE === "path"
    ? { host: "example.test", pathname: "/hostile/", query: {} }
    : { host: "hostile.example.test", pathname: "/", query: {} };
}

function answer(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

beforeEach(() => {
  seams.routing = { state: "ok", config: ROUTED };
  seams.session = SIGNED_IN;
  seams.calls.length = 0;
  seams.outcomes.clear();
  seams.shell = null;
  vi.stubEnv("RHEO_CORE_INTERNAL_URL", "http://core.example.test");
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", "http://core-internal.example.test");
  vi.stubEnv("RHEO_INTERNAL_SECRET", "synthetic-internal-secret");
  vi.stubGlobal("fetch", async (url: string) => {
    const name = decodeURIComponent(url.split("/").pop() ?? "");
    seams.calls.push(name);
    if (name === "core.workspace.status") {
      return answer({ state: "succeeded", operation_id: null, result: STATUS });
    }
    return answer({ state: "succeeded", operation_id: null, result: { ok: true } });
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("a module screen's shell.call", () => {
  it("reaches core for its own module's READ operation", async () => {
    renderToStaticMarkup(await renderSurface(moduleRequest()));
    expect(seams.outcomes.get(PERMITTED)).toEqual({ state: "ok", result: { ok: true } });
    expect(seams.calls.filter((name) => name === PERMITTED)).toHaveLength(1);
  });

  it.each(REFUSED_NAMES)("refuses %j without calling core", async (name) => {
    renderToStaticMarkup(await renderSurface(moduleRequest()));
    expect(seams.outcomes.get(name)).toEqual({
      state: "refused",
      code: "operation_not_permitted",
      text: "a module screen may call only its own module's read operations",
    });
    // The shell's own status read is the only other request; the refused name is
    // never sent, in any spelling.
    expect(seams.calls.every((sent) => sent === PERMITTED || sent === "core.workspace.status")).toBe(
      true,
    );
  });

  it("keeps the allowlist it was built with, whatever happens to the array later", async () => {
    const { screenCall } = await import("./screen-call");
    const reads = [PERMITTED];
    const sent: string[] = [];
    const call = screenCall(reads, async (operation) => {
      sent.push(operation);
      return { state: "ok", result: null };
    });
    reads.push("hostile_probe.note.add");
    expect((await call("hostile_probe.note.add", {})).state).toBe("refused");
    expect((await call(PERMITTED, {})).state).toBe("ok");
    expect(sent).toEqual([PERMITTED]);
  });

  it("does not widen when the composed module array is changed after load", async () => {
    const { MODULES } = await import("@/modules.generated");
    const reads = MODULES[0].readOperations as unknown as string[];
    reads.push("hostile_probe.note.add");
    try {
      renderToStaticMarkup(await renderSurface(moduleRequest()));
      expect(seams.outcomes.get("hostile_probe.note.add")?.state).toBe("refused");
      expect(seams.calls).not.toContain("hostile_probe.note.add");
    } finally {
      reads.pop();
    }
  });

  it("refuses a non-string operation without calling core", async () => {
    renderToStaticMarkup(await renderSurface(moduleRequest()));
    const shell = seams.shell;
    expect(shell).not.toBeNull();
    const before = seams.calls.length;
    for (const bogus of [undefined, null, 42, { toString: () => PERMITTED }, [PERMITTED]]) {
      const outcome = await shell!.call(bogus as unknown as string, {});
      expect(outcome.state).toBe("refused");
    }
    expect(seams.calls.length).toBe(before);
  });
});
