import type {
  ComposedModule,
  OperationOutcome,
} from "@rheo-stream/web-contract/screen";

/** Only explicit, generated MUTATE/DRAFT form grants cross this boundary. */
export function formAllowed(
  modules: readonly ComposedModule[],
  moduleId: string,
  operation: string,
  enabledIds: readonly string[],
  routedSurfaces: Readonly<Record<string, unknown>>,
): boolean {
  const owner = modules.find((item) => item.id === moduleId);
  return (
    owner !== undefined &&
    enabledIds.includes(moduleId) &&
    Object.hasOwn(routedSurfaces, owner.surface) &&
    operation.startsWith(`${moduleId}.`) &&
    (owner.submitOperations ?? []).includes(operation) &&
    owner.forms.some((form) => form.operation === operation)
  );
}

export const FORM_REFUSED: OperationOutcome = {
  state: "refused",
  code: "operation_not_permitted",
  text: "This form is no longer available. Reload the page and try again.",
};
