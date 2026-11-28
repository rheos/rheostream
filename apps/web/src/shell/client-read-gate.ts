import { MODULES } from "@/modules.generated";

/**
 * Each module's client-readable operations (#645), copied once when this file
 * loads, so code that reached the `MODULES` array cannot widen a later request's
 * gate. An operation must be both one of the module's READ-class declarations and
 * named in its manifest's `web.client_reads`. A module id not here gets none.
 */
const CLIENT_READS: ReadonlyMap<string, ReadonlySet<string>> = new Map(
  MODULES.map((module) => {
    const reads = new Set<string>(module.readOperations);
    const declared: readonly string[] =
      "clientReadOperations" in module ? module.clientReadOperations : [];
    return [module.id, new Set(declared.filter((name) => reads.has(name)))];
  }),
);

/** Whether `moduleId` may read `operation` from a client component. */
export function clientReadAllowed(moduleId: string, operation: unknown): boolean {
  return typeof operation === "string" && (CLIENT_READS.get(moduleId)?.has(operation) ?? false);
}

/** Whether `moduleId` declares any client read, so its screens get `read` at all. */
export function hasClientReads(moduleId: string): boolean {
  return (CLIENT_READS.get(moduleId)?.size ?? 0) > 0;
}
