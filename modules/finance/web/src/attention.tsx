import type { ReactNode } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";

import {
  ATTENTION_STATUSES,
  localDay,
  monthLabel,
  shortDate,
  words,
  type CoverageMonth,
  type Document,
  type OpenItem,
} from "./data";
import { Failure, Frame } from "./frame";
import type { AttentionState } from "./load";
import { money, Names, ownLabel, statusText, UNNAMED_ACCOUNT, UNNAMED_PAYEE } from "./names";
import styles from "./finance.module.css";

/** At most this many rows open under one group; the count above stays whole. */
export const GROUP_ROWS = 50;

interface Group {
  id: string;
  label: string;
  count: number | null;
  /** Shown beside the count. */
  note?: string;
  rows: ReactNode[];
  /** Shown when there are no rows to list. */
  empty?: string;
}

function period(start: string | null, end: string | null, year: number): string {
  if (!start && !end) return "";
  if (start && end && start !== end) return `${shortDate(start, year)} to ${shortDate(end, year)}`;
  return shortDate(start ?? end, year);
}

/** The folder a document sits in under its kind directory: the issuer's or the
 * account's key, which is looked up, never shown. */
function holder(path: string): string | null {
  return path.split("/")[1] ?? null;
}

/** Every slug and label the screen's reads carry. */
export function namesOf(state: AttentionState): Names {
  const names = new Names();
  if (state.owed.state === "ok") {
    for (const row of state.owed.value.rows) names.add(row.payeeSlug, row.payeeLabel);
    for (const item of state.owed.value.credits.items) names.add(item.payeeSlug, item.payeeLabel);
    // A gap's issuer has no payee label in the read: it is a key, so it is unnamed
    // unless a payee above carries a label for it.
    for (const gap of state.owed.value.gaps) names.add(gap.issuer, gap.issuer);
  }
  if (state.coverage.state === "ok")
    for (const m of state.coverage.value.months) names.add(m.account, m.accountLabel);
  if (state.openItems.state === "ok")
    for (const item of state.openItems.value.items) names.add(item.payeeSlug, item.payeeLabel);
  return names;
}

function who(item: OpenItem, names: Names): string {
  if (item.payeeSlug) return names.bySlug(item.payeeSlug, UNNAMED_PAYEE);
  return names.label(item.accountLabel, item.accountLabel ? UNNAMED_ACCOUNT : "");
}

function itemRow(item: OpenItem, year: number, names: Names): ReactNode {
  const figure = item.currency ? money(item.amount === null ? null : Math.abs(item.amount), item.currency) : null;
  return (
    <li key={item.lineRef}>
      <span>{shortDate(item.occurredOn, year)}</span>
      <span>{who(item, names)}</span>
      {figure ? <span className={styles.num}>{figure}</span> : null}
      <span className={styles.faint}>{words(item.reason || item.state)}</span>
    </li>
  );
}

function docRow(d: Document, year: number, names: Names, gap: boolean): ReactNode {
  const name = names.bySlug(holder(d.path), "Unnamed issuer", "");
  return (
    <li key={d.path}>
      {name ? <span>{name}</span> : null}
      <span className={styles.faint}>
        {[words(d.kind), period(d.periodStart, d.periodEnd, year), gap ? "not on the owed list" : ""]
          .filter(Boolean)
          .join(", ")}
      </span>
    </li>
  );
}

/** Missing months, one row per account: "Jan, Feb, Mar 2026". */
function missingRows(months: readonly CoverageMonth[], names: Names): ReactNode[] {
  const byAccount = new Map<string, string[]>();
  for (const m of months) byAccount.set(m.account, [...(byAccount.get(m.account) ?? []), m.month]);
  return [...byAccount.entries()].map(([account, list]) => {
    const years = new Map<string, string[]>();
    for (const month of list.sort()) {
      const y = month.slice(0, 4);
      years.set(y, [...(years.get(y) ?? []), monthLabel(month)]);
    }
    return (
      <li key={account}>
        <span>{names.bySlug(account, UNNAMED_ACCOUNT)}</span>
        <span className={styles.faint}>
          {[...years.entries()].map(([y, m]) => `${m.join(", ")} ${y}`).join("; ")}
        </span>
      </li>
    );
  });
}

