import { cookies, headers } from "next/headers";
import { Suspense } from "react";

import { SpikeNotes } from "@/components/spike-notes";
import { spikePath } from "@/lib/routing/links";
import { loadRoutingConfig } from "@/lib/routing/load";
import type { RoutingConfig } from "@/lib/routing/config";
import { fetchSession, SESSION_COOKIE, type SessionResult } from "@/lib/session";
import { runOperation, succeeded } from "@/lib/spike/client";

/**
 * The spike surface's one page (0v, C4) — deleted at 0c0's branch cut.
 *
 * A throwaway demonstration surface, not a product screen: it exists to put the
 * generated client, a real session and a real cross-workspace switch in contact
 * with running code, and it renders FR 13's four required states — **loading**
 * (the streamed `Suspense` fallback around the note read), **empty** (a workspace
 * with no notes, which is a success), **error** (below), and **populated**.
 *
 * **The error state is driven by `searchParams`, and nothing else in this run
 * reaches it.** The two route handlers redirect back here with `?add=<state>` or
 * `?workspace=<state>`; a value that is not `succeeded`/`selected` renders the
 * error block. The driver is `curl` with a cookie and runs no client JavaScript
 * (finding F8), so a client-side error path would be dead code.
 *
 * **The shipped `WorkspaceSwitcher` is deliberately not rendered**, and this is
 * the one substitution in the file worth stating twice. That component is
 * `"use client"` and `fetch`es the *relative* `switcherAction`, which in this
 * topology resolves to Next.js on :3000 — where no `/auth/*` route exists. It is
 * inert twice over: no client JavaScript runs under the driver, and the action
 * 404s even in a real browser (finding F16). The spike's own form is a plain
 * `<form method="post">` in a server component posting to the spike's own route
 * handler, which forwards server-side (D-CF1).
 *
 * **One form serves both cases.** Before a workspace is selected the session is
 * `unauthenticated` with refusal `workspace_unselected` — `lib/session.ts` already
 * preserves that string, so this page branches on it with no change to that file
 * — and the options come from `RHEO_SPIKE_WORKSPACE_IDS` with a free-text
 * fallback. Once a workspace is selected the same form renders again from the
 * session's own `memberships`. That the refusal itself carries no memberships is
 * finding F9; the driver-supplied list is the workaround, not a fix.
 *
 * Both form actions are built with `spikePath`, which is relative and
 * **mode-independent** — it joins the surface's own prefix in both routing modes,
 * unlike `urlFor`, which drops the prefix in subdomain mode. That matters here
 * because the handlers are Next routes at `/spike/*` on the web origin no matter
 * what the routing mode is (D-5: the module's screen is hand-mounted, not
 * composed).
 */

// Request-time, like the shell: the session, the routing configuration and the
// note list are all read per request, and none may be baked into a build.
export const dynamic = "force-dynamic";

/** The outcome values that are not errors. Everything else renders the error state. */
const ADD_SUCCEEDED = "succeeded";
const WORKSPACE_SELECTED = "selected";

type SearchParams = Record<string, string | string[] | undefined>;

