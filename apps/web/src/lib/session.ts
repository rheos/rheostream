import {
  INTERNAL_SECRET_HEADER,
  INTERNAL_SESSION_PATH,
  internalApiCredentials,
} from "@/lib/routing/load";

/**
 * The internal-API client for `GET /internal/v1/session` (C9).
 *
 * This is the seam where "fail soft" and "fail closed" have to be told apart, and
 * getting them backwards in the same codebase is the trap. `core-health.ts` fails
 * *soft*: an unreachable `core` degrades to a banner, because nothing is being
 * authorised. This function degrades too — it never throws — but the only path to
 * `state: "ok"` is a 200 whose body passes the guard below with its own
 * `state === "ok"`. A refusal, a network error, a 401 from the shared-secret
 * check, a body that does not parse, a body of the wrong shape, or an unset
 * environment variable all resolve to "unauthenticated" or "unavailable" (or, for
 * `workspace_unselected` alone, "no-workspace"). None of them can produce an
 * account.
 *
 * The three request headers are the contract 08 publishes and this file consumes;
 * they are not interchangeable and are not ours to rename.
 */

/** The host-only session cookie the browser holds. Set by `core`, read here. */
export const SESSION_COOKIE = "rheo_session";

/** The session secret, forwarded to the internal listener. */
export const SESSION_HEADER = "X-Rheo-Session";

/** The host the browser actually asked for, which decides which session applies. */
export const HOST_HEADER = "X-Rheo-Host";

export interface Membership {
  workspace_id: string;
  role: string;
}

export interface SessionActor {
  kind: string;
  id: string;
}

export type SessionResult =
  | {
      state: "ok";
      actor: SessionActor;
      activeWorkspaceId: string;
      role: string;
      memberships: Membership[];
    }
  | { state: "no-workspace" }
  | { state: "unauthenticated"; refusal: string }
  | { state: "unavailable" };

/** The refusal the listener itself returns when either header is absent. */
const SESSION_MISSING = "session_missing";

/**
 * The one refusal that means "signed in, but in no workspace" (#157).
 *
 * `core` checks the session's own validity first (missing, expired, revoked) and
 * only then whether it points at a workspace, so a `workspace_unselected` answer
 * already says the session is live. Since `create_session` defaults every session
 * with an active membership, this is an account with no workspace to be in yet.
 * It still carries no account and unlocks nothing: it is a distinct state so the
 * page can say "no workspace yet" and offer sign-out, not "sign in".
 */
const WORKSPACE_UNSELECTED = "workspace_unselected";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isMembership(value: unknown): value is Membership {
  return (
    isRecord(value) &&
    typeof value.workspace_id === "string" &&
    typeof value.role === "string"
  );
}

interface SessionOkBody {
  state: "ok";
  actor: SessionActor;
  active_workspace_id: string;
  role: string;
  memberships: Membership[];
}

/**
 * Narrow a 200 body to the signed-in shape.
 *
 * Every field is required. A body of `{"state": "ok"}` and nothing else is a
 * contract break, not a signed-in account with unknown details — treating it as
 * the latter is precisely the fail-open this function exists to prevent.
 */
function isSessionOkBody(value: unknown): value is SessionOkBody {
  if (!isRecord(value) || value.state !== "ok") {
    return false;
  }
  const actor = value.actor;
  if (
    !isRecord(actor) ||
    typeof actor.kind !== "string" ||
    typeof actor.id !== "string"
  ) {
    return false;
  }
  if (
    typeof value.active_workspace_id !== "string" ||
    typeof value.role !== "string"
  ) {
    return false;
  }
  return Array.isArray(value.memberships) && value.memberships.every(isMembership);
}

/**
 * Resolve the session behind a request's `rheo_session` cookie and forwarded host.
 *
 * `sessionSecret` and `host` come from the request, never from configuration: the
 * cookie is host-only, so a session is only ever valid for the host that was asked
 * for.
 */
export async function fetchSession(options: {
  sessionSecret: string | undefined;
  host: string | undefined;
}): Promise<SessionResult> {
  const credentials = internalApiCredentials();
  if (credentials === null) {
    return { state: "unavailable" };
  }
  if (!options.sessionSecret || !options.host) {
    // Knowable without a round trip, and the same state the listener would
    // return for the same inputs.
    return { state: "unauthenticated", refusal: SESSION_MISSING };
  }
  try {
    const response = await fetch(
      `${credentials.baseUrl}${INTERNAL_SESSION_PATH}`,
      {
        cache: "no-store",
        headers: {
          [INTERNAL_SECRET_HEADER]: credentials.secret,
          [SESSION_HEADER]: options.sessionSecret,
          [HOST_HEADER]: options.host,
        },
      },
    );
    if (!response.ok) {
      // Includes the listener's own 401 when `X-Rheo-Internal` is wrong: that
      // says nothing about the session, so it is "unavailable", not "signed out".
      return { state: "unavailable" };
    }
    const body: unknown = await response.json();
    if (isSessionOkBody(body)) {
      return {
        state: "ok",
        actor: body.actor,
        activeWorkspaceId: body.active_workspace_id,
        role: body.role,
        memberships: body.memberships,
      };
    }
    if (isRecord(body) && body.state === WORKSPACE_UNSELECTED) {
      return { state: "no-workspace" };
    }
    if (isRecord(body) && typeof body.state === "string" && body.state !== "ok") {
      return { state: "unauthenticated", refusal: body.state };
    }
    // Either an unrecognisable body, or one claiming `"ok"` that did not satisfy
    // the guard above. The second is a contract break rather than a refusal, and
    // must not be reported as a refusal state called "ok".
    return { state: "unavailable" };
  } catch {
    return { state: "unavailable" };
  }
}
