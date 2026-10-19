import { row, str, type Row } from "./data";

/** Words for the Scoring view: the status line, each job's stage and each model
 * call. Pure, so the view and its tests read the same text. */

export const FAST_REFRESH_MS = 15000;
export const SLOW_REFRESH_MS = 120000;
const RECENT_SECONDS = 300;

const PURPOSES: Record<string, string> = {
  scoring: "Score",
  analysis: "Proposal analysis",
  gate: "Accuracy check",
  draft: "Proposal draft",
  repair: "Draft repair",
  milestones: "Pricing",
};
export function purposeLabel(purpose: unknown): string {
  const value = str(purpose);
  return PURPOSES[value] ?? value.replaceAll("_", " ");
}

const FAILURES: Record<string, string> = {
  usage_limited: "usage limit",
  output_invalid: "unreadable answer",
  no_structured_result: "no answer",
  assessment_invalid: "answer failed its checks",
  iteration_limit: "ran out of turns",
  approval_required: "asked for approval",
  tool_denied: "tried a tool",
  deadline_exceeded: "timed out",
  stream_truncated: "answer cut off",
  cancelled: "cancelled",
  adapter_unavailable: "model unavailable",
  credential_not_owned: "login belongs to another account",
  auth_failed: "login refused",
  auth_missing: "no model login",
  token_revoke_failed: "run token not revoked",
  unexpected: "unexpected error",
};
/** Why a job feed detail read failed, in words; codes it does not know (a new
 * tool error code) show as they are. */
const READ_FAILURES: Record<string, string> = {
  no_description: "answered without a description",
  no_feed_id: "no id to read",
  not_read_deadline: "not read: run out of time",
  not_read_cap: "not read: read limit reached",
  unavailable: "service unavailable",
  rate_limited: "rate limited",
  protocol: "unreadable answer",
  tool_error: "refused by the job service",
  endpoint_misconfigured: "wrong address",
};
export function readFailureLabel(code: string): string {
  const [kind, detail] = code.split(":", 2);
  const known = READ_FAILURES[kind ?? ""] ?? code.replaceAll("_", " ");
  return kind === "tool_error" && detail ? `${known} (${detail})` : known;
}

const STOPS: Record<string, string> = {
  upstream_unavailable: "job service unavailable",
  upstream_rate_limited: "rate limited",
  authorization_required: "needs reconnecting",
  endpoint_misconfigured: "wrong address",
  account_selection_required: "choose an account",
  collection_partial: "cut short",
  token_refresh_failed: "could not renew its login",
  refresh_token_unsaved: "renewed login not saved: reconnect",
};
export function stopLabel(code: unknown): string {
  const value = str(code);
  return STOPS[value] ?? value.replaceAll("_", " ");
}

export function failureLabel(kind: unknown): string {
  const value = str(kind);
  return FAILURES[value] ?? value.replaceAll("_", " ");
}

/** "42s", "3 min", "1h 5m": an elapsed span in seconds. */
export function span(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** Seconds from an instant to now, or null when it is not a time. */
export function since(value: unknown, now: number): number | null {
  const then = new Date(str(value)).getTime();
  return Number.isNaN(then) ? null : Math.max(0, (now - then) / 1000);
}

/** A clock time in UTC, with the weekday when it is not today. */
export function clock(value: unknown, now: number): string {
  const d = new Date(str(value));
  if (Number.isNaN(d.valueOf())) return "";
  const time = new Intl.DateTimeFormat("en", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  }).format(d);
  const day = (t: number) => Math.floor(t / 86400000);
  if (day(d.valueOf()) === day(now)) return time;
  const weekday = new Intl.DateTimeFormat("en", {
    weekday: "short",
    timeZone: "UTC",
  }).format(d);
  return `${weekday} ${time}`;
}

export function tokens(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString("en") : "–";
}

export type Tone = "idle" | "busy" | "warn";

