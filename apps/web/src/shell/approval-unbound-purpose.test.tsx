import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalPreview, ExternalAction } from "@/shell/approval-contract";

// Core stores the held caller's bound purpose and leaves it null for an unbound
// caller (a web session, an unbound token, an operator). REAL_PENDING is a real
// `core.approval.preview` result from a disposable local Prompt 14 seed, every
// field kept as returned except the identifiers, which are replaced with
// synthetic ones.
const REAL_PENDING = {
  status: "pending",
  actionable: true,
  refusable: false,
  approval_id: "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a41",
  operation_id: "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a42",
  external_action_id: "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a43",
  operation_name: "current.item.create_external",
  requester_kind: "account",
  requester_id: "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a44",
  subject_ref: null,
  destination_ref: "current",
  payload: {
    destination_ref: "current",
    source_ref: "leads.handoff:0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a45",
    title: "Prepare Northwind onboarding workshop",
    body: "Confirm agenda, facilitators, and access requirements from the qualified opportunity.",
    priority: "normal",
    area: "client_delivery",
    due_on: null,
    idempotency_key: "leads.handoff:0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a45#1",
  },
  payload_digest: "6f63fca49d23d0ec5391dae24197ad91cae6cac19990ce5a022ca981b454a02e",
  purpose: null,
  window_start: "2026-10-30T22:26:17.546143Z",
  window_end: "2026-10-30T22:41:17.546143Z",
  durable_state: "pending",
} satisfies ApprovalPreview;

const ID = REAL_PENDING.approval_id;
const DIGEST = REAL_PENDING.payload_digest;
const WORKSPACE = "workspace-synthetic";

const seams = vi.hoisted(() => ({
  preview: null as unknown,
  action: null as unknown,
  calls: [] as Array<{ name: string; input: unknown }>,
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
  callOperation: async (name: string, input: unknown) => {
    seams.calls.push({ name, input });
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
const { approveApproval } = await import("./approval-actions");

function succeeded(): ExternalAction {
  return {
    approval_id: ID,
    attempts: 1,
    created_at: "2026-10-30T22:26:17.546143Z",
    destination_ref: "current",
    error_code: null,
    error_detail: null,
    execution_job_id: null,
    external_action_id: REAL_PENDING.external_action_id,
    failure_class: null,
    operation_id: REAL_PENDING.operation_id,
    operation_name: "current.item.create_external",
    restage_eligible: false,
    result_ref: "current.item:0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a46",
    source_ref: REAL_PENDING.payload.source_ref,
    state: "succeeded",
    subject_ref: null,
    terminal_at: "2026-10-30T22:26:20Z",
    updated_at: "2026-10-30T22:26:20Z",
  };
}

async function render(): Promise<string> {
  return renderToStaticMarkup(
    await ApprovalDetail({ approvalId: ID, workspaceId: WORKSPACE, timeZone: "UTC" }),
  );
}

beforeEach(() => {
  seams.preview = { ...REAL_PENDING };
  seams.action = succeeded();
  seams.calls.length = 0;
});

describe("approvals with no purpose binding", () => {
  it("shows the real pending handoff approval with its payload and both decision forms", async () => {
    const html = await render();
    expect(html).toContain("Review Current handoff");
    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain(DIGEST);
    expect(html).toContain("No purpose binding");
    expect(html).toContain("No held subject");
    expect((html.match(/<form/g) ?? [])).toHaveLength(2);
    expect(html).not.toContain("Approval unavailable");
  });

  it("approves it only through the fresh bound preview and digest", async () => {
    await expect(
      approveApproval({ approvalId: ID, workspaceId: WORKSPACE, payloadDigest: DIGEST }, { state: "idle", message: "" }, new FormData()),
    ).resolves.toEqual({ state: "idle", message: "" });
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview", "core.approval.approve"]);
  });

  it("shows the resolved handoff approval as read-only history", async () => {
    seams.preview = { ...REAL_PENDING, status: "resolved", actionable: false, durable_state: "executed" };
    const html = await render();
    expect(html).toContain("Current item created");
    expect(html).toContain("Read-only approval history");
    expect(html).toContain("No purpose binding");
    expect(html).not.toContain("<form");
  });

  it("accepts a null purpose on an approval that is not an external action", async () => {
    seams.preview = {
      ...REAL_PENDING,
      external_action_id: null,
      subject_ref: "leads.opportunity:0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a47",
    };
    const html = await render();
    expect(html).toContain("Review Current handoff");
    expect(html).toContain("No purpose binding");
    expect((html.match(/<form/g) ?? [])).toHaveLength(2);
  });

  it("still fails closed when the purpose is present but not a string", async () => {
    seams.preview = { ...REAL_PENDING, purpose: 7 };
    const html = await render();
    expect(html).toContain("Approval unavailable");
    expect(html).not.toContain("<form");
  });

  it("still fails closed when another required field is null", async () => {
    seams.preview = { ...REAL_PENDING, payload_digest: null };
    const html = await render();
    expect(html).toContain("Approval unavailable");
    expect(html).not.toContain("Northwind");
    expect(html).not.toContain("<form");
  });
});
