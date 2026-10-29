import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalPreview, ExternalAction } from "@/shell/approval-contract";

const seams = vi.hoisted(() => ({
  preview: null as unknown,
  previewState: "ok" as "ok" | "refused" | "unavailable",
  action: null as unknown,
  calls: [] as Array<{ name: string; input: unknown; workspace: string | undefined }>,
}));

vi.mock("next/cache", () => ({ refresh: () => undefined }));
vi.mock("@/lib/operations", () => ({
  requestIdentity: async () => ({ sessionSecret: "session", host: "shell.example.test" }),
  currentSession: async () => ({ state: "unavailable" }),
  callOperation: async (name: string, input: unknown, _identity: unknown, workspace?: string) => {
    seams.calls.push({ name, input, workspace });
    if (name === "core.external_action.get") {
      return { state: "ok", result: seams.action };
    }
    if (seams.previewState === "unavailable") {
      return { state: "unavailable" };
    }
    if (seams.previewState === "refused") {
      return { state: "refused", code: "not_found", text: "" };
    }
    return { state: "ok", result: seams.preview };
  },
}));

const { ApprovalDetail } = await import("./approval-detail");

const ID = "123e4567-e89b-12d3-a456-426614174000";
const OPERATION_ID = "223e4567-e89b-12d3-a456-426614174000";
const ACTION_ID = "323e4567-e89b-12d3-a456-426614174000";
const DIGEST = "a".repeat(64);

function preview(overrides: Partial<ApprovalPreview> = {}): ApprovalPreview {
  return {
    status: "pending",
    actionable: true,
    refusable: false,
    approval_id: ID,
    operation_id: OPERATION_ID,
    external_action_id: ACTION_ID,
    operation_name: "current.item.create_external",
    requester_kind: "account",
    requester_id: "423e4567-e89b-12d3-a456-426614174000",
    subject_ref: "leads:opportunity:synthetic",
    destination_ref: "current",
    payload: {
      destination_ref: "current",
      source_ref: "leads:opportunity:synthetic",
      title: "Prepare Northwind onboarding workshop",
      body: "Confirm agenda, facilitators,\nand access requirements.",
      priority: "normal",
      area: "Client delivery",
      due_on: null,
      idempotency_key: "synthetic-key",
      unfamiliar_field: { complete: true },
    },
    payload_digest: DIGEST,
    purpose: "Create one standalone Current item",
    window_start: "2026-10-28T10:00:00Z",
    window_end: "2026-10-28T10:15:00Z",
    durable_state: "pending",
    ...overrides,
  };
}

function action(overrides: Partial<ExternalAction> = {}): ExternalAction {
  return {
    approval_id: ID,
    attempts: 0,
    created_at: "2026-10-28T10:00:00Z",
    destination_ref: "current",
    error_code: null,
    error_detail: null,
    execution_job_id: null,
    external_action_id: ACTION_ID,
    failure_class: null,
    operation_id: OPERATION_ID,
    operation_name: "current.item.create_external",
    restage_eligible: false,
    result_ref: null,
    source_ref: "leads:opportunity:synthetic",
    state: "pending",
    subject_ref: "leads:opportunity:synthetic",
    terminal_at: null,
    updated_at: "2026-10-28T10:00:00Z",
    ...overrides,
  };
}

async function render(id: string | undefined = ID): Promise<string> {
  return renderToStaticMarkup(
    await ApprovalDetail({ approvalId: id, workspaceId: "workspace-synthetic", timeZone: "UTC" }),
  );
}

