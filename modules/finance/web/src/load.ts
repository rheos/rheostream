import type { ShellApi } from "@rheo-stream/web-contract/screen";

import {
  coverage,
  documents,
  openItems,
  owed,
  sinks,
  statusFacts,
  statusLines,
  traces,
  type Coverage,
  type Document,
  type OpenItems,
  type Owed,
  type Sink,
  type StatusFacts,
  type StatusLine,
  type Trace,
} from "./data";

/**
 * The reads these screens call: seven of the eight finance registers in core
 * (`finance.txn.list` has no screen yet). All are READ, owner-only, and leave out
 * every restricted field. Nothing here submits, so the package declares no form
 * and no submit operation.
 */
export type Operation =
  | "finance.status.get"
  | "finance.sink.state"
  | "finance.owed.list"
  | "finance.owed.trace"
  | "finance.document.list"
  | "finance.coverage.get"
  | "finance.openitem.list";

export type Loaded<T> =
  | { state: "ok"; value: T }
  | { state: "refused"; code: string; text: string }
  | { state: "unavailable" };

async function read<T>(
  shell: ShellApi,
  operation: Operation,
  input: Readonly<Record<string, unknown>>,
  parse: (value: unknown) => T,
): Promise<Loaded<T>> {
  const outcome = await shell.call(operation, input);
  switch (outcome.state) {
    case "ok":
      return { state: "ok", value: parse(outcome.result) };
    case "refused":
      return { state: "refused", code: outcome.code, text: outcome.text };
    default:
      return { state: "unavailable" };
  }
}

export interface Strip {
  lines: StatusLine[];
  facts: StatusFacts;
  sinks: Sink[] | null;
}

/** The status strip every screen shows. A failed sink read leaves the strip up
 * with the sinks unknown; a failed status read fails the strip. */
export async function loadStrip(shell: ShellApi): Promise<Loaded<Strip>> {
  const [lines, sinkState] = await Promise.all([
    read(shell, "finance.status.get", {}, statusLines),
    read(shell, "finance.sink.state", {}, sinks),
  ]);
  if (lines.state !== "ok") return lines;
  return {
    state: "ok",
    value: {
      lines: lines.value,
      facts: statusFacts(lines.value),
      sinks: sinkState.state === "ok" ? sinkState.value : null,
    },
  };
}

export interface OwedState {
  strip: Loaded<Strip>;
  owed: Loaded<Owed>;
  /** Payments and evidence per obligation ref; null when the trace read failed, so a
   * bill never claims "no payments" it could not check. */
  traces: Map<string, Trace> | null;
}

export async function loadOwed(shell: ShellApi): Promise<OwedState> {
  const [strip, list, trace] = await Promise.all([
    loadStrip(shell),
    read(shell, "finance.owed.list", {}, owed),
    read(shell, "finance.owed.trace", {}, traces),
  ]);
  return {
    strip,
    owed: list,
    traces: trace.state === "ok" ? trace.value : null,
  };
}

export interface AttentionState {
  strip: Loaded<Strip>;
  owed: Loaded<Owed>;
  documents: Loaded<Document[]>;
  coverage: Loaded<Coverage>;
  openItems: Loaded<OpenItems>;
}

export async function loadAttention(shell: ShellApi): Promise<AttentionState> {
  const [strip, list, docs, months, items] = await Promise.all([
    loadStrip(shell),
    read(shell, "finance.owed.list", {}, owed),
    read(shell, "finance.document.list", {}, documents),
    read(shell, "finance.coverage.get", {}, coverage),
    read(shell, "finance.openitem.list", {}, openItems),
  ]);
  return { strip, owed: list, documents: docs, coverage: months, openItems: items };
}

export interface CoverageState {
  strip: Loaded<Strip>;
  year: number;
  coverage: Loaded<Coverage>;
}

export async function loadCoverage(
  shell: ShellApi,
  year: number,
): Promise<CoverageState> {
  const [strip, months] = await Promise.all([
    loadStrip(shell),
    read(shell, "finance.coverage.get", { year }, coverage),
  ]);
  return { strip, year, coverage: months };
}