export function groups(state: AttentionState, year: number, timeZone?: string): Group[] {
  const names = namesOf(state);
  const out: Group[] = [];
  if (state.owed.state === "ok") {
    const o = state.owed.value;
    const unconfirmed = o.rows.filter((r) => r.reportedUnconfirmed);
    // Only when there is one: an empty "all clear" here would claim more than it knows.
    if (unconfirmed.length)
      out.push({
        id: "reported",
        label: "Marked paid, payment not found",
        count: unconfirmed.length,
        note: "not on later statements",
        rows: unconfirmed.map((r) => {
          const on = localDay(r.reportedAt, timeZone);
          const figure = money(r.thresholdRemaining ?? r.amountDue, o.currency);
          return (
            <li key={r.ref}>
              <span>
                {ownLabel(r.payeeSlug, r.payeeLabel) ?? UNNAMED_PAYEE}, {r.periodLabel}
              </span>
              {figure ? <span className={styles.num}>{figure}</span> : null}
              {on ? <span className={styles.faint}>marked paid {shortDate(on, year)}</span> : null}
            </li>
          );
        }),
      });
  }
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
      empty: "The module counts these; it has no list of them to show here.",
    });
  }
  const gapPaths = new Set(state.owed.state === "ok" ? state.owed.value.gaps.map((g) => g.path) : []);
  if (state.documents.state === "ok") {
    const docs = state.documents.value;
    for (const status of ATTENTION_STATUSES) {
      const list = docs.filter((d) => d.status === status);
      const gaps = list.filter((d) => gapPaths.has(d.path)).length;
      out.push({
        id: `doc-${status}`,
        label:
          status === "needs_manual_entry"
            ? "Documents needing manual entry"
            : status === "rejected"
              ? "Rejected documents"
              : "Unparsed documents",
        count: list.length,
        note: gaps ? `${gaps} ${gaps === 1 ? "is a bill" : "are bills"} not on the owed list` : undefined,
        rows: list.map((d) => docRow(d, year, names, gapPaths.has(d.path))),
      });
    }
  }
  if (state.owed.state === "ok") {
    const o = state.owed.value;
    const sums = o.credits.sums
      .map((c) => money(c.amount, c.currency))
      .filter((c): c is string => c !== null);
    out.push({
      id: "credits",
      label: "Credits not applied",
      count: o.credits.count,
      note: sums.length ? sums.join(", ") : undefined,
      rows: o.credits.items.map((item) => itemRow(item, year, names)),
    });
  }
  if (state.coverage.state === "ok") {
    const missing = state.coverage.value.months.filter((m) => m.state === "missing");
    out.push({
      id: "missing",
      label: "Missing account-months",
      count: missing.length,
      rows: missingRows(missing, names),
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
        rows: items.filter((i) => i.kind === kind).map((item) => itemRow(item, year, names)),
      });
    }
  }
  // The names come from three reads; with any of them missing, a zero is not "all clear",
  // so the group is left out rather than counted.
  const namesComplete = [state.owed, state.coverage, state.openItems].every((r) => r.state === "ok");
  if (namesComplete || names.unnamedCount > 0)
    out.push({
      id: "unnamed",
      label: "Payees and accounts without a name",
      count: names.unnamedCount,
      rows: [],
      empty:
        "They show as unnamed until you give each a label in your payees or accounts file.",
    });
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
        <p className={styles.faint}>{group.empty ?? "No rows to list."}</p>
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
  const all = groups(state, year, shell.timeZone);
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
  const checks =
    state.strip.state === "ok"
      ? (state.strip.value.lines.find((l) => l.lineNo === 9) ?? null)
      : null;
  return (
    <Frame shell={shell} active="attention" strip={state.strip}>
      <div className={styles.lead}>
        <p className={styles.headline}>
          {open.length
            ? `${open.length} ${open.length === 1 ? "list needs" : "lists need"} a look${
                failed.length ? "; some lists could not be read" : ""
              }.`
            : failed.length
              ? "Some lists could not be read."
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
          <h2 id="checks" className={styles.sectionHeading}>
            Data checks
          </h2>
          <ul className={styles.plain}>
            {statusText(checks)
              .split("; ")
              .map((part) => (
                <li key={part}>{part}</li>
              ))}
          </ul>
        </section>
      ) : null}
      {clear.length ? (
        <p className={styles.clear}>All clear: {clear.map((g) => g.label.toLowerCase()).join(", ")}.</p>
      ) : null}
    </Frame>
  );
}