function redacted(refusable = false): ApprovalPreview {
  return preview({
    status: "unavailable",
    actionable: false,
    refusable,
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

beforeEach(() => {
  seams.preview = preview();
  seams.previewState = "ok";
  seams.action = action();
  seams.calls.length = 0;
});

describe("ApprovalDetail", () => {
  it("does not call Core or render a form for a non-UUID query", async () => {
    const html = await render("https://evil.test/approval");
    expect(html).toContain("Approval unavailable");
    expect(html).not.toContain("<form");
    expect(seams.calls).toEqual([]);
  });

  it("renders refused, invalid, missing and unauthorized answers as one redacted state", async () => {
    seams.previewState = "refused";
    const refused = await render();
    seams.previewState = "ok";
    seams.preview = preview({ status: "invalid", actionable: false, refusable: false });
    const invalid = await render();
    seams.preview = redacted(false);
    const unavailable = await render();

    expect(new Set([refused, invalid, unavailable]).size).toBe(1);
    for (const html of [refused, invalid, unavailable]) {
      expect(html).toContain("Approval unavailable");
      expect(html).not.toContain("Northwind");
      expect(html).not.toContain(DIGEST);
      expect(html).not.toContain("destination_ref");
      expect(html).not.toContain("<form");
    }
  });

  it("keeps transport failure distinct but carries no stale controls", async () => {
    seams.previewState = "unavailable";
    const html = await render();
    expect(html).toContain("Approval temporarily unavailable");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("Northwind");
  });

  it("renders only a content-free refusal form for server-authorized refusable state", async () => {
    seams.preview = redacted(true);
    const html = await render();
    expect(html).toContain("Close restored approval");
    expect(html).toContain(">Refuse</button>");
    expect(html).not.toContain("Approve handoff");
    expect(html).not.toContain("destination_ref");
    expect(html).not.toContain(DIGEST);
    expect((html.match(/<form/g) ?? [])).toHaveLength(1);
  });

  it("renders the complete verified payload before two separate fixed decision forms", async () => {
    const html = await render();
    expect(html).toContain("Review Current handoff");
    expect(html).toContain("No action happens until you choose.");
    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain("Confirm agenda, facilitators,\nand access requirements.");
    expect(html).toContain("Due date (due_on)");
    expect(html).toContain("Not set");
    expect(html).toContain("unfamiliar_field");
    expect(html).toContain(DIGEST);
    expect(html.indexOf("Verified stored payload")).toBeLessThan(html.indexOf("Decision"));
    expect((html.match(/<form/g) ?? [])).toHaveLength(2);
    expect(seams.calls.map((call) => call.name)).toEqual(["core.approval.preview"]);
  });

  it("labels restored dates as historical and still requires a fresh explicit decision", async () => {
    seams.preview = preview({
      status: "restored",
      durable_state: "requires_reapproval",
    });
    const html = await render();
    expect(html).toContain("restored approval needs a fresh decision");
    expect(html).toContain("Historical source window start");
    expect(html).toContain("Approving reopens the execution window now");
    expect(html).not.toContain("Approval window start");
    expect((html.match(/<form/g) ?? [])).toHaveLength(2);
  });

  it("renders expired verified history with no form or submit path", async () => {
    seams.preview = preview({ status: "expired", actionable: false, durable_state: "expired" });
    const html = await render();
    expect(html).toContain("Approval expired");
    expect(html).toContain("Read-only approval history");
    expect(html).toContain("Northwind");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("Approve handoff");
  });

  it("shows queued and unresolved external outcomes without repeat controls", async () => {
    seams.preview = preview({ status: "resolved", actionable: false, durable_state: "approved" });
    seams.action = action({ state: "pending" });
    const queued = await render();
    expect(queued).toContain("Handoff approved and queued");
    expect(queued).toMatch(/<h1[^>]*tabindex="-1"[^>]*>Handoff approved and queued<\/h1>/);
    expect(queued).not.toContain("<form");

    seams.action = action({
      state: "unresolved",
      failure_class: "outcome_unknown",
      error_code: "destination_failed",
      terminal_at: "2026-10-28T10:05:00Z",
    });
    const unresolved = await render();
    expect(unresolved).toContain("Outcome unknown");
    expect(unresolved).toContain('role="alert"');
    expect(unresolved).not.toContain("<form");
    expect(seams.calls).toContainEqual({
      name: "core.external_action.get",
      input: { external_action_id: ACTION_ID },
      workspace: "workspace-synthetic",
    });
  });

  it.each([
    ["executing", "Handoff in progress"],
    ["succeeded", "Current item created"],
    ["failed", "Handoff failed with no effect"],
    ["refused", "Handoff refused"],
    ["cancelled", "Handoff cancelled"],
  ] as const)("renders the %s external outcome as terminal read-only state", async (state, heading) => {
    seams.preview = preview({ status: "resolved", actionable: false, durable_state: "executed" });
    seams.action = action({
      state,
      result_ref: state === "succeeded" ? "current:item:synthetic" : null,
      failure_class:
        state === "failed"
          ? "retryable_no_effect"
          : state === "refused"
            ? "permanent_refusal_no_effect"
            : state === "cancelled"
              ? "cancelled_no_effect"
              : null,
      error_code: ["failed", "refused", "cancelled"].includes(state) ? "synthetic" : null,
      terminal_at: ["executing"].includes(state) ? null : "2026-10-28T10:05:00Z",
    });
    const html = await render();
    expect(html).toContain(heading);
    expect(html).not.toContain("<form");
  });
});
