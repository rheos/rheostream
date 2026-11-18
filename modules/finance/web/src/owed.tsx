import type { ShellApi } from "@rheo-stream/web-contract/screen";

import {
  bucket,
  relativeDays,
  shortDate,
  words,
  type Bucket,
  type Obligation,
  type Owed,
  type Trace,
} from "./data";
import { BellIcon, Failure, Footnote, Frame, popoverId } from "./frame";
import type { OwedState } from "./load";
import { money, ownLabel, UNNAMED_PAYEE } from "./names";
import styles from "./finance.module.css";

const SECTIONS: readonly [Bucket, (window: number) => string][] = [
  ["overdue", () => "Overdue"],
  ["soon", (w) => `Due in the next ${w} days`],
  ["later", () => "Later"],
  ["covered", () => "Past due, minimum paid"],
  ["undated", () => "No due date"],
  ["parked", () => "Presumed paid or disputed"],
];

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The one sentence at the top: what needs paying, by when, in money when the
 * figures are there and in bills when they are not. */
export function headline(o: Owed): string {
  const counts: Record<Bucket, number> = {
    overdue: 0,
    covered: 0,
    soon: 0,
    later: 0,
    undated: 0,
    parked: 0,
  };
  for (const row of o.rows) counts[bucket(row, o.windowDays)] += 1;
  if (!o.rows.length) return "Nothing owed right now.";
  const overdue = money(o.overdue, o.currency);
  const due = money(o.dueInWindow, o.currency);
  const parts: string[] = [];
  if (counts.overdue)
    parts.push(overdue ? `${overdue} overdue` : `${plural(counts.overdue, "bill", "bills")} overdue`);
  if (counts.soon)
    parts.push(
      due
        ? `${due} due in the next ${o.windowDays} days`
        : `${counts.soon} due in the next ${o.windowDays} days`,
    );
  if (!parts.length) parts.push(`Nothing due in the next ${o.windowDays} days`);
  return parts.join(", ") + ".";
}

/** The figure a row leads with: what still has to be paid to keep it in good
 * standing (a card's minimum), else what is left of it. */
function lead(o: Obligation, currency: string): string | null {
  if (o.kind === "card_statement" && o.thresholdRemaining !== null && o.thresholdRemaining > 0)
    return money(o.thresholdRemaining, currency);
  return money(o.remaining ?? o.amountDue, currency);
}

function Bill({
  o,
  trace,
  year,
  windowDays,
  currency,
}: {
  o: Obligation;
  trace?: Trace;
  year: number;
  windowDays: number;
  currency: string;
}) {
  const id = popoverId("bill", o.ref);
  const where = bucket(o, windowDays);
  const late = where === "overdue";
  const name = ownLabel(o.payeeSlug, o.payeeLabel) ?? UNNAMED_PAYEE;
  const figure = lead(o, currency);
  const isCard = o.kind === "card_statement";
  return (
    <li className={`${styles.bill}${o.beforeFloor ? ` ${styles.floored}` : ""}`}>
      <div className={styles.billMain}>
        <span className={styles.payee}>{name}</span>
        <span className={styles.period}>{o.periodLabel}</span>
        {o.status !== "open" ? <span className={styles.tag}>{words(o.status)}</span> : null}
        {o.beforeFloor ? (
          <span className={`${styles.tag} ${styles.tagQuiet}`}>before floor</span>
        ) : null}
      </div>
      <div className={styles.billWhen}>
        {figure ? (
          <span className={`${styles.amount}${late ? ` ${styles.late}` : ""}`}>
            {isCard && o.thresholdRemaining ? `${figure} min` : figure}
          </span>
        ) : null}
        {o.daysUntilDue !== null && where !== "parked" ? (
          <span className={late ? styles.late : styles.faint}>
            {where === "covered" ? "past due" : relativeDays(o.daysUntilDue)}
          </span>
        ) : null}
        {o.dueDate ? <span className={styles.faint}>{shortDate(o.dueDate, year)}</span> : null}
        {o.reminderEligible ? <BellIcon title="Gets a reminder in Current" /> : null}
        <Footnote id={id} label={`About ${name} ${o.periodLabel}`} title={`${name}, ${o.periodLabel}`}>
          <dl className={styles.facts}>
            <dt>Kind</dt>
            <dd>{words(o.kind)}</dd>
            <dt>Status</dt>
            <dd>{words(o.status)}</dd>
            {money(o.amountDue, currency) ? (
              <>
                <dt>{isCard ? "Statement balance" : "Amount due"}</dt>
                <dd className={styles.num}>{money(o.amountDue, currency)}</dd>
              </>
            ) : null}
            {isCard && money(o.minimumDue, currency) ? (
              <>
                <dt>Minimum</dt>
                <dd className={styles.num}>{money(o.minimumDue, currency)}</dd>
              </>
            ) : null}
            {money(o.paid, currency) && o.paid ? (
              <>
                <dt>Paid</dt>
                <dd className={styles.num}>{money(o.paid, currency)}</dd>
              </>
            ) : null}
            {money(o.remaining, currency) ? (
              <>
                <dt>Left to pay</dt>
                <dd className={styles.num}>{money(o.remaining, currency)}</dd>
              </>
            ) : null}
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
            <dt>From</dt>
            <dd>{o.manual ? "Your manual entry" : "A parsed document"}</dd>
            <dt>Reminder</dt>
            <dd>
              {o.reminderEligible
                ? "Eligible for a reminder in Current"
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
                    {shortDate(p.paidOn, year)}
                    {money(p.applied, p.currency || currency)
                      ? `, ${money(p.applied, p.currency || currency)}`
                      : ""}
                    , {words(p.state)}
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
      {owed.state !== "ok" ? <Failure what="what you owe" outcome={owed} /> : <OwedBody state={state} />}
    </Frame>
  );
}

function OwedBody({ state }: { state: OwedState }) {
  if (state.owed.state !== "ok") return null;
  const o = state.owed.value;
  const year = Number(o.today.slice(0, 4)) || 0;
  const grouped = new Map<Bucket, Obligation[]>();
  for (const row of o.rows) {
    const key = bucket(row, o.windowDays);
    grouped.set(key, [...(grouped.get(key) ?? []), row]);
  }
  // The module sorts by its own overdue figure first; within a group, by date.
  for (const list of grouped.values())
    list.sort((a, b) => (a.dueDate ?? "9999").localeCompare(b.dueDate ?? "9999"));
  const floored = o.rows.filter((r) => r.beforeFloor).length;
  const carried = money(o.carried, o.currency);
  const credits = o.credits.sums
    .map((c) => money(c.amount, c.currency))
    .filter((c): c is string => c !== null);
  return (
    <>
      <div className={styles.lead}>
        <p className={styles.headline}>{headline(o)}</p>
        <Footnote id={popoverId("owed-about")} label="About this list" title="About this list">
          <p>
            Open, part-paid, presumed-paid and disputed bills, overdue first, then by due
            date. {o.asOf}.
          </p>
          {carried && o.carried ? (
            <p>
              {carried} more is carried on card balances past their minimums, not counted
              as due.
            </p>
          ) : null}
          {credits.length ? <p>Credits not yet applied: {credits.join(", ")}.</p> : null}
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
                <Bill
                  key={row.ref}
                  o={row}
                  trace={state.traces.get(row.ref)}
                  year={year}
                  windowDays={o.windowDays}
                  currency={o.currency}
                />
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
