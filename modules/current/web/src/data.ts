import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";

/**
 * Wire shapes of Current's read operations (`rheo_current.contracts`), narrowed
 * from the untyped `OperationOutcome.result`. Each parser tolerates a missing or
 * mistyped field by falling back to an empty value, so one odd record cannot take
 * a whole screen down; the service remains the authority on the real shape.
 */
export type Status = "open" | "waiting" | "snoozed" | "done";
export type Priority = "critical" | "high" | "normal" | "low";

export interface ItemSummary {
  ref: string;
  title: string;
  status: Status;
  priority: Priority;
  area: string | null;
  projectRef: string | null;
  dueOn: string | null;
  snoozedUntil: string | null;
  woke: boolean;
  overdue: boolean;
  blocked: boolean;
  eventCount: number;
  revision: number;
  updatedAt: string;
}
export interface Neighbour {
  ref: string;
  title: string;
  status: Status;
}
export interface Comment {
  id: string;
  kind: string;
  body: string;
  authorKind: string;
  authorId: string | null;
  createdAt: string;
}
export interface ItemDetail extends ItemSummary {
  body: string;
  provenance: string;
  sourceKind: string | null;
  sourceRef: string | null;
  createdAt: string;
  closedAt: string | null;
  project: { ref: string; title: string; status: string } | null;
  blockers: Neighbour[];
  blocking: Neighbour[];
  comments: Comment[];
}
export interface Project {
  ref: string;
  title: string;
  description: string;
  status: "active" | "paused" | "done";
  area: string | null;
  dueOn: string | null;
  revision: number;
  updatedAt: string;
  counts: Record<Status, number>;
}
export interface Area {
  key: string;
  label: string;
  position: number;
  retired: boolean;
}
export interface Summary {
  byStatus: Record<Status, number>;
  active: number;
  woke: number;
  overdue: number;
  blocked: number;
  byArea: { area: string | null; active: number }[];
}

