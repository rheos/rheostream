import type { ReactNode } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";

import {
  ATTENTION_STATUSES,
  monthLabel,
  shortDate,
  words,
  type CoverageMonth,
  type OpenItem,
} from "./data";
import { Failure, Frame } from "./frame";
import type { AttentionState } from "./load";
import styles from "./finance.module.css";

/** At most this many rows open under one group; the count above stays whole. */
export const GROUP_ROWS = 50;

interface Group {
  id: string;
  label: string;
  count: number | null;
  /** Shown beside the count when the group cannot list its rows. */
  note?: string;
  rows: ReactNode[];
}

function period(start: string | null, end: string | null, year: number): string {
  if (!start && !end) return "";
  if (start && end && start !== end) return `${shortDate(start, year)} to ${shortDate(end, year)}`;
  return shortDate(start ?? end, year);
}

function itemRow(item: OpenItem, year: number): ReactNode {
  const who = item.payeeLabel ?? item.accountLabel ?? "";
  return (
    <li key={item.lineRef}>
      <span>{shortDate(item.occurredOn, year)}</span>
      <span>{who}</span>
      <span className={styles.faint}>{words(item.reason || item.state)}</span>
    </li>
  );
}

/** Missing months, one row per account: "Jan, Feb, Mar 2026". */
function missingRows(months: readonly CoverageMonth[]): ReactNode[] {
  const byAccount = new Map<string, { label: string; months: string[] }>();
  for (const m of months) {
    const entry = byAccount.get(m.account) ?? { label: m.accountLabel, months: [] };
    entry.months.push(m.month);
    byAccount.set(m.account, entry);
  }
  return [...byAccount.entries()].map(([account, { label, months: list }]) => {
    const years = new Map<string, string[]>();
    for (const month of list.sort()) {
      const y = month.slice(0, 4);
      years.set(y, [...(years.get(y) ?? []), monthLabel(month)]);
    }
    return (
      <li key={account}>
        <span>{label}</span>
        <span className={styles.faint}>
          {[...years.entries()].map(([y, names]) => `${names.join(", ")} ${y}`).join("; ")}
        </span>
      </li>
    );
  });
}

export function groups(state: AttentionState, year: number): Group[] {
  const out: Group[] = [];
  if (state.strip.state === "ok") {
    const { confirm, confirmAsOf } = state.strip.value.facts;
    out.push({
      id: "confirm",
      label: "Waiting for you to confirm",
      count: confirm.length ? confirm.reduce((n, c) => n + c.count, 0) : null,
      note: confirmAsOf ? `as of ${shortDate(confirmAsOf, year)}` : "count unavailable",
      rows: confirm
        .filter((c) => c.count > 0)
        .map((c) => (
          <li key={c.kind}>
            <span>{c.kind}</span>
            <span className={styles.num}>{c.count}</span>
          </li>
        )),
    });
  }
  if (state.documents.state === "ok") {
    const docs = state.documents.value;
    for (const status of ATTENTION_STATUSES) {
      const list = docs.filter((d) => d.status === status);
      out.push({
        id: `doc-${status}`,
        label:
          status === "needs_manual_entry"
            ? "Documents needing manual entry"
            : status === "rejected"
              ? "Rejected documents"
              : "Unparsed documents",
        count: list.length,
        rows: list.map((d) => (
          <li key={d.path}>
            <span className={styles.path}>{d.path}</span>
            <span className={styles.faint}>
              {[words(d.kind), period(d.periodStart, d.periodEnd, year)].filter(Boolean).join(", ")}
            </span>
          </li>
        )),
      });
    }
  }
  if (state.owed.state === "ok") {
    const o = state.owed.value;
    out.push({
      id: "gaps",
      label: "Bill gaps: bills not on the owed list",
      count: o.gapCount,
      rows: o.gaps.map((g) => (
        <li key={g.path}>
          <span>{g.issuerLabel}</span>
          <span className={styles.faint}>
            {[period(g.periodStart, g.periodEnd, year), words(g.status)].filter(Boolean).join(", ")}
          </span>
        </li>
      )),
    });
    out.push({
      id: "credits",
      label: "Credits not applied",
      count: o.credits.count,
      rows: o.credits.items.map((item) => itemRow(item, year)),
    });
  }
  if (state.coverage.state === "ok") {
    const missing = state.coverage.value.months.filter((m) => m.state === "missing");
    out.push({
      id: "missing",
      label: "Missing account-months",
      count: missing.length,
      rows: missingRows(missing),
    });
  }
  if (state.openItems.state === "ok") {
    const { items, summary } = state.openItems.value;
    for (const { kind, count } of summary) {
      // Unapplied credits are listed above, from the owed read.
      if (kind === "unapplied_credit") continue;
      out.push({
        id: `open-${kind}`,
        label: words(kind).replace(/^./, (c) => c.toUpperCase()),
        count,
        rows: items.filter((i) => i.kind === kind).map((item) => itemRow(item, year)),
      });
    }
  }
  return out;
}

function GroupView({ group }: { group: Group }) {
  const shown = group.rows.slice(0, GROUP_ROWS);
  return (
    <details className={styles.group}>
      <summary>
        <span className={styles.groupLabel}>{group.label}</span>
        <span className={styles.groupCount}>
          {group.count === null ? "?" : group.count}
          {group.note ? <span className={styles.faint}> {group.note}</span> : null}
        </span>
      </summary>
      {shown.length ? (
        <ul className={styles.rows}>{shown}</ul>
      ) : (
        <p className={styles.faint}>
          {group.id === "confirm"
            ? "The module counts these; it has no list of them to show here."
            : "No rows to list."}
        </p>
      )}
      {group.rows.length > shown.length ? (
        <p className={styles.faint}>
          First {shown.length} of {group.rows.length}.
        </p>
      ) : null}
    </details>
  );
}

export function AttentionView({
  shell,
  state,
  year,
}: {
  shell: ShellApi;
  state: AttentionState;
  year: number;
}) {
  const all = groups(state, year);
  const open = all.filter((g) => g.count === null || g.count > 0);
  const clear = all.filter((g) => g.count === 0);
  const failed = (
    [
      ["documents", state.documents],
      ["bills", state.owed],
      ["coverage", state.coverage],
      ["open items", state.openItems],
    ] as const
  ).filter(([, outcome]) => outcome.state !== "ok");
  const checks = state.strip.state === "ok"
    ? state.strip.value.lines.find((l) => l.lineNo === 9)?.text ?? null
    : null;
  return (
    <Frame shell={shell} active="attention" strip={state.strip}>
      <div className={styles.lead}>
        <p className={styles.headline}>
          {open.length
            ? `${open.length} ${open.length === 1 ? "thing needs" : "things need"} a look.`
            : "Nothing needs a look."}
        </p>
      </div>
      {failed.map(([what, outcome]) => (
        <Failure key={what} what={what} outcome={outcome} />
      ))}
      <div className={styles.groups}>
        {open.map((g) => (
          <GroupView key={g.id} group={g} />
        ))}
      </div>
      {checks ? (
        <section className={styles.section} aria-labelledby="checks">
          <h2 id="checks" className={styles.sectionHeading}>Data checks</h2>
          <ul className={styles.plain}>
            {checks.split("; ").map((part) => (
              <li key={part}>{part}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {clear.length ? (
        <p className={styles.clear}>
          All clear: {clear.map((g) => g.label.toLowerCase()).join(", ")}.
        </p>
      ) : null}
    </Frame>
  );
}
