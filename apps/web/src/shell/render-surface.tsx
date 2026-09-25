import type { ComposedModule, Screen, ShellApi } from "@rheo-stream/web-contract/screen";
import { notFound } from "next/navigation";
import { cache, type ReactNode } from "react";

import { fetchCoreHealth, type CoreHealthResult } from "@/lib/core-health";
import {
  callOperation,
  currentSession,
  currentWorkspaceStatus,
  enabledModuleIds,
  requestIdentity,
  type WorkspaceStatusResult,
} from "@/lib/operations";
import type { RoutingConfig } from "@/lib/routing/config";
import { loginHref, logoutAction, switcherAction } from "@/lib/routing/links";
import { loadRoutingConfig } from "@/lib/routing/load";
import { normalizePath, resolveRequest, type ResolvedRequest } from "@/lib/routing/resolve";
import { urlFor } from "@/lib/routing/url-for";
import type { SessionResult } from "@/lib/session";
import { MODULES } from "@/modules.generated";
import {
  composeNavigation,
  findModuleRoute,
  visibleModules,
  type VisibilityOptions,
} from "@/shell/compose";
import panel from "@/shell/panel.module.css";
import { ShellFrame, type ShellAccount, type ShellNavigationItem } from "@/shell/ShellFrame";

/**
 * The one place a request becomes a page, for both route files: `app/page.tsx`
 * (the bare host) and `app/[...segments]/page.tsx` (everything below it).
 *
 * `decideSurface` makes every decision: which surface, whether it is served here,
 * and what state it is in. `renderSurface` only draws the decision, and the catch-all
 * segment's layout (`ensureServed`) only acts on its `not-found`, so the layout and
 * the page cannot disagree about 404 versus render.
 */

export interface SurfaceRequest {
  host: string;
  pathname: string;
  query: Readonly<Record<string, string | undefined>>;
}

type Query = Readonly<Record<string, string | undefined>>;
type SignedIn = Extract<SessionResult, { state: "ok" }>;
type ModuleRequest = Extract<ResolvedRequest, { kind: "module" }>;

/**
 * Everything a request can come to, in the guard order: routing first, then the
 * path, then the session, then the workspace's modules. Nothing about an account,
 * and no module screen, is reachable on any branch but a signed-in session.
 */
export type SurfaceDecision =
  | { kind: "routing-unavailable" }
  | { kind: "not-found" }
  | {
      kind: "signed-out";
      config: RoutingConfig;
      resolved: Exclude<ResolvedRequest, { kind: "not-found" }>;
      session: Exclude<SessionResult, { state: "ok" }>;
    }
  | {
      kind: "shell";
      config: RoutingConfig;
      resolved: { kind: "shell" };
      session: SignedIn;
      workspace: WorkspaceStatusResult;
      visibility: VisibilityOptions;
    }
  | {
      /** Whether the module is enabled here cannot be known: no screen, no 404. */
      kind: "modules-unavailable";
      config: RoutingConfig;
      resolved: ModuleRequest;
      session: SignedIn;
      visibility: VisibilityOptions;
    }
  | {
      kind: "module";
      config: RoutingConfig;
      resolved: ModuleRequest;
      session: SignedIn;
      visibility: VisibilityOptions;
      owner: ComposedModule;
      screen: Screen;
    };

/** The visible module serving `resolved`, and its matched screen, or `undefined`. */
function matchModule(
  visibility: VisibilityOptions,
  resolved: ModuleRequest,
): { owner: ComposedModule; screen: Screen } | undefined {
  const visible = visibleModules(MODULES, visibility);
  const screen = findModuleRoute(visible, resolved.surface, resolved.subPath);
  const owner = visible.find((each) => each.surface === resolved.surface);
  return screen === undefined || owner === undefined ? undefined : { owner, screen };
}

/**
 * The decision for one request, made once per request.
 *
 * The path is resolved against the routing configuration **before** the session or
 * `core.workspace.status` is read, so a path this application does not serve costs
 * no internal call beyond the routing read, which `loadRoutingConfig` memoises per
 * process. The session and the workspace status are then read together.
 *
 * `React.cache` over the two string arguments makes the layout's call and the
 * page's call one computation in a server request; outside one it does not memoise.
 */
export const decideSurface = cache(
  async (host: string, pathname: string): Promise<SurfaceDecision> => {
    const routing = await loadRoutingConfig();
    if (routing.state !== "ok") {
      return { kind: "routing-unavailable" };
    }
    const config = routing.config;
    const resolved = resolveRequest(config, host, pathname);
    if (resolved.kind === "not-found") {
      return { kind: "not-found" };
    }
    const [session, workspace] = await Promise.all([
      currentSession(),
      currentWorkspaceStatus(),
    ]);
    if (session.state !== "ok") {
      return { kind: "signed-out", config, resolved, session };
    }
    const visibility: VisibilityOptions = {
      routingModules: config.surfaces.modules,
      enabledModuleIds: workspace.state === "ok" ? enabledModuleIds(workspace.status) : [],
    };
    if (resolved.kind === "shell") {
      return { kind: "shell", config, resolved, session, workspace, visibility };
    }
    if (workspace.state !== "ok") {
      return { kind: "modules-unavailable", config, resolved, session, visibility };
    }
    const match = matchModule(visibility, resolved);
    if (match === undefined) {
      return { kind: "not-found" };
    }
    return { kind: "module", config, resolved, session, visibility, ...match };
  },
);

