import type { ShellApi } from "@rheo-stream/web-contract/screen";

import {
  bucket,
  relativeDays,
  shortDate,
  words,
  type Bucket,
  type Obligation,
  type Trace,
} from "./data";
import { BellIcon, Failure, Footnote, Frame, popoverId } from "./frame";
import type { OwedState } from "./load";
import styles from "./finance.module.css";

const SECTIONS: readonly [Bucket, (window: number) => string][] = [
  ["overdue", () => "Overdue"],
  ["soon", (w) => `Due in the next ${w} days`],
  ["later", () => "Later"],
  ["undated", () => "No due date"],
  ["parked", () => "Presumed paid or disputed"],
];

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The one sentence at the top: what needs paying, by when. */
export function headline(rows: readonly Obligation[], windowDays: number): string {
  const counts = { overdue: 0, soon: 0, later: 0, undated: 0, parked: 0 };
  for (const o of rows) counts[bucket(o, windowDays)] += 1;
  if (!rows.length) return "Nothing owed right now.";
  const parts: string[] = [];
  if (counts.overdue) parts.push(`${plural(counts.overdue, "bill", "bills")} overdue`);
  if (counts.soon) parts.push(`${counts.soon} due in the next ${windowDays} days`);
  if (!parts.length) parts.push(`Nothing due in the next ${windowDays} days`);
  return parts.join(", ") + ".";
}

function Bill({ o, trace, year }: { o: Obligation; trace?: Trace; year: number }) {
  const id = popoverId("bill", o.ref);
  const late = o.daysUntilDue !== null && o.daysUntilDue < 0;
  return (
    <li className={`${styles.bill}${o.beforeFloor ? ` ${styles.floored}` : ""}`}>
      <div className={styles.billMain}>
        <span className={styles.payee}>{o.payeeLabel}</span>
        <span className={styles.period}>{o.periodLabel}</span>
        {o.status !== "open" ? (
          <span className={styles.tag}>{words(o.status)}</span>
        ) : null}
        {o.beforeFloor ? (
          <span className={`${styles.tag} ${styles.tagQuiet}`}>before floor</span>
        ) : null}
      </div>
      <div className={styles.billWhen}>
        {o.daysUntilDue !== null ? (
          <span className={late ? styles.late : undefined}>{relativeDays(o.daysUntilDue)}</span>
        ) : null}
        {o.dueDate ? <span className={styles.faint}>{shortDate(o.dueDate, year)}</span> : null}
        {o.reminderEligible ? <BellIcon title="Reminder in Current" /> : null}
        <Footnote id={id} label={`About ${o.payeeLabel} ${o.periodLabel}`} title={`${o.payeeLabel}, ${o.periodLabel}`}>
          <dl className={styles.facts}>
            <dt>Kind</dt>
            <dd>{words(o.kind)}</dd>
            <dt>Status</dt>
            <dd>{words(o.status)}</dd>
            {o.dueDate ? (
              <>
                <dt>Due</dt>
                <dd>{shortDate(o.dueDate, year)}</dd>
              </>
            ) : null}
            {o.statementDate ? (
              <>
                <dt>Statement</dt>
                <dd>{shortDate(o.statementDate, year)}</dd>
              </>
            ) : null}
            {o.issuedOn ? (
              <>
                <dt>Issued</dt>
                <dd>{shortDate(o.issuedOn, year)}</dd>
              </>
            ) : null}
            <dt>Source</dt>
            <dd className={styles.path}>{o.primarySource || "none"}</dd>
            {o.manualSource ? (
              <>
                <dt>Manual entry</dt>
                <dd className={styles.path}>{o.manualSource}</dd>
              </>
            ) : null}
            <dt>Reminder</dt>
            <dd>
              {o.reminderEligible
                ? "Filed in Current"
                : o.beforeFloor
                  ? "None: due before the reminders floor"
                  : "None"}
            </dd>
          </dl>
          {o.paymentsUnmatchable || o.statementConflict ? (
            <ul className={styles.flags}>
              {o.paymentsUnmatchable ? <li>Its payments cannot be matched automatically.</li> : null}
              {o.statementConflict ? <li>Two copies of this statement disagree.</li> : null}
            </ul>
          ) : null}
          {trace?.presumedBasisKind ? (
            <p>Presumed paid on the basis of {words(trace.presumedBasisKind)}.</p>
          ) : null}
          {trace?.payments.length ? (
            <>
              <h4>Payments applied</h4>
              <ul className={styles.plain}>
                {trace.payments.map((p, i) => (
                  <li key={i}>
                    {shortDate(p.paidOn, year)} from {words(p.source)}, {words(p.state)}
                    {p.auto ? ", matched automatically" : ""}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className={styles.faint}>No payments applied yet.</p>
          )}
        </Footnote>
      </div>
    </li>
  );
}

export function OwedView({ shell, state }: { shell: ShellApi; state: OwedState }) {
  const { owed } = state;
  return (
    <Frame shell={shell} active="owed" strip={state.strip}>
      {owed.state !== "ok" ? (
        <Failure what="what you owe" outcome={owed} />
      ) : (
        <OwedBody state={state} />
      )}
    </Frame>
  );
}

function OwedBody({ state }: { state: OwedState }) {
  if (state.owed.state !== "ok") return null;
  const o = state.owed.value;
  const year = Number(o.today.slice(0, 4)) || undefined;
  const grouped = new Map<Bucket, Obligation[]>();
  for (const row of o.rows) {
    const key = bucket(row, o.windowDays);
    grouped.set(key, [...(grouped.get(key) ?? []), row]);
  }
  // The module sorts by its own overdue figure first; within a group, by date.
  for (const list of grouped.values())
    list.sort((a, b) => (a.dueDate ?? "9999").localeCompare(b.dueDate ?? "9999"));
  const floored = o.rows.filter((r) => r.beforeFloor).length;
  return (
    <>
      <div className={styles.lead}>
        <p className={styles.headline}>{headline(o.rows, o.windowDays)}</p>
        <Footnote id={popoverId("owed-about")} label="About this list" title="About this list">
          <p>
            Open, part-paid, presumed-paid and disputed bills, overdue first, then by due
            date. {o.asOf}.
          </p>
          <p>
            Amounts are not shown in the browser yet: every finance read leaves them out
            until how finance figures are shared is decided.
          </p>
          {floored ? (
            <p>
              {plural(floored, "bill is", "bills are")} due before the reminders floor, so
              {floored === 1 ? " it gets" : " they get"} no reminder in Current.
            </p>
          ) : null}
        </Footnote>
      </div>
      {SECTIONS.map(([key, label]) => {
        const list = grouped.get(key);
        if (!list?.length) return null;
        return (
          <section key={key} className={styles.section} aria-labelledby={`owed-${key}`}>
            <h2 id={`owed-${key}`} className={styles.sectionHeading}>
              {label(o.windowDays)} <span className={styles.count}>{list.length}</span>
            </h2>
            <ul className={styles.bills}>
              {list.map((row) => (
                <Bill key={row.ref} o={row} trace={state.traces.get(row.ref)} year={year ?? 0} />
              ))}
            </ul>
          </section>
        );
      })}
      {o.more ? (
        <p className={styles.faint}>
          {plural(o.more, "more bill", "more bills")} past the list&apos;s limit, not shown.
        </p>
      ) : null}
    </>
  );
}