/** The status line: what scoring is doing now. */
export function statusLine(
  status: Row,
  now: number,
): { tone: Tone; text: string } {
  const state = str(status.state);
  const title = str(status.title) || "a job";
  if (state === "off")
    return { tone: "warn", text: "Scoring is off: job search is not on." };
  if (state === "no_profile")
    return {
      tone: "warn",
      text: "Scoring waits for a job-search profile.",
    };
  if (state === "running") {
    const elapsed = since(status.started_at, now);
    const took = elapsed === null ? "" : ` for ${span(elapsed)}`;
    const purpose = str(status.purpose);
    if (purpose === "scoring")
      return { tone: "busy", text: `Scoring "${title}"${took}` };
    if (purpose === "milestones")
      return { tone: "busy", text: `Pricing "${title}"${took}` };
    return {
      tone: "busy",
      text: `Drafting a proposal for "${title}"${took}. Scoring resumes after.`,
    };
  }
  if (state === "paused") {
    const until = clock(status.until, now);
    const kind = str(status.failure_kind);
    const when = until ? ` around ${until}` : "";
    return kind === "usage_limited"
      ? {
          tone: "warn",
          text: `Paused on the usage limit. Resumes${when}.`,
        }
      : {
          tone: "warn",
          text: `Paused: ${failureLabel(kind)}. Tries again${when}.`,
        };
  }
  const last = since(status.last_call_at, now);
  return {
    tone: "idle",
    text: last === null ? "Idle." : `Idle. Last model call ${span(last)} ago.`,
  };
}

/** Whether scoring is moving, so the page refreshes itself sooner: a call is
 * running, or jobs are queued and a call finished in the last few minutes. A
 * queue nothing is draining refreshes slowly. */
export function busy(view: Row, now: number): boolean {
  const status = row(view.status);
  if (str(status.state) === "running") return true;
  const queued = typeof view.queued_total === "number" && view.queued_total > 0;
  const last = since(status.last_call_at, now);
  return queued && last !== null && last < RECENT_SECONDS;
}

export const STAGE_LABELS: Record<string, string> = {
  scoring: "scoring now",
  queued: "queued",
  waiting: "claimed",
  retrying: "retrying",
  paused: "paused",
  scored: "scored",
  needs_posting: "needs the full posting",
  failed: "failed",
  held: "held by the filters",
  closed: "closed",
  untracked: "new",
};

/** A job's stage chip: its short mark and the longer title behind it. */
export function stageChip(
  job: Row,
  now: number,
): { mark: string; title: string; tone: string } {
  const stage = str(job.stage);
  const retry = clock(job.retry_at, now);
  switch (stage) {
    case "scoring":
      return { mark: "now", title: "Scoring now", tone: "busy" };
    case "queued":
      return {
        mark: `#${str(job.position)}`,
        title: `Position ${str(job.position)} in the queue`,
        tone: "plain",
      };
    case "retrying":
      return {
        mark: "retry",
        title: `Attempt ${str(job.attempts)} failed (${failureLabel(job.failure_kind)}).${retry ? ` Next try around ${retry}.` : ""}`,
        tone: "warn",
      };
    case "paused":
      return {
        mark: "paused",
        title: `Waiting on the ${failureLabel(job.failure_kind)}.${retry ? ` Resumes around ${retry}.` : ""}`,
        tone: "warn",
      };
    case "waiting":
      return {
        mark: "claimed",
        title: `A sweep holds it${retry ? ` until ${retry}` : ""}.`,
        tone: "plain",
      };
    case "needs_posting":
      return {
        mark: "posting",
        title: "Not scored: paste the full posting on the job to score it.",
        tone: "plain",
      };
    case "failed":
      return {
        mark: "failed",
        title: job.failure_kind
          ? `Scoring failed (${failureLabel(job.failure_kind)}).`
          : "Scoring failed.",
        tone: "danger",
      };
    case "held":
      return {
        mark: "held",
        title: "Held back by the client filters before scoring.",
        tone: "plain",
      };
    case "closed":
      return { mark: "closed", title: "Closed before scoring.", tone: "dim" };
    default:
      return {
        mark: "new",
        title: "Not seen by scoring yet; the next sweep queues it.",
        tone: "dim",
      };
  }
}

/** The fit band a 1-100 score falls in, as the job queue colours it. */
export function scoreBand(score: number): "high" | "mid" | "low" {
  return score >= 80 ? "high" : score >= 60 ? "mid" : "low";
}

/** How a model call ended, in words. */
export function outcomeLabel(call: Row): string {
  const outcome = str(call.outcome);
  if (outcome === "answered")
    return str(call.purpose) === "scoring" ? "Scored" : "Answered";
  if (outcome === "deferred")
    return `Deferred: ${failureLabel(call.failure_kind)}`;
  if (outcome === "failed") return `Failed: ${failureLabel(call.failure_kind)}`;
  if (outcome === "running") return "Running";
  if (outcome === "interrupted") return "Interrupted";
  return outcome;
}

/** Where an import came from, for its heading. */
export function batchSource(sources: unknown): string {
  const list = Array.isArray(sources) ? sources.map(str) : [];
  const words = list.map((s) =>
    s === "poll" ? "Job feed" : s === "manual" ? "Pasted" : "Import",
  );
  return [...new Set(words)].join(" + ") || "Import";
}
