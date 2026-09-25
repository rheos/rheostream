import type { ShellApi } from "@rheo-stream/web-contract/screen";

import type { Operation } from "./operations";

/**
 * One operation call, narrowed by a guard. A result the guard refuses is treated as
 * `unavailable`: a screen never renders a shape it has not checked, and the generic
 * error state is the honest answer to a response it cannot read.
 *
 * This is the package's only caller of `shell.call`, whose own parameter is any string
 * because the shared contract serves every module. Here `operation` is the read-only
 * `Operation` union, and the package's ESLint config refuses a `.call(` on a shell
 * anywhere else, so tsc and lint together keep every call on the allowlist.
 */
export type Called<T> =
  | { state: "ok"; value: T }
  | { state: "refused"; code: string }
  | { state: "unavailable" };

export async function callChecked<T>(
  shell: ShellApi,
  operation: Operation,
  input: Readonly<Record<string, unknown>>,
  guard: (value: unknown) => value is T,
): Promise<Called<T>> {
  const outcome = await shell.call(operation, input);
  switch (outcome.state) {
    case "ok":
      return guard(outcome.result)
        ? { state: "ok", value: outcome.result }
        : { state: "unavailable" };
    case "refused":
      return { state: "refused", code: outcome.code };
    default:
      return { state: "unavailable" };
  }
}