type Row = Record<string, unknown>;
function row(value: unknown): Row {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Row)
    : {};
}
function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.map(row) : [];
}
export function str(value: unknown): string {
  return typeof value === "string"
    ? value
    : typeof value === "number"
      ? String(value)
      : "";
}
function optional(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}
function int(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
export const STATUSES = ["open", "waiting", "snoozed", "done"] as const;
export const PRIORITIES = ["critical", "high", "normal", "low"] as const;
function status(value: unknown): Status {
  return (STATUSES as readonly unknown[]).includes(value)
    ? (value as Status)
    : "open";
}
function priority(value: unknown): Priority {
  return (PRIORITIES as readonly unknown[]).includes(value)
    ? (value as Priority)
    : "normal";
}
function counts(value: unknown): Record<Status, number> {
  const source = row(value);
  return {
    open: int(source.open),
    waiting: int(source.waiting),
    snoozed: int(source.snoozed),
    done: int(source.done),
  };
}

export function itemSummary(value: unknown): ItemSummary {
  const r = row(value);
  return {
    ref: str(r.ref),
    title: str(r.title),
    status: status(r.status),
    priority: priority(r.priority),
    area: optional(r.area),
    projectRef: optional(r.project_ref),
    dueOn: optional(r.due_on),
    snoozedUntil: optional(r.snoozed_until),
    woke: r.woke === true,
    overdue: r.overdue === true,
    blocked: r.blocked === true,
    eventCount: int(r.event_count),
    revision: int(r.revision),
    updatedAt: str(r.updated_at),
  };
}
function neighbour(value: unknown): Neighbour {
  const r = row(value);
  return { ref: str(r.ref), title: str(r.title), status: status(r.status) };
}
export function itemDetail(value: unknown): ItemDetail {
  const r = row(value);
  const project = row(r.project);
  return {
    ...itemSummary(r),
    body: str(r.body),
    provenance: str(r.provenance),
    sourceKind: optional(r.source_kind),
    sourceRef: optional(r.source_ref),
    createdAt: str(r.created_at),
    closedAt: optional(r.closed_at),
    project: project.ref
      ? {
          ref: str(project.ref),
          title: str(project.title),
          status: str(project.status),
        }
      : null,
    blockers: rows(r.blockers).map(neighbour),
    blocking: rows(r.blocking).map(neighbour),
    comments: rows(r.comments).map((c) => ({
      id: str(c.id),
      kind: str(c.kind),
      body: str(c.body),
      authorKind: str(c.author_kind),
      authorId: optional(c.author_id),
      createdAt: str(c.created_at),
    })),
  };
}
export function project(value: unknown): Project {
  const r = row(value);
  const projectStatus = str(r.status);
  return {
    ref: str(r.ref),
    title: str(r.title),
    description: str(r.description),
    status:
      projectStatus === "paused" || projectStatus === "done"
        ? projectStatus
        : "active",
    area: optional(r.area),
    dueOn: optional(r.due_on),
    revision: int(r.revision),
    updatedAt: str(r.updated_at),
    counts: counts(r.item_counts),
  };
}
export function area(value: unknown): Area {
  const r = row(value);
  return {
    key: str(r.key),
    label: str(r.label),
    position: int(r.position),
    retired: r.retired === true,
  };
}
export function summary(value: unknown): Summary {
  const r = row(value);
  return {
    byStatus: counts(r.by_status),
    active: int(r.active),
    woke: int(r.woke),
    overdue: int(r.overdue),
    blocked: int(r.blocked),
    byArea: rows(r.by_area).map((a) => ({
      area: optional(a.area),
      active: int(a.active),
    })),
  };
}
/** The `items` array of a page result, or none when the outcome is not ok. */
export function page<T>(
  outcome: OperationOutcome | null,
  parse: (value: unknown) => T,
): { items: T[]; nextOffset: number | null } {
  if (outcome?.state !== "ok") return { items: [], nextOffset: null };
  const r = row(outcome.result);
  return {
    items: rows(r.items).map(parse),
    nextOffset: typeof r.next_offset === "number" ? r.next_offset : null,
  };
}

// --- reads ------------------------------------------------------------------------

const READS = [
  "current.item.list",
  "current.item.get",
  "current.project.list",
  "current.project.get",
  "current.area.list",
  "current.summary.get",
] as const;
export type ReadOperation = (typeof READS)[number];
/** Current's own read operations only; the shell's allowlist refuses anything else. */
export function read(
  shell: ShellApi,
  name: ReadOperation,
  input: unknown = {},
): Promise<OperationOutcome> {
  return READS.includes(name)
    ? shell.call(name, input)
    : Promise.resolve({ state: "unavailable" });
}

// --- labels and dates ---------------------------------------------------------------

export const STATUS_LABEL: Record<Status, string> = {
  open: "Open",
  waiting: "Waiting",
  snoozed: "Snoozed",
  done: "Done",
};
export const PRIORITY_LABEL: Record<Priority, string> = {
  critical: "Critical",
  high: "High",
  normal: "Normal",
  low: "Low",
};
const AUTHOR_LABEL: Record<string, string> = {
  account: "Person",
  token: "Agent",
  operator: "Operator",
  system: "System",
  connection: "Connection",
};
/** Who wrote a comment, from the record's own author fields. The id shows as its
 * last eight characters: a UUIDv7 starts with a timestamp, so its head repeats. */
export function author(comment: Comment): string {
  const kind = AUTHOR_LABEL[comment.authorKind] ?? comment.authorKind;
  return comment.authorId ? `${kind} ${comment.authorId.slice(-8)}` : kind;
}
export const PROVENANCE_LABEL: Record<string, string> = {
  manual: "Added by hand",
  agent: "Added by an agent",
  email: "From email",
  alert: "From an alert",
  handoff: "Handed off from another module",
};

/** A YYYY-MM-DD calendar date as "Oct 10, 2026"; empty for anything else. */
export function day(value: string | null): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
  const d = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(d.valueOf())
    ? ""
    : new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeZone: "UTC",
      }).format(d);
}
export function pageOffset(value: string | undefined): number {
  const n = Number(value ?? 0);
  return Number.isInteger(n) && n >= 0 && n <= 100000 ? n : 0;
}

/** Labels by area key, retired areas included, so a retired area stays readable. */
export function areaLabels(areas: readonly Area[]): Map<string, string> {
  return new Map(areas.map((a) => [a.key, a.label]));
}
export function areaLabel(
  labels: ReadonlyMap<string, string>,
  key: string | null,
): string {
  return key ? (labels.get(key) ?? key.replaceAll("_", " ")) : "";
}

// --- status transitions and snooze --------------------------------------------------

/** The service's own transition table (`rheo_current.items.TRANSITIONS`). */
export const TRANSITIONS: Record<Status, readonly Status[]> = {
  open: ["waiting", "snoozed", "done"],
  waiting: ["open", "snoozed", "done"],
  snoozed: ["open", "waiting", "done"],
  done: ["open"],
};
/** Targets a control may offer: allowed by the service and not a no-op. A snoozed
 * item may be snoozed again to a new time, so snoozed is always offered from it. */
export function allowedTargets(current: Status): Status[] {
  const targets = [...TRANSITIONS[current]];
  return current === "snoozed" ? [...targets, "snoozed"] : targets;
}

