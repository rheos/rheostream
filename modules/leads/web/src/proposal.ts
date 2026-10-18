/** The proposal workflow's pure parts: what each tab says, the progress line, the
 * plain words for a failure or a refusal, and the findings panel's summary. No
 * React and no "use client", so a server screen and the tests can import it. */
import { row, rows, str, type Row } from "./data";

export type StageId = "posting" | "questions" | "proposal" | "pricing";
export const STAGE_IDS: readonly StageId[] = [
  "posting",
  "questions",
  "proposal",
  "pricing",
];
export const STAGE_LABELS: Record<StageId, string> = {
  posting: "Posting",
  questions: "Questions",
  proposal: "Proposal",
  pricing: "Pricing",
};
/** How long a page waits before it reloads to show a running draft's progress. */
export const REFRESH_MS = 4000;
/** The wait while a draft is held for the account (a usage limit, a login): the
 * cause clears in minutes at best, so the page looks again once a minute. */
export const WAITING_REFRESH_MS = 60_000;

/** Why a queued draft is waiting, when the account or deployment holds it. */
const WAITING: Record<string, string> = {
  usage_limited:
    "Waiting for the model's usage limit to reset. The draft starts again on its own.",
  credential_invalid:
    "Waiting: the model login needs attention. The draft starts once the login works again.",
  credential_not_owned:
    "Waiting: the model login belongs to another account. An owner needs to fix the login.",
  executable_unavailable:
    "Waiting: the model runtime is not available on the server.",
  adapter_unavailable: "Waiting: the model runtime is not available on the server.",
  token_revoke_failed: "Waiting on a server problem. The draft is retried shortly.",
};
/** The waiting line for a queued draft held by the account, or "". */
export function waitingLine(view: Row): string {
  if (draftState(view) !== "queued") return "";
  const kind = str(view.failure_kind);
  if (!kind) return "";
  return WAITING[kind] ?? "Waiting to retry the draft.";
}
/** The same for a queued pricing request. */
export function pricingWaitingLine(view: Row): string {
  const pricing = row(view.pricing);
  if (str(pricing.state) !== "queued") return "";
  const kind = str(pricing.failure_kind);
  if (!kind) return "";
  return WAITING[kind] ?? "";
}
export function refreshDelay(view: Row): number {
  // Whichever piece of work is moving sets the pace.
  const draftWaits = draftBusy(view) ? Boolean(waitingLine(view)) : true;
  const pricingWaits = pricingBusy(view)
    ? Boolean(pricingWaitingLine(view))
    : true;
  return draftWaits && pricingWaits ? WAITING_REFRESH_MS : REFRESH_MS;
}

