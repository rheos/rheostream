import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalPreview, ExternalAction } from "@/shell/approval-contract";

// A Current handoff approval is subject-less: it creates a new record, so Core's
// `core.approval.preview` publishes `subject_ref: null`. The shape below follows a
// real preview from a local disposable Prompt 14 run, with fictional values.

const seams = vi.hoisted(() => ({
  preview: null as unknown,
  action: null as unknown,
  calls: [] as Array<{ name: string; input: unknown; workspace: string | undefined }>,
}));

vi.mock("next/cache", () => ({ refresh: () => undefined }));
vi.mock("@/lib/operations", () => ({
  requestIdentity: async () => ({ sessionSecret: "session", host: "shell.example.test" }),
  currentSession: async () => ({
    state: "ok",
    actor: { kind: "account", id: "account-synthetic" },
    activeWorkspaceId: "workspace-synthetic",
    role: "owner",
    memberships: [],
  }),
  callOperation: async (name: string, input: unknown, _identity: unknown, workspace?: string) => {
    seams.calls.push({ name, input, workspace });
    if (name === "core.approval.preview") {
      return { state: "ok", result: seams.preview };
    }
    if (name === "core.external_action.get") {
      return { state: "ok", result: seams.action };
    }
    return { state: "ok", result: { state: "approved" } };
  },
}));

const { ApprovalDetail } = await import("./approval-detail");
const { approveApproval, refuseApproval } = await import("./approval-actions");

const ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const ACTION_ID = "1a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const DIGEST = "c".repeat(64);
const WORKSPACE = "workspace-synthetic";

function preview(overrides: Partial<ApprovalPreview> = {}): ApprovalPreview {
  return {
    status: "pending",
    actionable: true,
    refusable: false,
    approval_id: ID,
    operation_id: "2a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    external_action_id: ACTION_ID,
    operation_name: "current.item.create_external",
    requester_kind: "account",
    requester_id: "3a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    subject_ref: null,
    destination_ref: "current",
    payload: {
      destination_ref: "current",
      source_ref: "leads.handoff:4a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
      title: "Prepare Northwind onboarding workshop",
      body: "Confirm agenda, facilitators, and access requirements from the qualified opportunity.",
      priority: "normal",
      area: "client_delivery",
      due_on: null,
      idempotency_key: "leads.handoff:4a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d#1",
    },
    payload_digest: DIGEST,
    // Real handoff approvals carry no purpose binding (see approval-unbound-purpose).
    purpose: null,
    window_start: "2026-10-30T18:37:10Z",
    window_end: "2026-10-30T18:52:10Z",
    durable_state: "pending",
    ...overrides,
  };
}

function action(overrides: Partial<ExternalAction> = {}): ExternalAction {
  return {
    approval_id: ID,
    attempts: 1,
    created_at: "2026-10-30T18:37:10Z",
    destination_ref: "current",
    error_code: null,
    error_detail: null,
    execution_job_id: null,
    external_action_id: ACTION_ID,
    failure_class: null,
    operation_id: "2a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    operation_name: "current.item.create_external",
    restage_eligible: false,
    result_ref: "current.item:5a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    source_ref: "leads.handoff:4a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    state: "succeeded",
    subject_ref: null,
    terminal_at: "2026-10-30T18:37:12Z",
    updated_at: "2026-10-30T18:37:12Z",
    ...overrides,
  };
}

async function render(): Promise<string> {
  return renderToStaticMarkup(
    await ApprovalDetail({ approvalId: ID, workspaceId: WORKSPACE, timeZone: "UTC" }),
  );
}

const initial = { state: "idle" as const, message: "" };

beforeEach(() => {
  seams.preview = preview();
  seams.action = action();
  seams.calls.length = 0;
});

describe("subject-less external approvals", () => {
  it("shows a pending subject-less approval's full payload before both decision forms", async () => {
    const html = await render();
    expect(html).toContain("Review Current handoff");
    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain(
      "Confirm agenda, facilitators, and access requirements from the qualified opportunity.",
    );
    expect(html).toContain(DIGEST);
    expect(html).toContain("No held subject");
    expect(html.indexOf("No held subject")).toBeLessThan(html.indexOf("Decision"));
    expect((html.match(/<form/g) ?? [])).toHaveLength(2);
    expect(html).not.toContain("Approval unavailable");
  });

  it("shows a resolved subject-less approval as read-only history", async () => {
    seams.preview = preview({ status: "resolved", actionable: false, durable_state: "executed" });
    const html = await render();
    expect(html).toContain("Current item created");
    expect(html).toContain("Read-only approval history");
    expect(html).toContain("No held subject");
    expect(html).not.toContain("<form");
  });

  it("still fails closed when a subject-less preview lacks its destination", async () => {
    seams.preview = preview({ destination_ref: null });
    const html = await render();
    expect(html).toContain("Approval unavailable");
    expect(html).not.toContain("Northwind");
    expect(html).not.toContain("<form");
  });

  it("still fails closed for a subject-less preview that is not an external action", async () => {
    seams.preview = preview({ external_action_id: null });
    const html = await render();
    expect(html).toContain("Approval unavailable");
    expect(html).not.toContain("<form");
  });

  it("approves a subject-less approval only through the fresh bound preview and digest", async () => {
    await expect(
      approveApproval({ approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST }, initial, new FormData()),
    ).resolves.toEqual(initial);
    expect(seams.calls.map((call) => [call.name, call.input])).toEqual([
      ["core.approval.preview", { approval_id: ID }],
      ["core.approval.approve", { approval_id: ID, expected_payload_digest: DIGEST }],
    ]);
  });

  it("refuses a subject-less approval with a stale digest before any mutation", async () => {
    const result = await refuseApproval(
      { approvalId: ID, workspaceId: WORKSPACE, payloadDigest: "d".repeat(64) },
      initial,
      new FormData(),
    );
    expect(result.state).toBe("error");
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });
});
