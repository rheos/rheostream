import type { OperationOutcome, ShellApi } from "@rheo-stream/web-contract/screen";

/**
 * A recording fake `ShellApi` for loader tests: no network, every call written down in
 * order. An operation with no responder answers `unavailable`, so a loader that calls
 * something unexpected still shows up in `calls` rather than hanging or throwing.
 */

export interface RecordedCall {
  operation: string;
  input: Readonly<Record<string, unknown>>;
}

type Input = Readonly<Record<string, unknown>>;
export type Responder = OperationOutcome | ((input: Input) => OperationOutcome);

export interface FakeShell {
  shell: ShellApi;
  calls: RecordedCall[];
}

/** A test-only href: the route id as a path, then the defined query values. */
export function fakeHref(routeId: string, query?: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) params.set(key, value);
  }
  const search = params.toString();
  return search ? `/${routeId}?${search}` : `/${routeId}`;
}

export function fakeShell(
  role: string,
  responders: Readonly<Record<string, Responder>>,
): FakeShell {
  const calls: RecordedCall[] = [];
  const shell: ShellApi = {
    role,
    href: fakeHref,
    async call(operation, input) {
      const recorded = (input ?? {}) as Input;
      calls.push({ operation, input: recorded });
      const responder = responders[operation];
      if (responder === undefined) return { state: "unavailable" };
      return typeof responder === "function" ? responder(recorded) : responder;
    },
  };
  return { shell, calls };
}
