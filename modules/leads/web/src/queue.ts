/**
 * The job queue's pure rules: how a card formats its facts, how a fit rating
 * becomes a pill, and how filters, sorting, saved preferences and an optimistic
 * dismiss behave. Kept apart from the components so each rule is tested alone.
 */
import { row, rows, str, type Row } from "./data";

/** Above this many proposals a posting is crowded: the chip hides it and the
 * detail header warns. */
export const MAX_PROPOSALS = 50;

/**
 * Band-only proposal counts are stored as the band's upper bound, so "20 to 50"
 * arrives as 50. Showing that bare number would read as fifty bidders. Exact
 * counts carry their own flag and show as the literal number.
 */
const PROPOSAL_BANDS: Record<number, string> = {
  4: "under 5",
  10: "5-10",
  15: "10-15",
  20: "15-20",
  50: "20-50",
};

function count(value: unknown): number | null {
  const text = str(value).trim();
  if (!/^\d+$/.test(text)) return null;
  return Number(text);
}

function amount(value: unknown): number | null {
  const text = str(value).trim();
  if (text === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

export function proposalCount(fields: Row): number | null {
  return count(fields.proposal_count);
}

export function formatProposals(fields: Row): string {
  const n = proposalCount(fields);
  if (n === null) return "proposals unknown";
  if (str(fields.proposal_count_exact) === "true") return `${n} proposals`;
  if (n > MAX_PROPOSALS) return `${MAX_PROPOSALS}+ proposals`;
  return `${PROPOSAL_BANDS[n] ?? n} proposals`;
}

const dollars = (n: number) =>
  `$${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

export function formatBudget(fields: Row): string {
  const min = amount(fields.budget_min);
  const max = amount(fields.budget_max);
  if (!min && !max) return "No budget listed";
  const rate = str(fields.job_type) === "hourly" ? "/hr" : "";
  if (min && max && min !== max) return `${dollars(min)} - ${dollars(max)}${rate}`;
  return `${dollars((min || max) as number)}${rate}`;
}

/** "$12,400 spent", or nothing when the spend is unknown or zero. */
export function formatSpend(fields: Row): string | null {
  const n = amount(fields.client_spend);
  return n !== null && n > 0 ? `${dollars(Math.round(n))} spent` : null;
}

export function formatRating(fields: Row): string | null {
  const n = amount(fields.client_rating);
  return n !== null && n > 0 ? `${n} rating` : null;
}

/** "68% hire rate", or nothing when unknown. A 0% rate with no reviews is the
 * feed's placeholder for a client with no history, not a fact (#391). */
export function formatHireRate(fields: Row): string | null {
  const n = amount(fields.client_hire_rate);
  if (n === null) return null;
  if (n === 0 && !count(fields.client_reviews)) return null;
  const percent = n <= 1 ? n * 100 : n;
  return `${Math.round(percent)}% hire rate`;
}

/** "379 reviews", or nothing when there are none or the count is unknown. */
export function formatReviews(fields: Row): string | null {
  const n = count(fields.client_reviews);
  return n ? `${n.toLocaleString("en-US")} ${n === 1 ? "review" : "reviews"}` : null;
}

/** A client with a known zero spend and a known zero review count. Unknown
 * values are not evidence of a new client. */
export function isNewClient(fields: Row): boolean {
  return amount(fields.client_spend) === 0 && count(fields.client_reviews) === 0;
}

/** The client invited the owner to this job. */
export function isInvited(fields: Row): boolean {
  return str(fields.invited) === "true";
}

export function paymentVerified(fields: Row): boolean {
  return str(fields.client_payment_verified) === "true";
}

/** Over the cap only on a count above it; a band never claims more. */
export function overCap(fields: Row): boolean {
  const n = proposalCount(fields);
  return n !== null && n > MAX_PROPOSALS;
}

/** The posting address, only when it is an https URL. */
export function postingUrl(fields: Row): string | null {
  const value = str(fields.posting_url);
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** How the opportunity arrived, for the source badge. */
export function sourceLabel(source: unknown): string | null {
  const labels: Record<string, string> = {
    poll: "Auto",
    manual: "Manual",
    import: "Import",
    webhook: "Webhook",
  };
  const value = str(source);
  return value ? (labels[value] ?? value) : null;
}

export type FitBand = "high" | "mid" | "low" | "dim";
export interface Fit {
  /** What the pill shows: the 1-100 score when one was given, else a short
   * rating. Never a number nobody gave. */
  mark: string;
  band: FitBand;
  /** The decision word, from the same table as the band so they never disagree. */
  verdict: string;
  /** Title and accessible name. */
  description: string;
  /** Sort weight: higher first; unassessed is 0. */
  rank: number;
}
const FITS: Record<string, Omit<Fit, "description">> = {
  high: { mark: "High", band: "high", verdict: "Pursue", rank: 4 },
  medium: { mark: "Med", band: "mid", verdict: "Review", rank: 3 },
  needs_information: { mark: "?", band: "dim", verdict: "Review", rank: 2 },
  low: { mark: "Low", band: "low", verdict: "Skip", rank: 1 },
};
/** A recorded 1-100 score, or null when the assessment carries none. */
export function scoreOf(assessment: unknown): number | null {
  const score = row(assessment).score;
  return typeof score === "number" &&
    Number.isInteger(score) &&
    score >= 1 &&
    score <= 100
    ? score
    : null;
}
/** The pill for an assessment: its score when it has one, else its fit rating;
 * null when there is no assessment (or its rating is unknown): nothing is
 * invented. The band always follows the fit, which the score decided. */
export function fitFor(assessment: unknown): Fit | null {
  const fit = str(row(assessment).fit);
  const entry = assessment == null ? undefined : FITS[fit];
  if (!entry) return null;
  const said =
    fit === "needs_information" ? "fit needs information" : `fit ${fit}`;
  const score = scoreOf(assessment);
  if (score !== null) {
    return {
      ...entry,
      mark: String(score),
      description: `${entry.verdict}: scored ${score} of 100, ${said}`,
    };
  }
  return { ...entry, description: `${entry.verdict}: ${said}` };
}

const HOUR = 3600000;
/** "3h ago", "2d ago" or "just now" from an instant. */
export function timeAgo(value: unknown, now: number): string {
  const then = new Date(str(value)).getTime();
  if (Number.isNaN(then)) return "";
  const hours = Math.floor((now - then) / HOUR);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
/** A posting date has no time, so it reads in days: "posted today", "posted 3d ago". */
export function postedAgo(value: unknown, now: number): string {
  const text = str(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return "";
  const day = Date.parse(`${text}T00:00:00Z`);
  const today = Math.floor(now / (24 * HOUR)) * 24 * HOUR;
  const days = Math.round((today - day) / (24 * HOUR));
  if (Number.isNaN(days)) return "";
  return days <= 0 ? "posted today" : `posted ${days}d ago`;
}
/** When it was posted, or else when it arrived here. */
export function age(item: Row, now: number): string {
  return (
    postedAgo(row(item.fields).posted_on, now) ||
    `arrived ${timeAgo(item.created_at, now)}`
  );
}

/** A UUID's short ID: its last 8 hex characters, the random tail (a UUIDv7's
 * head is a timestamp, so records from the same minute share it). */
export function shortId(id: unknown): string {
  const hex = str(id).replaceAll("-", "").toLowerCase();
  return /^[0-9a-f]{32}$/.test(hex) ? hex.slice(-8) : str(id);
}

/** A job's short ID as the server gave it (lengthened if two jobs ever share a
 * tail), or the plain 8-character tail of its id. */
export function jobId(item: Row): string {
  return str(item.short_id) || shortId(item.id);
}

export interface Followup {
  due: string;
  action: string;
  overdue: boolean;
}
/** The pending follow-up, with overdue meaning due before today (UTC). */
export function followupFor(item: Row, now: number): Followup | null {
  const followup = row(item.followup);
  const due = str(followup.due_on);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(due)) return null;
  const today = new Date(now).toISOString().slice(0, 10);
  return { due, action: str(followup.action), overdue: due < today };
}

export function partyName(item: Row): string | null {
  const name = str(row(item.party).name).trim();
  return name || null;
}

export type Sort = "score" | "newest";
export const SORTS: readonly { value: Sort; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "newest", label: "Newest" },
];
export interface Prefs {
  /** "" is the default scope: the job-search pipelines. */
  pipeline: string;
  /** "" is the pipeline's default (a job-search pipeline's first open stage,
   * else open stages), "open" open stages, "all" every stage, else a stage id. */
  stage: string;
  hideFull: boolean;
  sort: Sort;
}
export const DEFAULT_PREFS: Prefs = {
  pipeline: "",
  stage: "",
  hideFull: true,
  sort: "score",
};
export const PREFS_KEY = "rheo.leads.queue.v1";

type Store = Pick<Storage, "getItem" | "setItem">;
/** Saved preferences (JSON text) merged over the defaults; any unreadable part
 * is ignored. */
export function parsePrefs(text: string | null | undefined): Prefs {
  let saved: Row = {};
  try {
    saved = row(JSON.parse(text ?? "null"));
  } catch {
    saved = {};
  }
  const choice = (value: unknown, fallback: string) =>
    typeof value === "string" && value.length <= 128 ? value : fallback;
  return {
    pipeline: choice(saved.pipeline, DEFAULT_PREFS.pipeline),
    stage: choice(saved.stage, DEFAULT_PREFS.stage),
    hideFull:
      typeof saved.hideFull === "boolean"
        ? saved.hideFull
        : DEFAULT_PREFS.hideFull,
    sort: saved.sort === "newest" ? "newest" : DEFAULT_PREFS.sort,
  };
}
/** What the store holds, or null when it is missing or refuses to be read. */
export function readPrefsText(store: Store | null | undefined): string | null {
  try {
    return store?.getItem(PREFS_KEY) ?? null;
  } catch {
    return null;
  }
}
export function loadPrefs(store: Store | null | undefined): Prefs {
  return parsePrefs(readPrefsText(store));
}
/** Saves quietly: a private window or full storage keeps the session's choice.
 * Returns the saved text so a caller can keep it when the store refused it. */
export function savePrefs(store: Store | null | undefined, prefs: Prefs): string {
  const text = JSON.stringify(prefs);
  try {
    store?.setItem(PREFS_KEY, text);
  } catch {
    // Storage is a convenience; the controls still work without it.
  }
  return text;
}

/** Whether a stage still belongs in the view the stage filter asks for. */
export function stageMatches(
  stage: string,
  stageId: string,
  kind: string,
): boolean {
  if (stage === "all") return true;
  if (stage === "" || stage === "open") return kind === "open";
  return stage === stageId;
}

export interface StageMove {
  stage_id: string;
  label: string;
  kind: string;
}
/** The list after a stage change: the card takes its new stage, and leaves the
 * list when the stage filter no longer covers it. Its next moves are unknown
 * until the page reloads, so they are cleared rather than guessed. */
export function afterMove(
  items: Row[],
  id: string,
  to: StageMove,
  stage: string,
): Row[] {
  return items.flatMap((item) => {
    if (str(item.id) !== id) return [item];
    const moved = {
      ...item,
      stage_id: to.stage_id,
      stage_label: to.label,
      stage_kind: to.kind,
      transitions: [],
      dismiss: null,
    };
    return stageMatches(stage, to.stage_id, to.kind) ? [moved] : [];
  });
}

/** A dismiss is a move to the card's closed dismiss stage. */
export function afterDismiss(items: Row[], id: string, stage: string): Row[] {
  const item = items.find((each) => str(each.id) === id);
  const to = row(item?.dismiss);
  return afterMove(
    items,
    id,
    { stage_id: str(to.stage_id), label: str(to.label), kind: "terminal" },
    stage,
  );
}

/** Where a dismiss can be undone to: the open stage the server named for the
 * card's dismiss, when ``to`` is that dismiss. Null for any other move, or when
 * the closed stage has no way back. */
export function undoFor(item: Row, to: StageMove): StageMove | null {
  const dismiss = row(item.dismiss);
  const undo = row(dismiss.undo);
  if (to.kind !== "terminal" || to.stage_id !== str(dismiss.stage_id)) return null;
  if (!str(undo.stage_id)) return null;
  return { stage_id: str(undo.stage_id), label: str(undo.label), kind: "open" };
}

/** A closed card's way back: its first open next stage, or null while it is open
 * or has none. */
export function reopenFor(item: Row): StageMove | null {
  return str(item.stage_kind) === "terminal" ? undoTarget(item, "") : null;
}

/** The queue's query names a job just dismissed from its page, and the stage it
 * left, so the queue can offer Undo. */
export const DISMISSED_PARAMS = ["dismissed", "from"] as const;
export function dismissedHref(back: string, ref: string, from: string): string {
  const query = new URLSearchParams({ dismissed: ref, from }).toString();
  return `${back}${back.includes("?") ? "&" : "?"}${query}`;
}
/** Of a dismissed job's moves, the one an undo takes: back to the stage it left
 * when allowed, else its first open one. */
export function undoTarget(item: Row, from: string): StageMove | null {
  const open = rows(item.transitions).filter((m) => str(m.kind) === "open");
  const to = open.find((m) => str(m.stage_id) === from) ?? open[0];
  return to
    ? { stage_id: str(to.stage_id), label: str(to.label), kind: "open" }
    : null;
}

/** A held job's clause, as the review card's badge names it. */
const HOLD_LABELS: Record<string, string> = {
  payment_unverified: "Payment unverified",
  low_spend: "Low client spend",
  low_rating: "Low client rating",
  low_hire_rate: "Low hire rate",
  high_proposals: "Too many proposals",
};
export interface Hold {
  code: string;
  label: string;
  text: string;
}
/** The clauses that held a job, from the review queue's ``hold`` list. */
export function holdsOf(item: Row): Hold[] {
  return rows(item.hold).map((h) => ({
    code: str(h.code),
    label: HOLD_LABELS[str(h.code)] ?? "Held by the filters",
    text: str(h.text),
  }));
}
/** The review list after a promote or a dismiss: that card leaves it. */
export function without(items: Row[], id: string): Row[] {
  return items.filter((item) => str(item.id) !== id);
}