function one(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** The workspace ids the driver seeds for the first-run case (`RHEO_SPIKE_WORKSPACE_IDS`). */
function seededWorkspaceIds(): string[] {
  return (process.env.RHEO_SPIKE_WORKSPACE_IDS ?? "")
    .split(",")
    .map((id) => id.trim())
    .filter((id) => id !== "");
}

/**
 * The workspace form: one `<form method="post">`, both cases.
 *
 * `options` is the session's memberships once there is a session, and the
 * driver's seeded ids before there is one. The free-text field is the fallback
 * for both, so a workspace that is in neither list can still be reached.
 */
function WorkspaceForm({
  action,
  options,
  active,
}: {
  action: string;
  options: { id: string; label: string }[];
  active?: string;
}) {
  return (
    <form method="post" action={action}>
      <fieldset>
        <legend>Active workspace</legend>
        {options.map((option) => (
          <label key={option.id}>
            <input
              type="radio"
              name="target_workspace_id"
              value={option.id}
              defaultChecked={option.id === active}
            />
            {option.label}
          </label>
        ))}
        <label>
          or a workspace id
          <input type="text" name="target_workspace_id" />
        </label>
        <button type="submit">Select workspace</button>
      </fieldset>
    </form>
  );
}

/** The add form: a plain form post, no client JavaScript. */
function AddNoteForm({ action }: { action: string }) {
  return (
    <form method="post" action={action}>
      <label htmlFor="rheo-spike-body">Note</label>
      <input id="rheo-spike-body" type="text" name="body" />
      <button type="submit">Add note</button>
    </form>
  );
}

/**
 * The note list, read through the generated client.
 *
 * Its own async component so the read sits behind the `Suspense` boundary below:
 * that boundary is FR 13's **loading** state, streamed while this awaits, and it
 * is the only one of the four that is not a rendered branch of `Page` itself.
 */
async function NoteList({
  sessionSecret,
  host,
}: {
  sessionSecret: string | undefined;
  host: string | undefined;
}) {
  const outcome = await runOperation({
    name: "spike.note.list",
    input: { limit: 50 },
    sessionSecret,
    host,
  });
  if (!succeeded(outcome)) {
    // Every refusal and `unavailable` alike: the state is shown rather than an
    // empty list, because an empty list is a *success* here (FR 10) and the two
    // must not look the same.
    return <p>notes: {outcome.state}</p>;
  }
  return <SpikeNotes notes={outcome.result.notes} />;
}

/** The error block FR 13 requires, driven entirely by the redirect's own query. */
function OutcomeError({ add, workspace }: { add?: string; workspace?: string }) {
  const failures: string[] = [];
  if (add !== undefined && add !== ADD_SUCCEEDED) {
    failures.push(`add refused: ${add}`);
  }
  if (workspace !== undefined && workspace !== WORKSPACE_SELECTED) {
    failures.push(`workspace refused: ${workspace}`);
  }
  if (failures.length === 0) {
    return null;
  }
  return (
    <section>
      <h2>Something was refused</h2>
      {failures.map((failure) => (
        <p key={failure}>{failure}</p>
      ))}
    </section>
  );
}

function SessionBody({
  config,
  session,
  sessionSecret,
  host,
}: {
  config: RoutingConfig;
  session: SessionResult;
  sessionSecret: string | undefined;
  host: string | undefined;
}) {
  const workspaceAction = spikePath(config, "/workspace");

  if (session.state === "unavailable") {
    return <p>session: unavailable</p>;
  }
  if (session.state === "unauthenticated") {
    if (session.refusal !== "workspace_unselected") {
      return <p>session: signed out ({session.refusal})</p>;
    }
    // D-CF1's first-run case. The refusal carries no memberships (finding F9),
    // so the options come from the driver's own list, with free text as the
    // fallback for anything not in it.
    return (
      <>
        <p>session: no workspace selected</p>
        <WorkspaceForm
          action={workspaceAction}
          options={seededWorkspaceIds().map((id) => ({ id, label: id }))}
        />
      </>
    );
  }
  return (
    <>
      <p>
        account: {session.actor.kind} {session.actor.id}
      </p>
      <p>
        workspace: {session.activeWorkspaceId} ({session.role})
      </p>
      <WorkspaceForm
        action={workspaceAction}
        options={session.memberships.map((membership) => ({
          id: membership.workspace_id,
          label: `${membership.workspace_id} (${membership.role})`,
        }))}
        active={session.activeWorkspaceId}
      />
      <AddNoteForm action={spikePath(config, "/add")} />
      <Suspense fallback={<p>notes: loading…</p>}>
        <NoteList sessionSecret={sessionSecret} host={host} />
      </Suspense>
    </>
  );
}

export default async function Page({
  searchParams,
}: {
  searchParams?: Promise<SearchParams>;
}) {
  const params: SearchParams = searchParams === undefined ? {} : await searchParams;
  const [cookieStore, headerList, routing] = await Promise.all([
    cookies(),
    headers(),
    loadRoutingConfig(),
  ]);
  const sessionSecret = cookieStore.get(SESSION_COOKIE)?.value;
  const host =
    headerList.get("x-forwarded-host") ?? headerList.get("host") ?? undefined;
  const session = await fetchSession({ sessionSecret, host });

  return (
    <main>
      <h1>spike</h1>
      <OutcomeError add={one(params.add)} workspace={one(params.workspace)} />
      {routing.state !== "ok" ? (
        <p>routing: unavailable</p>
      ) : (
        <SessionBody
          config={routing.config}
          session={session}
          sessionSecret={sessionSecret}
          host={host}
        />
      )}
    </main>
  );
}