export const SNOOZE_PICKS = [
  { id: "hour", label: "1 hour" },
  { id: "evening", label: "This evening, 7pm" },
  { id: "tomorrow", label: "Tomorrow, 9am" },
  { id: "saturday", label: "Saturday, 9am" },
  { id: "monday", label: "Monday, 9am" },
] as const;
export type SnoozePick = (typeof SNOOZE_PICKS)[number]["id"];

/** The runtime's own zone: the browser's, where the snooze controls run. */
function ownZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
/** The wall clock an instant shows in a zone. */
function wall(ms: number, zone: string) {
  const parts = new Intl.DateTimeFormat("en", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
    timeZone: zone,
  }).formatToParts(ms);
  const n = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((part) => part.type === type)?.value);
  return { y: n("year"), m: n("month"), d: n("day"), h: n("hour"), min: n("minute") };
}
/** How far a zone's clock is ahead of UTC at an instant, in ms. */
function offset(ms: number, zone: string): number {
  const w = wall(ms, zone);
  return Date.UTC(w.y, w.m - 1, w.d, w.h, w.min) - (ms - (ms % 60000));
}
/** The instant a wall-clock time names in a zone (days and hours may overflow,
 * as with `Date.UTC`). A time the clocks fall back over means its first
 * occurrence; a time a spring-forward skips moves past the gap by its length
 * (2:30 becomes 3:30), so a snooze never wakes before the time asked for. */
function zoned(y: number, m: number, d: number, h: number, min: number, zone: string): Date {
  const target = Date.UTC(y, m - 1, d, h, min);
  const day = 24 * 60 * 60 * 1000;
  const before = offset(target - day, zone);
  const after = offset(target + day, zone);
  const fits = [target - before, target - after]
    .filter((t) => target - t === offset(t, zone))
    .sort((a, b) => a - b);
  return new Date(fits[0] ?? target - before);
}
function at(now: Date, addDays: number, hour: number, zone: string): Date {
  const w = wall(now.getTime(), zone);
  return zoned(w.y, w.m, w.d + addDays, hour, 0, zone);
}
function next(now: Date, weekday: number, hour: number, zone: string): Date {
  const w = wall(now.getTime(), zone);
  for (let add = 0; add <= 7; add += 1) {
    const d = at(now, add, hour, zone);
    if (new Date(Date.UTC(w.y, w.m - 1, w.d + add)).getUTCDay() === weekday && d > now)
      return d;
  }
  return at(now, 7, hour, zone);
}
/**
 * The wake time a quick pick means on the viewer's clock, in `zone` (the shell's,
 * the one times are shown in; the browser's own when none is given). `now` is read
 * in the browser. "This evening" has no answer once 7pm has passed, and the
 * control hides it then.
 */
export function snoozeAt(pick: SnoozePick, now: Date, zone = ownZone()): Date | null {
  switch (pick) {
    case "hour":
      return new Date(now.getTime() + 60 * 60 * 1000);
    case "evening": {
      const evening = at(now, 0, 19, zone);
      return evening > now ? evening : null;
    }
    case "tomorrow":
      return at(now, 1, 9, zone);
    case "saturday":
      return next(now, 6, 9, zone);
    case "monday":
      return next(now, 1, 9, zone);
  }
}
/** A `datetime-local` value read as a wall-clock time in `zone`; null unless it is
 * a real time after `now`. */
export function customSnooze(value: string, now: Date, zone = ownZone()): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!m) return null;
  const [y, mo, d, h, min] = [m[1], m[2], m[3], m[4], m[5]].map(Number) as [
    number,
    number,
    number,
    number,
    number,
  ];
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || min > 59) return null;
  const when = zoned(y, mo, d, h, min, zone);
  return Number.isNaN(when.valueOf()) || when <= now ? null : when;
}

/** A key for a new area derived from its label, in the service's key grammar. */
export function areaKey(label: string): string {
  const key = label
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .replace(/_+$/, "")
    .slice(0, 40)
    .replace(/_+$/, "");
  return key;
}

/** What a refusal means for the person who caused it. */
export function refusalText(code: string, text: string): string {
  switch (code) {
    case "record_stale":
      return "This changed since the page loaded. Your entry is still here: reload to get the latest version, then save again.";
    case "dependency_cycle":
      return "That would make a loop: these two items already depend on each other, directly or through other items.";
    case "transition_not_allowed":
      return "That status change is not allowed from where the item is now. Reload to see its current status.";
    case "snooze_invalid":
      return "Choose a snooze time in the future.";
    case "area_retired":
      return "That area is retired. Choose another.";
    case "area_unknown":
      return "That area no longer exists. Reload and choose another.";
    case "area_exists":
      return "An area with that key already exists.";
    case "not_found":
      return "This record is no longer available. It may have been deleted.";
    default:
      return text || "The change was refused. Check your access and try again.";
  }
}