/**
 * `notFound()`, before anything streams, when the decision is `not-found`.
 *
 * `app/[...segments]/layout.tsx` calls this. The segment's `loading.tsx` wraps the
 * page in a Suspense boundary, and a `notFound()` thrown inside one arrives after
 * the 200 status has been sent: the not-found page renders, but as a soft 404. A
 * layout sits outside that boundary, so acting on the decision here makes the
 * status a real 404. Every other decision is left to `renderSurface`.
 */
export async function ensureServed(request: Omit<SurfaceRequest, "query">): Promise<void> {
  const decision = await decideSurface(request.host, request.pathname);
  if (decision.kind === "not-found") {
    notFound();
  }
}

/** A search-params object as Next hands it over, first value per key. */
export function firstValues(
  search: Readonly<Record<string, string | string[] | undefined>>,
): Record<string, string | undefined> {
  const query: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(search)) {
    query[key] = Array.isArray(value) ? value[0] : value;
  }
  return query;
}

/** The catch-all route's segments as the pathname `resolveRequest` reads. */
export function pathFromSegments(segments: readonly string[]): string {
  return `/${segments.join("/")}`;
}

/** A module route's absolute URL, with the defined query values appended. */
function moduleHref(
  config: RoutingConfig,
  owner: ComposedModule,
  routeId: string,
  query?: Query,
): string {
  const route = owner.routes.find((each) => each.id === routeId);
  if (route === undefined) {
    throw new Error(`module '${owner.id}' declares no route '${routeId}'`);
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) {
      params.set(key, value);
    }
  }
  const base = urlFor(config, owner.surface, route.path);
  const search = params.toString();
  return search ? `${base}?${search}` : base;
}

function navigationItems(
  config: RoutingConfig,
  resolved: ResolvedRequest,
  options: VisibilityOptions & { role: string },
): ShellNavigationItem[] {
  return composeNavigation(MODULES, options).map((entry) => ({
    key: `${entry.moduleId}.${entry.id}`,
    label: entry.label,
    href: urlFor(config, entry.surface, entry.path),
    current:
      resolved.kind === "module" &&
      resolved.surface === entry.surface &&
      normalizePath(resolved.subPath) === normalizePath(entry.path),
  }));
}

/** The signed-out, session-unavailable and routing-unavailable panels. */
function StatusPanel({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <section className={panel.panel}>
      <h1 className={panel.heading}>{heading}</h1>
      {children}
    </section>
  );
}

function sessionStatus(
  config: RoutingConfig,
  session: Exclude<SessionResult, { state: "ok" }>,
): ReactNode {
  if (session.state === "unavailable") {
    return <p className={panel.line}>session: unavailable</p>;
  }
  return (
    <>
      <p className={panel.line}>session: signed out ({session.refusal})</p>
      <a className={panel.action} href={loginHref(config)}>
        Sign in
      </a>
    </>
  );
}

function unavailableHealth(): Promise<CoreHealthResult> {
  return Promise.resolve({ status: "unavailable" });
}

function accountOf(config: RoutingConfig, session: SignedIn): ShellAccount {
  return {
    memberships: session.memberships,
    activeWorkspaceId: session.activeWorkspaceId,
    switcherAction: switcherAction(config),
    logoutAction: logoutAction(config),
  };
}

/**
 * Draw the decision. Core's `/healthz` (0a's, on the public listener via
 * `RHEO_CORE_INTERNAL_URL`) is read alongside it and shown on every branch, so an
 * unreachable core still leaves the page rendering and no branch renders blank.
 */
export async function renderSurface(request: SurfaceRequest): Promise<ReactNode> {
  const baseUrl = process.env.RHEO_CORE_INTERNAL_URL;
  const [health, decision] = await Promise.all([
    baseUrl ? fetchCoreHealth(baseUrl) : unavailableHealth(),
    decideSurface(request.host, request.pathname),
  ]);

  switch (decision.kind) {
    case "routing-unavailable":
      return (
        <ShellFrame health={health}>
          <StatusPanel heading="Home">
            <p className={panel.line}>routing: unavailable</p>
          </StatusPanel>
        </ShellFrame>
      );
    case "not-found":
      return notFound();
    case "signed-out":
      return (
        <ShellFrame health={health}>
          <StatusPanel heading={decision.resolved.kind === "shell" ? "Home" : "Workspace"}>
            {sessionStatus(decision.config, decision.session)}
          </StatusPanel>
        </ShellFrame>
      );
  }

  const { config, resolved, session } = decision;
  const account = accountOf(config, session);
  const navigation = navigationItems(config, resolved, {
    ...decision.visibility,
    role: session.role,
  });

  if (decision.kind === "shell") {
    return (
      <ShellFrame health={health} account={account} navigation={navigation}>
        <StatusPanel heading="Home">
          <p className={panel.line}>
            account: {session.actor.kind} {session.actor.id}
          </p>
          <p className={panel.line}>
            workspace: {session.activeWorkspaceId} ({session.role})
          </p>
          {decision.workspace.state === "ok" ? null : (
            <p className={panel.line}>modules: unavailable</p>
          )}
        </StatusPanel>
      </ShellFrame>
    );
  }

  if (decision.kind === "modules-unavailable") {
    return (
      <ShellFrame health={health} account={account} navigation={navigation}>
        <StatusPanel heading="Workspace">
          <p className={panel.line}>modules: unavailable</p>
        </StatusPanel>
      </ShellFrame>
    );
  }

  const identity = await requestIdentity();
  const { owner, screen } = decision;
  const shell: ShellApi = {
    role: session.role,
    call: (operation, input) => callOperation(operation, input, identity),
    href: (routeId, query) => moduleHref(config, owner, routeId, query),
  };
  return (
    <ShellFrame health={health} account={account} navigation={navigation}>
      {await screen({ shell, query: request.query })}
    </ShellFrame>
  );
}
