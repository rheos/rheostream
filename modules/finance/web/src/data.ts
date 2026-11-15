/**
 * Wire shapes of finance's eight owner-only reads (`rheo_finance.wiring.reads`),
 * narrowed from the untyped `OperationOutcome.result`.
 *
 * The registered outputs leave out every field finance tiers restricted, so no
 * amount, total or headline ever reaches these parsers: a row says who, when and in
 * what state, never how much. Each parser tolerates a missing or mistyped field by
 * falling back to an empty value, so one odd row cannot take a screen down; the
 * module stays the authority on the real shape.
 */

type Row = Record<string, unknown>;

function row(value: unknown): Row {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Row)
    : {};
}
function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.map(row) : [];
}
function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}
function optional(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}
function int(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
function optionalInt(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function bool(value: unknown): boolean {
  return value === true;
}

// --- finance.status.get -------------------------------------------------------------

export interface StatusLine {
  lineNo: number;
  text: string;
}

export function statusLines(value: unknown): StatusLine[] {
  return rows(row(value).lines).map((r) => ({
    lineNo: int(r.line_no),
    text: str(r.text),
  }));
}

/**
 * The few facts the status strip shows, read out of the status lines' fixed
 * wording (`rheo_finance.reports.ops.status`). Anything the wording does not match
 * stays null, and the strip then says so rather than guessing.
 */
export interface StatusFacts {
  /** Line 1's leading stamp, e.g. "as of 2026-01-05 09:30 UTC". */
  asOf: string | null;
  /** True when line 1 says the schema is current. */
  databaseCurrent: boolean;
  /** Minutes since the last tick, or null when there was none or it did not parse. */
  tickMinutes: number | null;
  noTickYet: boolean;
  tickStale: boolean;
  /** The last completed sync to Current, as the module rendered it. */
  lastSync: string | null;
  /** "not synced since <stamp>", when the last sync failed. */
  syncFailedSince: string | null;
  queuedRetries: number | null;
  /** Line 5's confirm snapshot, kind -> count, in the line's own order. */
  confirm: { kind: string; count: number }[];
  confirmAsOf: string | null;
  /** Line 5's totals after the snapshot ("untied card payments 3", ...). */
  backlog: { label: string; count: number }[];
}

const TICK = /tick (\d+) min ago/;
const LAST_SYNC = /, last sync (.+)$/;
const NOT_SYNCED = /^reminders not synced since (.+?);/;
const RETRIES = /(\d+) queued retries/;
const CONFIRM = /^confirm last derive snapshot (\d{4}-\d{2}-\d{2}): ([^;]*)/;
const COUNTED = /^(.*\D) (\d+)$/;

function counted(part: string): { label: string; count: number } | null {
  const match = COUNTED.exec(part.trim());
  return match ? { label: match[1]!.trim(), count: Number(match[2]) } : null;
}

export function statusFacts(lines: readonly StatusLine[]): StatusFacts {
  const text = (n: number) => lines.find((l) => l.lineNo === n)?.text ?? "";
  const first = text(1);
  const tick = text(2);
  const backlog = text(5);
  const reminders = text(6);
  const checks = text(9);
  const minutes = TICK.exec(tick);
  const confirm = CONFIRM.exec(backlog);
  const totals = backlog.includes("; ") ? backlog.slice(backlog.indexOf("; ") + 2) : "";
  const retries = RETRIES.exec(reminders);
  return {
    asOf: first ? (first.split(",")[0] ?? null) : null,
    databaseCurrent: first.endsWith("database current"),
    tickMinutes: minutes ? Number(minutes[1]) : null,
    noTickYet: tick.includes("no tick yet"),
    tickStale: checks.includes("tick stale"),
    lastSync: LAST_SYNC.exec(reminders)?.[1] ?? null,
    syncFailedSince: NOT_SYNCED.exec(reminders)?.[1] ?? null,
    queuedRetries: retries ? Number(retries[1]) : null,
    confirm: confirm
      ? confirm[2]!
          .split(",")
          .map((part) => counted(part))
          .filter((c): c is { label: string; count: number } => c !== null)
          .map((c) => ({ kind: c.label, count: c.count }))
      : [],
    confirmAsOf: confirm ? confirm[1]! : null,
    backlog: totals
      .split(",")
      .map((part) => counted(part))
      .filter((c): c is { label: string; count: number } => c !== null),
  };
}

// --- finance.sink.state -------------------------------------------------------------

export type Approval = "none" | "current" | "stale";

export interface Sink {
  sink: string;
  settingEnabled: boolean;
  approval: Approval;
  approvedAt: string | null;
  /** The setting is on and the approval current: the sink actually sends. */
  enabled: boolean;
}

function approval(value: unknown): Approval {
  return value === "current" || value === "stale" ? value : "none";
}

export function sinks(value: unknown): Sink[] {
  return rows(row(value).sinks).map((r) => ({
    sink: str(r.sink),
    settingEnabled: bool(r.setting_enabled),
    approval: approval(r.approval),
    approvedAt: optional(r.approved_at),
    enabled: bool(r.enabled),
  }));
}

// --- finance.owed.list --------------------------------------------------------------

export interface Obligation {
  ref: string;
  payeeSlug: string;
  payeeLabel: string;
  kind: string;
  periodLabel: string;
  statementDate: string | null;
  issuedOn: string | null;
  dueDate: string | null;
  daysUntilDue: number | null;
  /** open, partial, presumed_paid or disputed: the only states the owed view holds. */
  status: string;
  primarySource: string;
  manualSource: string | null;
  beforeFloor: boolean;
  reminderEligible: boolean;
  paymentsUnmatchable: boolean;
  statementConflict: boolean;
  presumedPaidBasis: string | null;
}

export function obligation(value: unknown): Obligation {
  const r = row(value);
  return {
    ref: str(r.obligation_ref),
    payeeSlug: str(r.payee_slug),
    payeeLabel: str(r.payee_label) || str(r.payee_slug),
    kind: str(r.kind),
    periodLabel: str(r.period_label),
    statementDate: optional(r.statement_date),
    issuedOn: optional(r.issued_on),
    dueDate: optional(r.due_date),
    daysUntilDue: optionalInt(r.days_until_due),
    status: str(r.status),
    primarySource: str(r.primary_source),
    manualSource: optional(r.manual_source),
    beforeFloor: bool(r.before_floor),
    reminderEligible: bool(r.reminder_eligible),
    paymentsUnmatchable: bool(r.payments_unmatchable),
    statementConflict: bool(r.statement_conflict),
    presumedPaidBasis: optional(r.presumed_paid_basis),
  };
}

export interface Gap {
  path: string;
  issuerLabel: string;
  kind: string;
  status: string;
  periodStart: string | null;
  periodEnd: string | null;
}

export interface OpenItem {
  lineRef: string;
  taxYear: number;
  kind: string;
  occurredOn: string;
  accountLabel: string | null;
  direction: "in" | "out" | null;
  currency: string | null;
  state: string;
  reason: string;
  payeeLabel: string | null;
}

function openItem(value: unknown): OpenItem {
  const r = row(value);
  return {
    lineRef: str(r.line_ref),
    taxYear: int(r.tax_year),
    kind: str(r.kind),
    occurredOn: str(r.occurred_on),
    accountLabel: optional(r.account_label),
    direction: r.direction === "in" || r.direction === "out" ? r.direction : null,
    currency: optional(r.currency),
    state: str(r.state),
    reason: str(r.reason),
    payeeLabel: optional(r.payee_label) ?? optional(r.payee_slug),
  };
}

export interface Owed {
  asOf: string;
  today: string;
  windowDays: number;
  rows: Obligation[];
  /** Rows past the module's `finance.owed.max_rows` cap, not in `rows`. */
  more: number;
  credits: { count: number; items: OpenItem[] };
  gaps: Gap[];
  gapCount: number;
}

export function owed(value: unknown): Owed {
  const r = row(value);
  const credits = row(r.credits);
  return {
    asOf: str(r.as_of),
    today: str(r.today),
    windowDays: int(r.window_days),
    rows: rows(r.rows).map(obligation),
    more: int(r.more),
    credits: { count: int(credits.count), items: rows(credits.items).map(openItem) },
    gaps: rows(r.gaps).map((g) => ({
      path: str(g.path),
      issuerLabel: str(g.issuer_label),
      kind: str(g.kind),
      status: str(g.status),
      periodStart: optional(g.period_start),
      periodEnd: optional(g.period_end),
    })),
    gapCount: int(r.gap_count),
  };
}

export type Bucket = "overdue" | "soon" | "later" | "undated" | "parked";

/** States the module counts as owing nothing (`reports.owed.figures`). */
const PARKED = new Set(["presumed_paid", "disputed"]);

/** Where a row sits on the Owed screen: presumed-paid and disputed rows apart,
 * the rest by days to the due date and the module's window. */
export function bucket(o: Obligation, windowDays: number): Bucket {
  if (PARKED.has(o.status)) return "parked";
  if (o.daysUntilDue === null) return "undated";
  if (o.daysUntilDue < 0) return "overdue";
  return o.daysUntilDue <= windowDays ? "soon" : "later";
}

// --- finance.owed.trace -------------------------------------------------------------

export interface Payment {
  paidOn: string;
  source: string;
  state: string;
  auto: boolean;
  evidence: { role: string; documentPath: string | null; lineNo: number | null }[];
}

export interface Trace {
  ref: string;
  basisClass: string | null;
  presumedBasisKind: string | null;
  payments: Payment[];
}

export function traces(value: unknown): Map<string, Trace> {
  const out = new Map<string, Trace>();
  for (const t of rows(row(value).obligations)) {
    const ref = str(row(t.row).obligation_ref);
    if (!ref) continue;
    out.set(ref, {
      ref,
      basisClass: optional(t.basis_class),
      presumedBasisKind: optional(t.presumed_basis_kind),
      payments: rows(t.applications).map((a) => ({
        paidOn: str(a.paid_on),
        source: str(a.source),
        state: str(a.state),
        auto: bool(a.auto),
        evidence: rows(a.evidence).map((e) => ({
          role: str(e.role),
          documentPath: optional(e.document_path),
          lineNo: optionalInt(e.line_no),
        })),
      })),
    });
  }
  return out;
}

// --- finance.document.list ----------------------------------------------------------

export interface Document {
  path: string;
  kind: string;
  status: string;
  periodStart: string | null;
  periodEnd: string | null;
  official: boolean;
  extraction: string;
}

export function documents(value: unknown): Document[] {
  return rows(row(value).documents).map((r) => ({
    path: str(r.path),
    kind: str(r.kind),
    status: str(r.status),
    periodStart: optional(r.period_start),
    periodEnd: optional(r.period_end),
    official: bool(r.official),
    extraction: str(r.extraction),
  }));
}

/** The document states that need the owner, in the order the screen lists them. */
export const ATTENTION_STATUSES = ["needs_manual_entry", "rejected", "unparsed"] as const;

// --- finance.coverage.get -----------------------------------------------------------

export type CoverageState =
  | "have"
  | "partial"
  | "missing"
  | "rejected"
  | "unparsed"
  | "not_applicable";

export const COVERAGE_STATES: readonly CoverageState[] = [
  "have",
  "partial",
  "missing",
  "rejected",
  "unparsed",
  "not_applicable",
];

export interface CoverageMonth {
  account: string;
  accountLabel: string;
  /** The month's first day, YYYY-MM-DD. */
  month: string;
  state: CoverageState;
  reason: string | null;
}

export interface Coverage {
  months: CoverageMonth[];
  noDataAccounts: number;
}

function coverageState(value: unknown): CoverageState {
  return (COVERAGE_STATES as readonly unknown[]).includes(value)
    ? (value as CoverageState)
    : "missing";
}

export function coverage(value: unknown): Coverage {
  const r = row(value);
  return {
    months: rows(r.months).map((m) => ({
      account: str(m.account),
      accountLabel: str(m.account_label) || str(m.account),
      month: str(m.month),
      state: coverageState(m.state),
      reason: optional(m.reason),
    })),
    noDataAccounts: int(r.no_data_accounts),
  };
}

// --- finance.openitem.list ----------------------------------------------------------

export interface OpenItems {
  items: OpenItem[];
  /** Count per kind, in the module's own kind order. */
  summary: { kind: string; count: number }[];
}

export function openItems(value: unknown): OpenItems {
  const r = row(value);
  return {
    items: rows(r.items).map(openItem),
    summary: rows(r.summary).map((s) => ({ kind: str(s.kind), count: int(s.count) })),
  };
}

// --- dates --------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-11-24" as "Nov 24", with the year when it is not `thisYear`. A calendar
 * date carries no time zone, so none is applied. */
export function shortDate(iso: string | null, thisYear?: number): string {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  const day = `${MONTHS[m - 1]} ${d}`;
  return thisYear === y ? day : `${day}, ${y}`;
}

export function monthLabel(iso: string): string {
  const m = Number(iso.slice(5, 7));
  return MONTHS[m - 1] ?? iso;
}

/** "in 3 days", "today", "2 days overdue". */
export function relativeDays(days: number | null): string {
  if (days === null) return "no due date";
  if (days === 0) return "due today";
  if (days === 1) return "due tomorrow";
  if (days > 1) return `in ${days} days`;
  return days === -1 ? "1 day overdue" : `${-days} days overdue`;
}

export function words(code: string): string {
  return code.replace(/_/g, " ");
}
