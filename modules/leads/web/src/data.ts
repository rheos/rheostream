import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
export type Row = Record<string, unknown>;
export function row(value: unknown): Row {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Row)
    : {};
}
export function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.map(row) : [];
}
export function str(value: unknown): string {
  return typeof value === "string"
    ? value
    : typeof value === "number"
      ? String(value)
      : "";
}
export function items(outcome: OperationOutcome): Row[] {
  return outcome.state === "ok" ? rows(row(outcome.result).items) : [];
}
export function data(outcome: OperationOutcome): Row {
  return outcome.state === "ok" ? row(row(outcome.result).data) : {};
}
/** Text as a person reads it: without the provider's marker tag that feed
 * descriptions can arrive wrapped in (#392). Only that outer wrapper goes,
 * never other markup; the stored text is unchanged. */
export function unwrapped(text: string): string {
  return text
    .replace(/^\s*<untrusted_participant_content>\s*/, "")
    .replace(/\s*<\/untrusted_participant_content>\s*$/, "");
}
export function label(value: unknown): string {
  return str(value).replaceAll("_", " ");
}
export function date(value: unknown): string {
  const d = new Date(str(value));
  return Number.isNaN(d.valueOf())
    ? "Not yet"
    : new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeZone: "UTC",
      }).format(d);
}
/** A moment to the minute, in UTC and labelled so, or the fallback. */
export function moment(value: unknown, fallback: string): string {
  const d = new Date(str(value));
  return value == null || Number.isNaN(d.valueOf())
    ? fallback
    : new Intl.DateTimeFormat("en", {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "UTC",
        timeZoneName: "short",
      }).format(d);
}
export function pageOffset(value: string | undefined): number {
  const n = Number(value ?? 0);
  return Number.isInteger(n) && n >= 0 && n <= 100000 ? n : 0;
}
const READS = [
  "leads.ui.catalog",
  "leads.ui.queue",
  "leads.ui.scoring",
  "leads.opportunity.list",
  "leads.opportunity.get",
  "leads.qualification.list",
  "leads.observation.get",
  "leads.intake.receipt",
  "leads.connection.health",
  "leads.jobproposal.get",
] as const;
export function read(
  shell: ShellApi,
  name: (typeof READS)[number],
  input: unknown = {},
): Promise<OperationOutcome> {
  return READS.includes(name)
    ? shell.call(name, input)
    : Promise.resolve({ state: "unavailable" });
}
/** The Job search preset's seeded id, from
 * rheo_leads/pipeline_common.py:JOBSEARCH_PRESET_ID (stable, set by migration). */
export const JOBSEARCH_PRESET_ID = "019bf0b0-0000-7000-8000-000000000002";
/** What a detail link carries for an opportunity: its short ID (#345) when the
 * server gave one, else the full reference. Both resolve. */
export function detailRef(record: Row): string {
  return str(record.short_id) || str(record.ref);
}
/** A pipeline's record reference. Here, not in a client module, because server
 * screens call it while rendering. */
export function pipelineRef(id: string): string {
  return `leads.pipeline:${id}`;
}
