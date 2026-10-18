/** The proposal workflow's pure parts: what each tab says, the progress line, the
 * plain words for a failure or a refusal, and the findings panel's summary. No
 * React and no "use client", so a server screen and the tests can import it. */
import { row, rows, str, type Row } from "./data";

export type StageId = "posting" | "questions" | "proposal";
export const STAGE_IDS: readonly StageId[] = ["posting", "questions", "proposal"];
export const STAGE_LABELS: Record<StageId, string> = {
  posting: "Posting",
  questions: "Questions",
  proposal: "Proposal",
};
/** How long a page waits before it reloads to show a running draft's progress. */
export const REFRESH_MS = 4000;

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
export function busy(view: Row): boolean {
  const state = draftState(view);
  return state === "queued" || state === "drafting";
}

/** The state word under each tab's name. */
export function stageWords(
  view: Row,
  posting: string,
  questions: string,
): Record<StageId, string> {
  const saved = str(view.questions);
  const state = draftState(view);
  return {
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
  if (str(view.failure_kind) === "usage_limited")
    return "Waiting for the model's usage limit to reset. The draft starts again on its own.";
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
  credential_invalid: "The model login needs attention.",
  credential_not_owned: "The model login belongs to another account.",
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
  input_invalid: "That text could not be saved. Check it is not empty.",
  record_stale: "This job changed. Reload the page.",
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
    !result.retried &&
    flagged.length === 0 &&
    style.length === 0 &&
    !result.repaired;
  return result;
}
