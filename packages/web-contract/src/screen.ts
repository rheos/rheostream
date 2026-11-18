import type { ReactNode } from "react";

export interface ScreenProps {
  shell: ShellApi;
  query: Readonly<Record<string, string | undefined>>;
}

export interface ShellApi {
  role: string;
  /** Server action for explicitly declared MUTATE/DRAFT forms; absent in read-only hosts. */
  submit?: (operation: string, input: unknown) => Promise<OperationOutcome>;
  call(operation: string, input: unknown): Promise<OperationOutcome>;
  /** Server action that renders the current screen again with fresh data, in
   * place: no page load, so scroll, focus and client state survive. Absent in
   * hosts that cannot; a screen then falls back to reloading the page. */
  refresh?: () => Promise<void>;
  href(routeId: string, query?: Record<string, string | undefined>): string;
  /** Resolve a provider-published route for one canonical record reference. */
  recordHref?: (ref: string) => string | null;
  /** Resolve the shell-owned review route for one approval id. */
  approvalHref?: (approvalId: string) => string | null;
  /** The viewer's IANA time zone (#465), for formatting moments. Absent in hosts
   * that do not know it; a screen then formats in UTC and labels it so. */
  timeZone?: string;
  /** A preference this module saved with `savePreference` (a cookie, see
   * `preferenceCookie`), so a server render can start from it instead of
   * redirecting after the first paint (#369). The shell reads only the owning
   * module's own preference cookies. Absent in hosts that cannot read them; a
   * screen then falls back to whatever the browser applies. */
  preference?: (name: string) => string | undefined;
  /** Server action that saves one of this module's preferences for
   * `preference` to read on later renders. Absent in hosts that cannot. */
  savePreference?: (name: string, value: string) => Promise<void>;
}

/** A preference name: short, lower case, letters, digits and hyphens. */
export const PREFERENCE_NAME = /^[a-z0-9-]{1,40}$/;

/** The cookie a module's preference lives in, or null for a name that is not
 * one. Scoped by module id, so one module never reads another's. */
export function preferenceCookie(moduleId: string, name: string): string | null {
  return PREFERENCE_NAME.test(moduleId) && PREFERENCE_NAME.test(name)
    ? `rheo-pref-${moduleId}-${name}`
    : null;
}

/**
 * What `ShellApi.call` answers.
 *
 * - `ok`: the operation succeeded. `result` is its result, or `null` when it
 *   succeeded with none (the core envelope then omits `result`, and that is still a
 *   success, never `unavailable`).
 * - `refused`: the operation, or the session, refused; `code` is the envelope's
 *   `error_code` and `text` its `error_text` (`""` when it has none).
 * - `unavailable`: no readable answer, whatever the reason (network, a body that is
 *   not an envelope, an answer still pending).
 */
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
  recordRoutes: readonly { recordType: string; routeId: string; refQuery: string }[];
  recordViews: readonly { recordType: string; component: unknown }[];
  forms: readonly { operation: string; component: unknown }[];
  searchProviders: readonly { id: string; operation: string }[];
  /**
   * The only operations this module's screens may call through `ShellApi.call`:
   * the module's own READ-class declarations, written by `rheo web compose` from
   * the manifest (#122). The shell refuses any other name before it leaves the web
   * tier; core still enforces roles and token sets on every call that does.
   */
  readOperations: readonly string[];
  submitOperations?: readonly string[];
}
