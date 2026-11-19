import type { components } from "@/generated/api-types";

export type ApprovalPreview = components["schemas"]["ApprovalPreview"];
export type ExternalAction = components["schemas"]["ExternalActionRecord"];

const STATUSES = new Set([
  "pending",
  "restored",
  "expired",
  "resolved",
  "invalid",
  "unavailable",
]);
const DIGEST = /^[0-9a-f]{64}$/;

const CONTENT_FIELDS = [
  "approval_id",
  "operation_id",
  "external_action_id",
  "operation_name",
  "requester_kind",
  "requester_id",
  "subject_ref",
  "destination_ref",
  "payload",
  "payload_digest",
  "purpose",
  "window_start",
  "window_end",
  "durable_state",
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isJson(value: unknown): boolean {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  ) {
    return true;
  }
  if (Array.isArray(value)) {
    return value.every(isJson);
  }
  return isRecord(value) && Object.values(value).every(isJson);
}

/** Runtime guard at the generated-client boundary. */
export function isApprovalPreview(value: unknown): value is ApprovalPreview {
  if (
    !isRecord(value) ||
    typeof value.status !== "string" ||
    !STATUSES.has(value.status) ||
    typeof value.actionable !== "boolean" ||
    typeof value.refusable !== "boolean"
  ) {
    return false;
  }
  for (const field of CONTENT_FIELDS) {
    if (field === "payload") {
      if (value.payload !== null && (!isRecord(value.payload) || !isJson(value.payload))) {
        return false;
      }
    } else if (!nullableString(value[field])) {
      return false;
    }
  }
  return true;
}

export function isRedactedPreview(preview: ApprovalPreview): boolean {
  return CONTENT_FIELDS.every((field) => preview[field] === null);
}

/**
 * Core publishes `subject_ref: null` for an external action that creates a new
 * record (a Current handoff) rather than acting on an existing one. Only that
 * case may lack a subject; every other field stays required.
 */
function hasHeldSubject(preview: ApprovalPreview): boolean {
  return (
    typeof preview.subject_ref === "string" ||
    (preview.subject_ref === null && typeof preview.external_action_id === "string")
  );
}

function hasCompleteSnapshot(
  preview: ApprovalPreview,
): preview is ApprovalPreview & {
  approval_id: string;
  operation_id: string;
  operation_name: string;
  requester_kind: string;
  subject_ref: string | null;
  destination_ref: string;
  payload: Record<string, components["schemas"]["JsonValue"]>;
  payload_digest: string;
  purpose: string | null;
  window_start: string;
  window_end: string;
  durable_state: string;
} {
  return (
    typeof preview.approval_id === "string" &&
    typeof preview.operation_id === "string" &&
    typeof preview.operation_name === "string" &&
    typeof preview.requester_kind === "string" &&
    hasHeldSubject(preview) &&
    typeof preview.destination_ref === "string" &&
    preview.payload !== null &&
    typeof preview.payload_digest === "string" &&
    DIGEST.test(preview.payload_digest) &&
    // The held caller's bound purpose. Core stores null for an unbound caller (a
    // web session, an unbound token, an operator); `isApprovalPreview` already
    // refused any other non-string value.
    nullableString(preview.purpose) &&
    typeof preview.window_start === "string" &&
    typeof preview.window_end === "string" &&
    typeof preview.durable_state === "string"
  );
}

export function isActionablePreview(preview: ApprovalPreview): boolean {
  return (
    ((preview.status === "pending" && preview.durable_state === "pending") ||
      (preview.status === "restored" && preview.durable_state === "requires_reapproval")) &&
    preview.actionable &&
    !preview.refusable &&
    hasCompleteSnapshot(preview)
  );
}

export function isHistoryPreview(preview: ApprovalPreview): boolean {
  return (
    (preview.status === "expired" || preview.status === "resolved") &&
    (preview.status !== "resolved" ||
      preview.durable_state === "approved" ||
      preview.durable_state === "executed" ||
      preview.durable_state === "refused") &&
    !preview.actionable &&
    !preview.refusable &&
    hasCompleteSnapshot(preview)
  );
}

export function isRefusablePreview(preview: ApprovalPreview): boolean {
  return (
    preview.status === "unavailable" &&
    !preview.actionable &&
    preview.refusable &&
    isRedactedPreview(preview)
  );
}

const ACTION_STATES = new Set([
  "pending",
  "executing",
  "succeeded",
  "failed",
  "refused",
  "unresolved",
  "cancelled",
]);

export function isExternalAction(value: unknown): value is ExternalAction {
  if (!isRecord(value) || typeof value.state !== "string" || !ACTION_STATES.has(value.state)) {
    return false;
  }
  return (
    typeof value.external_action_id === "string" &&
    typeof value.approval_id === "string" &&
    typeof value.operation_id === "string" &&
    typeof value.operation_name === "string" &&
    nullableString(value.subject_ref) &&
    nullableString(value.source_ref) &&
    typeof value.destination_ref === "string" &&
    nullableString(value.failure_class) &&
    typeof value.attempts === "number" &&
    nullableString(value.result_ref) &&
    nullableString(value.error_code) &&
    nullableString(value.error_detail) &&
    nullableString(value.execution_job_id) &&
    typeof value.restage_eligible === "boolean" &&
    typeof value.created_at === "string" &&
    typeof value.updated_at === "string" &&
    nullableString(value.terminal_at)
  );
}
