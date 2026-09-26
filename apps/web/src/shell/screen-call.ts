import type { OperationOutcome, ShellApi } from "@rheo-stream/web-contract/screen";

/**
 * The runtime READ-class allowlist on a module screen's `ShellApi.call` (#122).
 *
 * A module's screens are read-only by contract. The module package enforces that
 * with a closed `Operation` union, an ESLint rule and its own tests, but none of
 * those run in the shell, so this check does: a screen may call exactly the names
 * in its composed module's `readOperations`, which `rheo web compose` writes from
 * the module's own READ-class declarations. Anything else, including another
 * module's reads, the core's own operations, and any spelling that is not the
 * exact registered name, is refused here and never reaches core.
 *
 * This is defence in depth. Core still enforces roles and token sets on every call
 * that passes. It does not contain a module package that bypasses `ShellApi`
 * altogether: module web code runs in the web tier's own process.
 */

export const SCREEN_CALL_REFUSED = "operation_not_permitted";
export const SCREEN_CALL_REFUSED_TEXT =
  "a module screen may call only its own module's read operations";

const REFUSED: OperationOutcome = {
  state: "refused",
  code: SCREEN_CALL_REFUSED,
  text: SCREEN_CALL_REFUSED_TEXT,
};

/**
 * `dispatch`, gated to `readOperations`. The set is copied when the gate is built,
 * so a screen that holds the composed module cannot widen it afterwards.
 */
export function screenCall(
  readOperations: readonly string[],
  dispatch: (operation: string, input: unknown) => Promise<OperationOutcome>,
): ShellApi["call"] {
  const permitted: ReadonlySet<unknown> = new Set(readOperations);
  return async (operation, input) => {
    // Exact membership only: a non-string, or any other spelling, is not a member.
    if (!permitted.has(operation)) {
      return { ...REFUSED };
    }
    return dispatch(operation, input);
  };
}
