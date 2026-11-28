"use server";

import type { OperationOutcome } from "@rheo-stream/web-contract/screen";

import {
  callOperation,
  currentSession,
  currentWorkspaceStatus,
  enabledModuleIds,
  requestIdentity,
} from "@/lib/operations";
import { loadRoutingConfig } from "@/lib/routing/load";
import { MODULES } from "@/modules.generated";
import { clientReadAllowed } from "./client-read-gate";
import { SCREEN_CALL_REFUSED, SCREEN_CALL_REFUSED_TEXT } from "./screen-call";

const REFUSED: OperationOutcome = {
  state: "refused",
  code: SCREEN_CALL_REFUSED,
  text: SCREEN_CALL_REFUSED_TEXT,
};

/**
 * `ShellApi.read`: a read a client component makes while someone types (#645).
 *
 * The same checks as a form submit, done fresh on every call (session, the
 * workspace the page was rendered for, the module enabled and routed), then the
 * operation must be on the module's client-read list. Unlike a submit it never
 * refreshes the page: it changes nothing. Core still enforces roles on the read.
 */
export async function readModuleScreen(
  moduleId: string,
  workspaceId: string,
  operation: string,
  input: unknown,
): Promise<OperationOutcome> {
  if (!clientReadAllowed(moduleId, operation)) return { ...REFUSED };
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
    return { ...REFUSED };
  if (session.activeWorkspaceId !== workspaceId)
    return {
      state: "refused",
      code: "workspace_changed",
      text: "Your workspace changed. Reload the page.",
    };
  const owner = MODULES.find((module) => module.id === moduleId);
  if (
    owner === undefined ||
    !enabledModuleIds(workspace.status).includes(moduleId) ||
    !Object.hasOwn(routing.config.surfaces.modules, owner.surface)
  )
    return { ...REFUSED };
  return callOperation(operation, input, await requestIdentity(), workspaceId);
}
