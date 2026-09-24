import type { ReactNode } from "react";

export interface ScreenProps {
  shell: ShellApi;
  query: Readonly<Record<string, string | undefined>>;
}

export interface ShellApi {
  role: string;
  call(operation: string, input: unknown): Promise<OperationOutcome>;
  href(routeId: string, query?: Record<string, string | undefined>): string;
}

export type OperationOutcome =
  | { state: "ok"; result: unknown }
  | { state: "refused"; code: string; text: string }
  | { state: "unavailable" };

/**
 * A route's rendering entry point: the composite the manifest's `screen` identifier
 * resolves to via the generated file's property access. A module exports one of
 * these per route; internally it may (and, per spec's screen design, should) be
 * built from a separately-testable `load(shell, query)` data loader plus a
 * synchronous `View(state)` — but the value the generated `MODULES` array and
 * `ComposedModule.routes[].screen` actually hold is this single async function.
 */
export type Screen = (props: ScreenProps) => Promise<ReactNode>;

/**
 * One module's composed web contribution, the element type of the generated
 * `MODULES` array (`rheo web compose`). `routes[].screen` is `Screen`, not
 * `unknown`, so `tsc` refuses the generated file when a module's `screens`
 * export drifts from what a route declares.
 *
 * The module's identifier is `id`, the key the generator
 * (`apps/cli/src/rheo_app_cli/web_compose.py`, pinned by
 * `tests/test_web_compose.py`'s golden output) writes. The committed
 * `apps/web/src/modules.generated.ts` holds that shape against this type, with the
 * real module packages' real exports.
 */
export interface ComposedModule {
  id: string;
  surface: string;
  navigation: readonly { id: string; label: string; path: string; roles: readonly string[] }[];
  routes: readonly { id: string; path: string; screen: Screen }[];
  recordViews: readonly { recordType: string; component: unknown }[];
  forms: readonly { operation: string; component: unknown }[];
  searchProviders: readonly { id: string; operation: string }[];
}
