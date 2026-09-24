import type { ComposedModule, Screen } from "@rheo-stream/web-contract/screen";

import { normalizePath } from "@/lib/routing/resolve";

/**
 * The shell's composition runtime: which composed contributions a request may see.
 *
 * A contribution is visible only when all of these hold:
 *
 * 1. it is **composed**, present in the `MODULES` list `rheo web compose` generated;
 * 2. its **surface is routed**, named in the routing configuration's `modules`,
 *    which carries the surfaces this deployment loaded;
 * 3. its module's `id` is **enabled** in this workspace (`core.workspace.status`);
 * 4. for a navigation entry only, the session's **role** is among the entry's
 *    `roles`.
 *
 * Routes ignore role. A route is reachable by anyone who knows its path; the
 * operation the screen calls enforces its own role set (spec § Assumptions), so a
 * route-level role gate here would be a second, disagreeing access model.
 *
 * Both exported functions are pure: no request, no network.
 */

export interface VisibilityOptions {
  /** The routing configuration's `surfaces.modules`, keyed by surface name. */
  routingModules: Readonly<Record<string, unknown>>;
  /** `core.workspace.status`'s enabled module ids. */
  enabledModuleIds: readonly string[];
}

export interface NavigationEntry {
  moduleId: string;
  surface: string;
  id: string;
  label: string;
  /** The entry's path inside its surface. */
  path: string;
}

/** The composed modules that are routed and enabled here (rules 1 to 3). */
export function visibleModules(
  modules: readonly ComposedModule[],
  options: VisibilityOptions,
): ComposedModule[] {
  const enabled = new Set(options.enabledModuleIds);
  return modules.filter(
    (composed) =>
      Object.hasOwn(options.routingModules, composed.surface) && enabled.has(composed.id),
  );
}

/** Every navigation entry the session may see, in module then declaration order. */
export function composeNavigation(
  modules: readonly ComposedModule[],
  options: VisibilityOptions & { role: string },
): NavigationEntry[] {
  return visibleModules(modules, options).flatMap((composed) =>
    composed.navigation
      .filter((entry) => entry.roles.includes(options.role))
      .map((entry) => ({
        moduleId: composed.id,
        surface: composed.surface,
        id: entry.id,
        label: entry.label,
        path: entry.path,
      })),
  );
}

/**
 * The screen for `subPath` on `surface`, or `undefined`.
 *
 * Pass the modules this request may see (`visibleModules`); given the full
 * `MODULES` it answers for a module the workspace has not enabled.
 */
export function findModuleRoute(
  modules: readonly ComposedModule[],
  surface: string,
  subPath: string,
): Screen | undefined {
  const path = normalizePath(subPath);
  const owner = modules.find((each) => each.surface === surface);
  return owner?.routes.find((route) => normalizePath(route.path) === path)?.screen;
}
