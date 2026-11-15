import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalPreview } from "@/shell/approval-contract";

const seams = vi.hoisted(() => ({
  session: {
    state: "ok",
    actor: { kind: "account", id: "account-synthetic" },
    activeWorkspaceId: "workspace-synthetic",
    role: "owner",
    memberships: [],
  } as Awaited<ReturnType<typeof import("@/lib/operations")["currentSession"]>>,
  preview: null as unknown,
  calls: [] as Array<{ name: string; input: unknown; workspace: string | undefined }>,
  refreshes: 0,
}));

vi.mock("next/cache", () => ({
  refresh: () => {
    seams.refreshes += 1;
  },
}));

vi.mock("@/lib/operations", () => ({
  currentSession: async () => seams.session,
  requestIdentity: async () => ({ sessionSecret: "session", host: "shell.example.test" }),
  callOperation: async (name: string, input: unknown, _identity: unknown, workspace?: string) => {
    seams.calls.push({ name, input, workspace });
    return name === "core.approval.preview"
      ? { state: "ok", result: seams.preview }
      : { state: "ok", result: { state: "approved" } };
  },
}));

const { approveApproval, refuseApproval } = await import("./approval-actions");

const ID = "123e4567-e89b-12d3-a456-426614174000";
const DIGEST = "a".repeat(64);
const WORKSPACE = "workspace-synthetic";

function preview(overrides: Partial<ApprovalPreview> = {}): ApprovalPreview {
  return {
    status: "pending",
    actionable: true,
    refusable: false,
    approval_id: ID,
    operation_id: "223e4567-e89b-12d3-a456-426614174000",
    external_action_id: "323e4567-e89b-12d3-a456-426614174000",
    operation_name: "current.item.create_external",
    requester_kind: "account",
    requester_id: "423e4567-e89b-12d3-a456-426614174000",
    subject_ref: "leads:opportunity:synthetic",
    destination_ref: "current",
    payload: { title: "Synthetic handoff" },
    payload_digest: DIGEST,
    purpose: "Create a standalone Current item",
    window_start: "2026-10-28T10:00:00Z",
    window_end: "2026-10-28T10:15:00Z",
    durable_state: "pending",
    ...overrides,
  };
}

const initial = { state: "idle" as const, message: "" };

beforeEach(() => {
  seams.session = {
    state: "ok",
    actor: { kind: "account", id: "account-synthetic" },
    activeWorkspaceId: WORKSPACE,
    role: "owner",
    memberships: [],
  };
  seams.preview = preview();
  seams.calls.length = 0;
  seams.refreshes = 0;
});

describe("fixed approval decisions", () => {
  it("rereads the bound preview, then approves with only the bound id and digest", async () => {
    const forged = new FormData();
    forged.set("operation", "core.owner.grant");
    forged.set("payload_digest", "browser-forgery");
    forged.set("destination", "elsewhere");

    await expect(
      approveApproval(
        { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST },
        initial,
        forged,
      ),
    ).resolves.toEqual(initial);

    expect(seams.calls).toEqual([
      {
        name: "core.approval.preview",
        input: { approval_id: ID },
        workspace: WORKSPACE,
      },
      {
        name: "core.approval.approve",
        input: { approval_id: ID, expected_payload_digest: DIGEST },
        workspace: WORKSPACE,
      },
    ]);
    expect(seams.refreshes).toBe(1);
  });

  it("refuses a stale displayed digest before any mutation", async () => {
    const result = await approveApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: "b".repeat(64) },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });

  it("permits the reviewed option-A restored preview only with its fresh digest", async () => {
    seams.preview = preview({
      status: "restored",
      durable_state: "requires_reapproval",
    });
    await expect(
      approveApproval(
        { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST },
        initial,
        new FormData(),
      ),
    ).resolves.toEqual(initial);
    expect(seams.calls.map((call) => call.name)).toEqual([
      "core.approval.preview",
      "core.approval.approve",
    ]);
  });

  it("refuses when the real session changed workspace", async () => {
    seams.session = {
      state: "ok",
      actor: { kind: "account", id: "account-synthetic" },
      activeWorkspaceId: "workspace-other",
      role: "owner",
      memberships: [],
    };
    const result = await refuseApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(result.message).toContain("active workspace changed");
    expect(seams.calls).toEqual([]);
  });

  it("refuses a stale POST after the approval became read-only", async () => {
    seams.preview = preview({ status: "resolved", actionable: false, durable_state: "approved" });
    const result = await refuseApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });

  it("requires the fresh server preview's refusable capability for digest-free refusal", async () => {
    seams.preview = preview({
      status: "unavailable",
      actionable: false,
      refusable: true,
      approval_id: null,
      operation_id: null,
      external_action_id: null,
      operation_name: null,
      requester_kind: null,
      requester_id: null,
      subject_ref: null,
      destination_ref: null,
      payload: null,
      payload_digest: null,
      purpose: null,
      window_start: null,
      window_end: null,
      durable_state: null,
    });
    const form = new FormData();
    form.set("refusable", "false");
    await expect(
      refuseApproval(
        { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: null },
        initial,
        form,
      ),
    ).resolves.toEqual(initial);
    expect(seams.calls.at(-1)).toEqual({
      name: "core.approval.refuse",
      input: { approval_id: ID, expected_payload_digest: null },
      workspace: WORKSPACE,
    });
  });

  it("never treats a generic unavailable preview or browser field as refusal authority", async () => {
    seams.preview = preview({
      status: "unavailable",
      actionable: false,
      refusable: false,
      approval_id: null,
      operation_id: null,
      external_action_id: null,
      operation_name: null,
      requester_kind: null,
      requester_id: null,
      subject_ref: null,
      destination_ref: null,
      payload: null,
      payload_digest: null,
      purpose: null,
      window_start: null,
      window_end: null,
      durable_state: null,
    });
    const forged = new FormData();
    forged.set("refusable", "true");
    const result = await refuseApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: null },
      initial,
      forged,
    );
    expect(result.state).toBe("error");
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });

  it("rejects a refusable claim that is not completely redacted", async () => {
    seams.preview = { ...redactedPreview(), refusable: true, purpose: "leaked" };
    const result = await refuseApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: null },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });

  it("never permits approve without a bound displayed digest", async () => {
    const result = await approveApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: null },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(seams.calls).toEqual([]);
  });
});

function redactedPreview(): ApprovalPreview {
  return preview({
    status: "unavailable",
    actionable: false,
    refusable: false,
    approval_id: null,
    operation_id: null,
    external_action_id: null,
    operation_name: null,
    requester_kind: null,
    requester_id: null,
    subject_ref: null,
    destination_ref: null,
    payload: null,
    payload_digest: null,
    purpose: null,
    window_start: null,
    window_end: null,
    durable_state: null,
  });
}
