import type { OperationOutcome } from "@rheo-stream/web-contract/screen";
import { cookies, headers } from "next/headers";
import { cache } from "react";

import type { components } from "@/generated/api-types";
import { requestHost } from "@/lib/request";
import {
  INTERNAL_OPERATIONS_PATH,
  INTERNAL_SECRET_HEADER,
  internalApiCredentials,
} from "@/lib/routing/load";
import {
  fetchSession,
  HOST_HEADER,
  SESSION_COOKIE,
  SESSION_HEADER,
  type SessionResult,
} from "@/lib/session";

/**
 * The web tier's operation client: `POST /internal/v1/operations/<name>` on the
 * internal listener, for the session the browser presented.
 *
 * Mirrors `routing/load.ts` and `session.ts`: one `try`, a narrowing guard, a single
 * failure sentinel (`unavailable`), and it never throws. The listener answers with
 * the operation envelope `api_routes.envelope` builds for both listeners:
 * `{state, operation_id, result}` when the operation succeeded, or
 * `{state, operation_id, error: {error_code, error_text}}` when it was refused. A
 * refusal arrives on a non-2xx status (401, 403, 404, 422 or 400) with that same
 * body, so the body is read whatever the status. Only `succeeded` is `ok`, with its
 * `result`, or `null` when the envelope carries no `result` key: `envelope` omits
 * the key for an operation that succeeded with no result, and that is still a
 * success. A `pending` answer is not `ok`: no read this client serves is
 * long-running, and a queued result is not a result. A body that is neither shape,
 * including the listener's own 401 for a wrong `X-Rheo-Internal`, is `unavailable`.
 */

export interface RequestIdentity {
  /** The `rheo_session` cookie's value, forwarded as `X-Rheo-Session`. */
  sessionSecret: string | undefined;
  /** The host the browser asked for, forwarded as `X-Rheo-Host`. */
  host: string | undefined;
}

/** The refusal the listener itself answers when either session header is absent. */
const SESSION_MISSING = "session_missing";
const SUCCEEDED = "succeeded";

export const WORKSPACE_STATUS = "core.workspace.status";
const MODULE_ENABLED = "enabled";

export type WorkspaceStatus = components["schemas"]["WorkspaceStatus"];
type ModuleStatus = components["schemas"]["ModuleStatus"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrow an envelope to the outcome a screen reads. */
export function outcomeOf(body: unknown): OperationOutcome {
  if (!isRecord(body) || typeof body.state !== "string") {
    return { state: "unavailable" };
  }
  const error = body.error;
  if (isRecord(error)) {
    if (typeof error.error_code !== "string") {
      return { state: "unavailable" };
    }
    const text = typeof error.error_text === "string" ? error.error_text : "";
    return { state: "refused", code: error.error_code, text };
  }
  if (body.state === SUCCEEDED) {
    return { state: "ok", result: "result" in body ? body.result : null };
  }
  return { state: "unavailable" };
}

/** Dispatch `name` with `input` as the session in `identity`. Never throws. */
export async function callOperation(
  name: string,
  input: unknown,
  identity: RequestIdentity,
): Promise<OperationOutcome> {
  const credentials = internalApiCredentials();
  if (credentials === null) {
    return { state: "unavailable" };
  }
  if (!identity.sessionSecret || !identity.host) {
    // The listener's own answer for the same inputs, without the round trip.
    return { state: "refused", code: SESSION_MISSING, text: "" };
  }
  try {
    const response = await fetch(
      `${credentials.baseUrl}${INTERNAL_OPERATIONS_PATH}/${encodeURIComponent(name)}`,
      {
        method: "POST",
        cache: "no-store",
        headers: {
          "content-type": "application/json",
          [INTERNAL_SECRET_HEADER]: credentials.secret,
          [SESSION_HEADER]: identity.sessionSecret,
          [HOST_HEADER]: identity.host,
        },
        body: JSON.stringify(input ?? {}),
      },
    );
    const body: unknown = await response.json();
    return outcomeOf(body);
  } catch {
    return { state: "unavailable" };
  }
}

function isModuleStatus(value: unknown): value is ModuleStatus {
  return (
    isRecord(value) &&
    typeof value.module_id === "string" &&
    typeof value.package_version === "string" &&
    typeof value.state === "string" &&
    (value.schema_version === null || typeof value.schema_version === "string")
  );
}

/** Narrow a `core.workspace.status` result. */
export function isWorkspaceStatus(value: unknown): value is WorkspaceStatus {
  return (
    isRecord(value) &&
    typeof value.core_version === "string" &&
    typeof value.core_contract_version === "number" &&
    Array.isArray(value.modules) &&
    value.modules.every(isModuleStatus)
  );
}

/** The ids of the modules this workspace has enabled. */
export function enabledModuleIds(status: WorkspaceStatus): string[] {
  return status.modules
    .filter((module) => module.state === MODULE_ENABLED)
    .map((module) => module.module_id);
}

export type WorkspaceStatusResult =
  | { state: "ok"; status: WorkspaceStatus }
  | { state: "unavailable" };

/**
 * The request's cookie and host, read once per request.
 *
 * `React.cache` scopes each of the three readers below to one server request, so
 * the frame and the screen it wraps share one session read and one
 * `core.workspace.status` read (spec Technical Risk 8). Outside a server request
 * `cache` does not memoise, which is what the tests rely on.
 */
export const requestIdentity = cache(async (): Promise<RequestIdentity> => {
  const [cookieStore, headerList] = await Promise.all([cookies(), headers()]);
  return {
    sessionSecret: cookieStore.get(SESSION_COOKIE)?.value,
    host: requestHost(headerList),
  };
});

export const currentSession = cache(
  async (): Promise<SessionResult> => fetchSession(await requestIdentity()),
);

export const currentWorkspaceStatus = cache(
  async (): Promise<WorkspaceStatusResult> => {
    const outcome = await callOperation(WORKSPACE_STATUS, {}, await requestIdentity());
    if (outcome.state === "ok" && isWorkspaceStatus(outcome.result)) {
      return { state: "ok", status: outcome.result };
    }
    return { state: "unavailable" };
  },
);
