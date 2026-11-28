import { beforeEach, describe, expect, it, vi } from "vitest";
const seams = vi.hoisted(() => ({
  session: { state: "ok", activeWorkspaceId: "workspace" } as Record<string, unknown>,
  enabled: ["current"],
  surfaces: { current: {} } as Record<string, unknown>,
  call: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("next/cache", () => ({ refresh: seams.refresh }));
vi.mock("@/lib/operations", () => ({
  currentSession: async () => seams.session,
  currentWorkspaceStatus: async () => ({ state: "ok", status: {} }),
  enabledModuleIds: () => seams.enabled,
  requestIdentity: async () => ({ sessionSecret: "synthetic", host: "example.test" }),
  callOperation: async (...args: unknown[]) => {
    seams.call(...args);
    return { state: "ok", result: { items: [] } };
  },
}));
vi.mock("@/lib/routing/load", () => ({
  loadRoutingConfig: async () => ({
    state: "ok",
    config: { surfaces: { modules: seams.surfaces } },
  }),
}));
import { readModuleScreen } from "./client-read";
import { clientReadAllowed, hasClientReads } from "./client-read-gate";

const SEARCH = { status: "active", query: "garden", match: "title", limit: 10 };
beforeEach(() => {
  seams.session = { state: "ok", activeWorkspaceId: "workspace" };
  seams.enabled = ["current"];
  seams.surfaces = { current: {} };
  seams.call.mockClear();
  seams.refresh.mockClear();
});

describe("client read server action (#645)", () => {
  it("dispatches a declared client read with the fresh identity and never refreshes", async () => {
    const result = await readModuleScreen("current", "workspace", "current.item.list", SEARCH);
    expect(result.state).toBe("ok");
    expect(seams.call).toHaveBeenCalledWith(
      "current.item.list",
      SEARCH,
      { sessionSecret: "synthetic", host: "example.test" },
      "workspace",
    );
    expect(seams.refresh).not.toHaveBeenCalled();
  });
  it.each([
    ["a read not declared client-readable", "current", "current.summary.get"],
    ["a mutate operation", "current", "current.dependency.add"],
    ["another module's read", "current", "leads.opportunity.list"],
    ["a core operation", "current", "core.workspace.status"],
    ["a module that declares none", "finance", "finance.owed.list"],
    ["a module that does not exist", "nobody", "current.item.list"],
  ])("refuses %s before reaching core", async (_, moduleId, operation) => {
    const result = await readModuleScreen(moduleId, "workspace", operation, {});
    expect(result).toMatchObject({ state: "refused", code: "operation_not_permitted" });
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("refuses a non-string operation", async () => {
    const result = await readModuleScreen(
      "current",
      "workspace",
      { toString: () => "current.item.list" } as unknown as string,
      {},
    );
    expect(result.state).toBe("refused");
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("refuses an old tab after switching workspace", async () => {
    seams.session.activeWorkspaceId = "another";
    const result = await readModuleScreen("current", "workspace", "current.item.list", SEARCH);
    expect(result).toMatchObject({ state: "refused", code: "workspace_changed" });
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("refuses a signed-out session, a disabled module and an unrouted surface", async () => {
    seams.session = { state: "signed-out" };
    await readModuleScreen("current", "workspace", "current.item.list", SEARCH);
    seams.session = { state: "ok", activeWorkspaceId: "workspace" };
    seams.enabled = [];
    await readModuleScreen("current", "workspace", "current.item.list", SEARCH);
    seams.enabled = ["current"];
    seams.surfaces = {};
    await readModuleScreen("current", "workspace", "current.item.list", SEARCH);
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("offers read only to modules that declare client reads", () => {
    expect(hasClientReads("current")).toBe(true);
    expect(hasClientReads("finance")).toBe(false);
    expect(hasClientReads("leads")).toBe(false);
    expect(clientReadAllowed("current", "current.item.get")).toBe(true);
    expect(clientReadAllowed("current", "current.item.update")).toBe(false);
  });
});
