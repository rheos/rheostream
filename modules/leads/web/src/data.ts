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
export function pageOffset(value: string | undefined): number {
  const n = Number(value ?? 0);
  return Number.isInteger(n) && n >= 0 && n <= 100000 ? n : 0;
}
const READS = [
  "leads.ui.catalog",
  "leads.opportunity.list",
  "leads.opportunity.get",
  "leads.observation.get",
  "leads.intake.receipt",
  "leads.connection.health",
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
