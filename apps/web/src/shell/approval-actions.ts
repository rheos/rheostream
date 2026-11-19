"use server";

import { refresh } from "next/cache";

import { callOperation, currentSession, requestIdentity } from "@/lib/operations";
import { approvalId } from "@/lib/routing/links";
import {
  isActionablePreview,
  isApprovalPreview,
  isRefusablePreview,
} from "@/shell/approval-contract";

export interface ApprovalDecisionState {
  state: "idle" | "error";
  message: string;
}

export interface ApprovalDecisionBinding {
  approvalId: string;
  workspaceId: string;
  payloadDigest: string | null;
}

const ERROR: ApprovalDecisionState = {
  state: "error",
  message: "The decision was not applied. Reload the approval and review its current state.",
};
const WORKSPACE_CHANGED: ApprovalDecisionState = {
  state: "error",
  message: "Your active workspace changed. Reload this approval before deciding.",
};

async function decide(
  operation: "core.approval.approve" | "core.approval.refuse",
  binding: ApprovalDecisionBinding,
  previous: ApprovalDecisionState,
  formData: FormData,
): Promise<ApprovalDecisionState> {
  // The action protocol supplies these values, but neither is authority. All
  // decision inputs come from the server-bound binding and fresh Core preview.
  void previous;
  void formData;
  const id = approvalId(binding.approvalId);
  if (id === null || binding.workspaceId.length === 0) {
    return ERROR;
  }

  const session = await currentSession();
  if (session.state !== "ok") {
    return ERROR;
  }
  if (session.activeWorkspaceId !== binding.workspaceId) {
    return WORKSPACE_CHANGED;
  }

  const identity = await requestIdentity();
  const previewOutcome = await callOperation(
    "core.approval.preview",
    { approval_id: id },
    identity,
    binding.workspaceId,
  );
  if (previewOutcome.state !== "ok" || !isApprovalPreview(previewOutcome.result)) {
    return ERROR;
  }

  const preview = previewOutcome.result;
  if (binding.payloadDigest === null) {
    if (operation !== "core.approval.refuse" || !isRefusablePreview(preview)) {
      return ERROR;
    }
  } else if (
    !isActionablePreview(preview) ||
    preview.approval_id !== id ||
    preview.payload_digest !== binding.payloadDigest
  ) {
    return ERROR;
  }

  const outcome = await callOperation(
    operation,
    {
      approval_id: id,
      expected_payload_digest: binding.payloadDigest,
    },
    identity,
    binding.workspaceId,
  );
  refresh();
  return outcome.state === "ok" ? { state: "idle", message: "" } : ERROR;
}

export async function approveApproval(
  binding: ApprovalDecisionBinding,
  previous: ApprovalDecisionState,
  formData: FormData,
): Promise<ApprovalDecisionState> {
  if (binding.payloadDigest === null) {
    return ERROR;
  }
  return decide("core.approval.approve", binding, previous, formData);
}

export async function refuseApproval(
  binding: ApprovalDecisionBinding,
  previous: ApprovalDecisionState,
  formData: FormData,
): Promise<ApprovalDecisionState> {
  return decide("core.approval.refuse", binding, previous, formData);
}
