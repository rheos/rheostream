import type { ComposedModule, ShellApi } from "@rheo-stream/web-contract/screen";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { fetchCoreHealth, type CoreHealthResult } from "@/lib/core-health";
import {
  callOperation,
  currentSession,
  currentWorkspaceStatus,
  enabledModuleIds,
  requestIdentity,
  type RequestIdentity,
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
 * `resolveRequest` names the surface. The shell renders its home panel; a module
 * surface renders the matched route's screen inside the same frame; anything else,
 * including a module that is not composed, not routed or not enabled here, is
 * `notFound()`, and those cases are deliberately indistinguishable.
 *
 * Four request-time reads start together: core's `/healthz` (0a's, on the public
 * listener via `RHEO_CORE_INTERNAL_URL`), the routing configuration, the session,
 * and `core.workspace.status`. Each fails on its own and none renders blank. The
 * guard order is routing first, then the session: nothing about an account, and no
 * module screen, is shown on any path but a signed-in session.
 */

export interface SurfaceRequest {
  host: string;
  pathname: string;
  query: Readonly<Record<string, string | undefined>>;
}

type Query = Readonly<Record<string, string | undefined>>;

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

function sessionStatus(config: RoutingConfig, session: SessionResult): ReactNode {
  if (session.state === "unavailable") {
    return <p className={panel.line}>session: unavailable</p>;
  }
  if (session.state === "unauthenticated") {
    return (
      <>
        <p className={panel.line}>session: signed out ({session.refusal})</p>
        <a className={panel.action} href={loginHref(config)}>
          Sign in
        </a>
      </>
    );
  }
  return null;
}

function unavailableHealth(): Promise<CoreHealthResult> {
  return Promise.resolve({ status: "unavailable" });
}

export async function renderSurface(request: SurfaceRequest): Promise<ReactNode> {
  const baseUrl = process.env.RHEO_CORE_INTERNAL_URL;
  const [health, routing, session, workspace, identity] = await Promise.all([
    baseUrl ? fetchCoreHealth(baseUrl) : unavailableHealth(),
    loadRoutingConfig(),
    currentSession(),
    currentWorkspaceStatus(),
    requestIdentity(),
  ]);

  if (routing.state !== "ok") {
    return (
      <ShellFrame health={health}>
        <StatusPanel heading="Home">
          <p className={panel.line}>routing: unavailable</p>
        </StatusPanel>
      </ShellFrame>
    );
  }
  const config = routing.config;
  const resolved = resolveRequest(config, request.host, request.pathname);
  if (resolved.kind === "not-found") {
    notFound();
  }
  const heading = resolved.kind === "shell" ? "Home" : "Workspace";

  if (session.state !== "ok") {
    return (
      <ShellFrame health={health}>
        <StatusPanel heading={heading}>{sessionStatus(config, session)}</StatusPanel>
      </ShellFrame>
    );
  }

  const account: ShellAccount = {
    memberships: session.memberships,
    activeWorkspaceId: session.activeWorkspaceId,
    switcherAction: switcherAction(config),
    logoutAction: logoutAction(config),
  };
  const visibility: VisibilityOptions = {
    routingModules: config.surfaces.modules,
    enabledModuleIds: workspace.state === "ok" ? enabledModuleIds(workspace.status) : [],
  };
  const navigation = navigationItems(config, resolved, { ...visibility, role: session.role });

  if (resolved.kind === "shell") {
    return (
      <ShellFrame health={health} account={account} navigation={navigation}>
        <StatusPanel heading={heading}>
          <p className={panel.line}>
            account: {session.actor.kind} {session.actor.id}
          </p>
          <p className={panel.line}>
            workspace: {session.activeWorkspaceId} ({session.role})
          </p>
          {workspace.state === "ok" ? null : (
            <p className={panel.line}>modules: unavailable</p>
          )}
        </StatusPanel>
      </ShellFrame>
    );
  }

  return (
    <ShellFrame health={health} account={account} navigation={navigation}>
      {await moduleContent(config, resolved, workspace, visibility, {
        role: session.role,
        request,
        identity,
      })}
    </ShellFrame>
  );
}

async function moduleContent(
  config: RoutingConfig,
  resolved: Extract<ResolvedRequest, { kind: "module" }>,
  workspace: WorkspaceStatusResult,
  visibility: VisibilityOptions,
  context: {
    role: string;
    request: SurfaceRequest;
    identity: RequestIdentity;
  },
): Promise<ReactNode> {
  if (workspace.state !== "ok") {
    // Whether the module is enabled here cannot be known, so neither its screen nor
    // a not-found would be honest.
    return (
      <StatusPanel heading="Workspace">
        <p className={panel.line}>modules: unavailable</p>
      </StatusPanel>
    );
  }
  const visible = visibleModules(MODULES, visibility);
  const screen = findModuleRoute(visible, resolved.surface, resolved.subPath);
  const owner = visible.find((each) => each.surface === resolved.surface);
  if (screen === undefined || owner === undefined) {
    notFound();
  }
  const shell: ShellApi = {
    role: context.role,
    call: (operation, input) => callOperation(operation, input, context.identity),
    href: (routeId, query) => moduleHref(config, owner, routeId, query),
  };
  return screen({ shell, query: context.request.query });
}
