"use server";

import type { OperationOutcome } from "@rheo-stream/web-contract/screen";
import { refresh } from "next/cache";

import {
  callOperation,
  currentSession,
  currentWorkspaceStatus,
  enabledModuleIds,
  requestIdentity,
} from "@/lib/operations";
import { loadRoutingConfig } from "@/lib/routing/load";
import { MODULES } from "@/modules.generated";
import { formAllowed, FORM_REFUSED } from "./form-gate";

/** Next's server-action POST/origin check precedes fresh session and module checks.
 * Bound workspace prevents an old tab writing into a newly selected workspace.
 * Core still verifies current membership, operation role, input and revision.
 */
export async function submitModuleForm(
  moduleId: string,
  workspaceId: string,
  operation: string,
  input: unknown,
): Promise<OperationOutcome> {
  const [session, workspace, routing] = await Promise.all([
    currentSession(),
    currentWorkspaceStatus(),
    loadRoutingConfig(),
  ]);
  if (
    session.state !== "ok" ||
    workspace.state !== "ok" ||
    routing.state !== "ok"
  )
    return FORM_REFUSED;
  if (session.activeWorkspaceId !== workspaceId)
    return {
      state: "refused",
      code: "workspace_changed",
      text: "Your workspace changed. Reload before saving.",
    };
  if (
    !formAllowed(
      MODULES,
      moduleId,
      operation,
      enabledModuleIds(workspace.status),
      routing.config.surfaces.modules,
    )
  )
    return FORM_REFUSED;
  const outcome = await callOperation(
    operation,
    input,
    await requestIdentity(),
    workspaceId,
  );
  if (outcome.state === "ok") refresh();
  return outcome;
}