export function stageFromHash(hash: string): StageId {
  const candidate = hash.replace(/^#stage-/, "");
  return STAGE_IDS.find((stage) => stage === candidate) ?? "posting";
}

export type DraftState = "idle" | "queued" | "drafting" | "ready" | "failed";
export function draftState(view: Row): DraftState {
  const state = str(view.state);
  return state === "queued" ||
    state === "drafting" ||
    state === "ready" ||
    state === "failed"
    ? state
    : "idle";
}
export function draftBusy(view: Row): boolean {
  const state = draftState(view);
  return state === "queued" || state === "drafting";
}
/** Any work the page follows: a draft or a pricing request. */
export function busy(view: Row): boolean {
  return draftBusy(view) || pricingBusy(view);
}

/** The job's pricing, from the proposal view. */
export function pricingOf(view: Row): Row {
  return row(view.pricing);
}
export function pricingState(view: Row): string {
  return str(pricingOf(view).state) || "idle";
}
export function pricingBusy(view: Row): boolean {
  const state = pricingState(view);
  return state === "queued" || state === "running";
}

export interface MilestoneView {
  id: string;
  title: string;
  description: string;
  amount: string;
  dueOn: string;
}
export function milestones(view: Row): MilestoneView[] {
  return rows(pricingOf(view).milestones).map((m) => ({
    id: str(m.id),
    title: str(m.title),
    description: str(m.description),
    amount: str(m.amount),
    dueOn: str(m.due_on),
  }));
}
/** "$1,200.00" from a decimal string; the text itself when it is not a number. */
export function dollars(amount: string): string {
  const n = Number(amount);
  return Number.isFinite(n) && amount.trim() !== ""
    ? `$${n.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : amount;
}
/** What one milestone copies as: the form's title, amount and due date. */
export function milestoneLine(m: MilestoneView): string {
  return `${m.title}: ${dollars(m.amount)}${m.dueOn ? `, due ${m.dueOn}` : ""}`;
}
/** The rate with its per-hour unit, and "about 20 h for $800.00", from the pricing
 * suggestion. */
export function formRate(view: Row): { rate: string; detail: string } | null {
  const s = row(pricingOf(view).suggestion);
  const rate = Number(str(s.rate));
  if (!str(s.rate) || !Number.isFinite(rate)) return null;
  const hours = str(s.hours);
  const total = str(s.fixed_total);
  return {
    rate: `$${rate.toLocaleString("en", { maximumFractionDigits: 2 })}/hr`,
    detail:
      hours && total ? `about ${Number(hours)} h for ${dollars(total)}` : "",
  };
}

/** The state word under each tab's name. */
export function stageWords(
  view: Row,
  posting: string,
  questions: string,
): Record<StageId, string> {
  const saved = str(view.questions);
  const state = draftState(view);
  const priced = pricingState(view);
  return {
    pricing: pricingBusy(view)
      ? "Pricing"
      : milestones(view).length
        ? "Ready"
        : priced === "failed"
          ? "Failed"
          : pricingOf(view).available === false
            ? "Not set"
            : "Optional",
    posting: posting.trim() ? "Ready" : "Needed",
    questions:
      questions.trim() !== saved.trim()
        ? "Unsaved"
        : saved.trim()
          ? "Added"
          : "Optional",
    proposal:
      state === "ready"
        ? "Ready"
        : state === "failed"
          ? "Failed"
          : state === "idle"
            ? "Draft next"
            : "Drafting",
  };
}

/** "Drafting (2 of 4)" while a stage runs, else a plain waiting line. */
export function progressLine(view: Row): string {
  const stage = str(view.stage);
  const step = str(view.step);
  const total = str(view.total);
  if (draftState(view) === "drafting" && stage && step && total)
    return `${stage} (${step} of ${total})`;
  const waiting = waitingLine(view);
  if (waiting) return waiting;
  if (draftState(view) === "queued" && str(view.failure_kind))
    return `${failureCopy(str(view.failure_kind))} Trying again shortly.`;
  return draftState(view) === "queued"
    ? "Queued. The draft starts within a minute."
    : "Starting the draft.";
}

const FAILURES: Record<string, string> = {
  output_invalid: "The model's answer could not be used.",
  no_structured_result: "The model's answer could not be used.",
  deadline_exceeded: "The model took too long to answer.",
  runtime_error: "The model run stopped early.",
  stream_truncated: "The model run stopped early.",
  description_missing:
    "This job has no posting text yet. Paste the full posting first.",
  profile_missing: "No proposal profile is set for this workspace yet.",
  account_unavailable:
    "The account that drafts proposals is no longer an owner of this workspace.",
  claim_lapsed: "The draft was interrupted too many times.",
  restored: "The draft was interrupted when the workspace was restored.",
};
export function failureCopy(kind: string): string {
  return FAILURES[kind] ?? `Drafting failed (${kind || "unknown reason"}).`;
}

const REFUSALS: Record<string, string> = {
  profile_required: "No proposal profile is set for this workspace yet.",
  draft_in_progress: "A draft is already being prepared.",
  draft_edited:
    "Some sections were edited by hand or filled from the library. A new draft replaces every section.",
  capability_disabled: "Job search is off for this workspace.",
  not_found: "That draft or section is no longer there. Reload the page.",
  input_invalid:
    "That text could not be saved. It may be empty or longer than the form allows.",
  record_stale: "This job changed. Reload the page.",
  pricing_not_set:
    "Pricing is not set up yet: an owner adds a day rate and hourly rates to the proposal profile.",
  pricing_in_progress: "Milestones are already being prepared.",
  operation_not_permitted:
    "This action is no longer available. Reload the page and try again.",
};
export function refusalCopy(code: string): string {
  return REFUSALS[code] ?? "That did not go through. Try again.";
}

export interface SectionView {
  id: string;
  kind: string;
  label: string;
  question: string;
  body: string;
  libraryMatch: string;
  fromLibrary: boolean;
  edited: boolean;
}
export function sections(view: Row): SectionView[] {
  return rows(view.sections).map((section) => ({
    id: str(section.id),
    kind: str(section.kind),
    label:
      str(section.kind) === "cover_letter"
        ? "Cover letter"
        : str(section.question) || "Answer",
    question: str(section.question),
    body: str(section.body),
    libraryMatch: str(section.library_match_id),
    fromLibrary: Boolean(section.library_answer_id),
    edited: section.edited === true,
  }));
}

/** Rows for a textarea that fits its text without a manual resize. */
export function rowsFor(text: string, min = 4, width = 88): number {
  const lines = text
    .split("\n")
    .reduce((n, line) => n + Math.max(1, Math.ceil(line.length / width)), 0);
  return Math.min(Math.max(min, lines + 1), 60);
}

export const RULE_LABELS: Record<string, string> = {
  em_dash: "Em dash",
  markdown_link: "Markdown link",
  contrast_stacking: "Contrast stacking",
  contrast_density: "Contrast density (whole draft)",
  mantra_fragment: "Mantra fragment",
  candour_announcement: "Candour announcement",
  long_paragraph: "Long paragraph",
  tic_phrase: "Tic phrase",
  rhetorical_fragment: "Rhetorical fragment",
  long_letter: "Long cover letter",
  duplicate_answer: "Repeated in an answer",
};

export interface Claim {
  claim: string;
  kind: string;
  context: string;
  wasQuestioned: boolean;
}
export interface Findings {
  checked: boolean;
  retried: boolean;
  questioned: string[];
  flagged: Claim[];
  style: { rule: string; where: string; excerpt: string }[];
  repairedCount: number;
  repaired: boolean;
  figuresStale: boolean;
  analysis: {
    angle: string;
    hiringFor: string;
    mustAddress: string[];
    gaps: string[];
  };
  clean: boolean;
}

/** Where a style finding sits, in words: "Answer 2, paragraph 1". */
export function where(location: string): string {
  const [section, para] = location.split("/");
  const name =
    section === "cover_letter"
      ? "Cover letter"
      : section === "document"
        ? "Whole draft"
        : section?.startsWith("answer:")
          ? `Answer ${section.slice("answer:".length)}`
          : section || "Draft";
  return para?.startsWith("para:")
    ? `${name}, paragraph ${para.slice("para:".length)}`
    : name;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(str).filter(Boolean) : [];
}

export function findings(value: unknown): Findings | null {
  if (typeof value !== "object" || value === null) return null;
  const f = row(value);
  const flagged = rows(f.accuracy)
    .filter((a) => str(a.verdict) === "flagged")
    .map((a) => ({
      claim: str(a.claim),
      kind: str(a.kind),
      context: str(a.context),
      wasQuestioned: a.flagged_pre_retry === true,
    }));
  const style = rows(f.style).map((s) => ({
    rule: RULE_LABELS[str(s.rule)] ?? str(s.rule),
    where: where(str(s.location)),
    excerpt: str(s.excerpt),
  }));
  const analysis = row(f.analysis);
  const result: Findings = {
    // Older findings have no such key; they were checked.
    checked: f.accuracy_checked !== false,
    retried: f.retried === true,
    questioned: strings(f.flagged_pre_retry),
    flagged,
    style,
    repairedCount: rows(f.style_pre_repair).length,
    repaired: f.style_repaired === true,
    figuresStale: f.figures_stale === true,
    analysis: {
      angle: str(analysis.angle),
      hiringFor: str(analysis.hiring_for),
      mustAddress: strings(analysis.must_address),
      gaps: strings(analysis.gaps),
    },
    clean: false,
  };
  result.clean =
    result.checked &&
    !result.figuresStale &&
    !result.retried &&
    flagged.length === 0 &&
    style.length === 0 &&
    !result.repaired;
  return result;
}
