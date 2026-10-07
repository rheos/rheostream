import { beforeEach, describe, expect, it, vi } from "vitest";
const seams = vi.hoisted(() => ({
  session: {
    state: "ok",
    activeWorkspaceId: "workspace",
    role: "owner",
  } as Record<string, unknown>,
  enabled: ["leads"],
  outcome: { state: "ok", result: {} } as Record<string, unknown>,
  call: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("next/cache", () => ({ refresh: seams.refresh }));
vi.mock("@/lib/operations", () => ({
  currentSession: async () => seams.session,
  currentWorkspaceStatus: async () => ({ state: "ok", status: {} }),
  enabledModuleIds: () => seams.enabled,
  requestIdentity: async () => ({
    sessionSecret: "synthetic",
    host: "example.test",
  }),
  callOperation: async (...args: unknown[]) => {
    seams.call(...args);
    return seams.outcome;
  },
}));
vi.mock("@/lib/routing/load", () => ({
  loadRoutingConfig: async () => ({
    state: "ok",
    config: { surfaces: { modules: { leads: {} } } },
  }),
}));
import { submitModuleForm } from "./submit-form";
beforeEach(() => {
  seams.session = {
    state: "ok",
    activeWorkspaceId: "workspace",
    role: "owner",
  };
  seams.enabled = ["leads"];
  seams.outcome = { state: "ok", result: {} };
  seams.call.mockClear();
  seams.refresh.mockClear();
});
describe("module form server action", () => {
  it("dispatches a declared form using the fresh request identity", async () => {
    const result = await submitModuleForm(
      "leads",
      "workspace",
      "leads.opportunity.add_note",
      { ref: "r", revision: 3, body: "Synthetic note" },
    );
    expect(result.state).toBe("ok");
    expect(seams.call).toHaveBeenCalledWith(
      "leads.opportunity.add_note",
      { ref: "r", revision: 3, body: "Synthetic note" },
      { sessionSecret: "synthetic", host: "example.test" },
      "workspace",
    );
    expect(seams.refresh).toHaveBeenCalledOnce();
  });
  it("refuses an old tab after switching workspace", async () => {
    seams.session.activeWorkspaceId = "another";
    const result = await submitModuleForm(
      "leads",
      "workspace",
      "leads.intake.capture",
      {},
    );
    expect(result).toMatchObject({
      state: "refused",
      code: "workspace_changed",
    });
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("refuses revoked sessions and disabled modules", async () => {
    seams.session = { state: "signed-out" };
    await submitModuleForm("leads", "workspace", "leads.intake.capture", {});
    expect(seams.call).not.toHaveBeenCalled();
    seams.session = { state: "ok", activeWorkspaceId: "workspace" };
    seams.enabled = [];
    await submitModuleForm("leads", "workspace", "leads.intake.capture", {});
    expect(seams.call).not.toHaveBeenCalled();
  });
  it("preserves a core role or stale-record refusal without claiming success", async () => {
    seams.outcome = { state: "refused", code: "record_stale", text: "Reload" };
    const result = await submitModuleForm(
      "leads",
      "workspace",
      "leads.opportunity.update",
      {},
    );
    expect(result).toEqual(seams.outcome);
    expect(seams.refresh).not.toHaveBeenCalled();
  });
  it.each([
    "core.record.delete",
    "leads.intake.accept_delivery",
    "leads.opportunity.get",
  ])("does not forward %s", async (operation) => {
    await submitModuleForm("leads", "workspace", operation, {});
    expect(seams.call).not.toHaveBeenCalled();
  });
});
