import { cookies, headers } from "next/headers";
import type { ReactNode } from "react";

import { fetchCoreHealth, type CoreHealthResult } from "@/lib/core-health";
import { loginHref, logoutAction, switcherAction } from "@/lib/routing/links";
import { loadRoutingConfig } from "@/lib/routing/load";
import { fetchSession, SESSION_COOKIE } from "@/lib/session";
import panel from "@/shell/panel.module.css";
import { ShellFrame, type ShellAccount } from "@/shell/ShellFrame";

// Never prerender this route: reading `core`'s /healthz is request-time work, and
// this line — not the fetchCoreHealth try/catch — is what keeps `next build` green
// with no `core` process running (the page body never executes at build time).
export const dynamic = "force-dynamic";

/**
 * The shell.
 *
 * Three independent seams, each with its own explicit failure state, none of which
 * may render blank: `core`'s `/healthz` (0a's, on the **public** listener via
 * `RHEO_CORE_INTERNAL_URL` — untouched here), the routing configuration, and the
 * session. They fail separately on purpose: an unreachable internal listener still
 * leaves the health banner readable, and an unreachable `core` still leaves the
 * page rendering.
 *
 * The session read is the authoritative one. `middleware.ts` only looks for the
 * cookie's presence, so a stale cookie arrives here — and `fetchSession` resolves
 * it to "unauthenticated", which is what this page renders. Nothing about an
 * account is shown on any path but `state === "ok"`.
 */
export default async function Page() {
  const baseUrl = process.env.RHEO_CORE_INTERNAL_URL;
  const health: CoreHealthResult = baseUrl
    ? await fetchCoreHealth(baseUrl)
    : { status: "unavailable" };

  const [cookieStore, headerList, routing] = await Promise.all([
    cookies(),
    headers(),
    loadRoutingConfig(),
  ]);
  const session = await fetchSession({
    sessionSecret: cookieStore.get(SESSION_COOKIE)?.value,
    host: headerList.get("x-forwarded-host") ?? headerList.get("host") ?? undefined,
  });

  // The same guard order as ever: routing first, then the session. The account
  // controls exist only on the last, signed-in branch; `ShellFrame` renders the
  // core-status line (`health`) on every path, independently of both.
  let status: ReactNode;
  let account: ShellAccount | null = null;
  if (routing.state !== "ok") {
    status = <p className={panel.line}>routing: unavailable</p>;
  } else if (session.state === "unavailable") {
    status = <p className={panel.line}>session: unavailable</p>;
  } else if (session.state === "unauthenticated") {
    status = (
      <>
        <p className={panel.line}>session: signed out ({session.refusal})</p>
        <a className={panel.action} href={loginHref(routing.config)}>
          Sign in
        </a>
      </>
    );
  } else {
    status = (
      <>
        <p className={panel.line}>
          account: {session.actor.kind} {session.actor.id}
        </p>
        <p className={panel.line}>
          workspace: {session.activeWorkspaceId} ({session.role})
        </p>
      </>
    );
    account = {
      memberships: session.memberships,
      activeWorkspaceId: session.activeWorkspaceId,
      switcherAction: switcherAction(routing.config),
      logoutAction: logoutAction(routing.config),
    };
  }

  return (
    <ShellFrame health={health} account={account}>
      <section className={panel.panel}>
        <h1 className={panel.heading}>Home</h1>
        {status}
      </section>
    </ShellFrame>
  );
}
