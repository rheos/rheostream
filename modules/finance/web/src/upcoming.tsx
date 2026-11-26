import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { shortDate, words, type Charge, type Upcoming } from "./data";
import { Failure, Footnote, Frame, popoverId } from "./frame";
import type { UpcomingState } from "./load";
import { money, ownLabel, UNNAMED_ACCOUNT, UNNAMED_PAYEE } from "./names";
import styles from "./finance.module.css";

/**
 * What is coming up, by date: bills due (from Owed, exact) and recurring charges the
 * ledger predicts (estimates), each on the account it hits. Charges the ledger
 * expected before today, past the end of the loaded statements, sit apart as
 * probably already charged. A `?account=` query keeps one account's rows.
 */

/** Money with its code when it is not the home currency: the narrow symbol writes US
 * and Canadian dollars alike. */
export function cash(value: number | null, currency: string): string | null {
  const text = money(value, currency);
  if (text === null || currency === "CAD" || text.endsWith(currency)) return text;
  return `${text} ${currency}`;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "today", "tomorrow", "in 3 days", "4 days ago". */
export function when(days: number): string {
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days > 1) return `in ${days} days`;
  return days === -1 ? "yesterday" : `${-days} days ago`;
}

function accountName(c: { account: string | null; accountLabel: string | null }): string | null {
  if (c.account === null) return null;
  return ownLabel(c.account, c.accountLabel) ?? UNNAMED_ACCOUNT;
}

function chargeName(c: Charge): string {
  if (c.kind === "due") {
    const payee = ownLabel(c.payeeSlug, c.label) ?? UNNAMED_PAYEE;
    return c.obligationKind === "card_statement" ? `${payee} payment` : payee;
  }
  return c.label || "Unlabelled charge";
}

/** The figure a row shows: the exact amount for a bill, the latest charge for a
 * prediction, "about" when its recent charges differ. */
export function figure(c: Charge): string | null {
  const text = cash(c.amount, c.currency);
  if (text === null) return null;
  if (c.kind === "due" && c.obligationKind === "card_statement") return `${text} min`;
  return c.amountLow !== null && c.amountHigh !== null ? `about ${text}` : text;
}

/** The headline: what the window holds, in money when the figures are there. */
export function headline(u: Upcoming, shown: readonly Charge[]): string {
  const coming = shown.filter((c) => c.state !== "likely_charged");
  if (!coming.length) return `Nothing expected in the next ${u.windowDays} days.`;
  const sums = new Map<string, number>();
  let known = true;
  for (const c of coming) {
    if (c.amount === null) known = false;
    else sums.set(c.currency, (sums.get(c.currency) ?? 0) + c.amount);
  }
  const amounts = [...sums.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, total]) => cash(total, currency))
    .filter((t): t is string => t !== null);
  const count = plural(coming.length, "charge", "charges");
  return known && amounts.length
    ? `${amounts.join(" and ")} over ${count} in the next ${u.windowDays} days.`
    : `${count} in the next ${u.windowDays} days.`;
}

function Row({ c, year, place }: { c: Charge; year: number; place: string }) {
  const name = chargeName(c);
  const account = accountName(c);
  const amount = figure(c);
  // The row's place and, for a bill, its ref: never an account key, which may be a
  // codename, and unique even when the amounts are withheld.
  const id = popoverId("charge", place, c.obligationRef ?? "");
  const low = cash(c.amountLow, c.currency);
  const high = cash(c.amountHigh, c.currency);
  return (
    <li className={styles.bill}>
      <div className={styles.billMain}>
        <span className={styles.payee}>{name}</span>
        {account ? <span className={styles.period}>{account}</span> : null}
        {c.kind === "due" ? (
          <span className={styles.tag}>bill</span>
        ) : (
          <span className={`${styles.tag} ${styles.tagQuiet}`}>expected</span>
        )}
      </div>
      <div className={styles.billWhen}>
        {amount ? <span className={styles.amount}>{amount}</span> : null}
        <span className={styles.faint}>{when(c.daysAway)}</span>
        <span className={styles.faint}>{shortDate(c.on, year)}</span>
        <Footnote id={id} label={`About ${name} on ${shortDate(c.on, year)}`} title={name}>
          <dl className={styles.facts}>
            <dt>Kind</dt>
            <dd>{c.kind === "due" ? `Bill due (${words(c.obligationKind ?? "bill")})` : "Predicted from past charges"}</dd>
            {account ? (
              <>
                <dt>Account</dt>
                <dd>{account}</dd>
              </>
            ) : null}
            <dt>{c.kind === "due" ? "Due" : "Expected"}</dt>
            <dd>{shortDate(c.on, year)}</dd>
            {c.periodLabel ? (
              <>
                <dt>Period</dt>
                <dd>{c.periodLabel}</dd>
              </>
            ) : null}
            {c.cadence ? (
              <>
                <dt>Repeats</dt>
                <dd>{c.cadence}</dd>
              </>
            ) : null}
            {c.seen !== null ? (
              <>
                <dt>Seen</dt>
                <dd>{plural(c.seen, "time", "times")}</dd>
              </>
            ) : null}
            {c.lastSeen ? (
              <>
                <dt>Last charged</dt>
                <dd>{shortDate(c.lastSeen, year)}</dd>
              </>
            ) : null}
            {low && high ? (
              <>
                <dt>Recent range</dt>
                <dd className={styles.num}>
                  {low} to {high}
                </dd>
              </>
            ) : null}
          </dl>
          {c.kind === "due" ? (
            <p className={styles.faint}>The amount still open on this bill, as Owed shows it.</p>
          ) : (
            <p className={styles.faint}>
              An estimate: the latest charge, repeated {c.cadence ?? "regularly"}. It goes away
              once your statements show the charge stopped.
            </p>
          )}
        </Footnote>
      </div>
    </li>
  );
}

export function UpcomingView({
  shell,
  state,
  account,
}: {
  shell: ShellApi;
  state: UpcomingState;
  account: string | null;
}) {
  const { upcoming } = state;
  return (
    <Frame shell={shell} active="upcoming" strip={state.strip}>
      {upcoming.state !== "ok" ? (
        <Failure what="what is coming up" outcome={upcoming} />
      ) : (
        <UpcomingBody shell={shell} u={upcoming.value} account={account} />
      )}
    </Frame>
  );
}

function UpcomingBody({ shell, u, account }: { shell: ShellApi; u: Upcoming; account: string | null }) {
  const year = Number(u.today.slice(0, 4)) || 0;
  const accounts = new Map<string, string>();
  // Only an account the owner named gets a filter link: an unnamed one's key is a
  // codename, which never goes on the page, not even in a link.
  for (const c of u.rows) {
    const name = c.account === null ? null : ownLabel(c.account, c.accountLabel);
    if (c.account !== null && name !== null) accounts.set(c.account, name);
  }
  const selected = account !== null && accounts.has(account) ? account : null;
  const shown = selected === null ? u.rows : u.rows.filter((c) => c.account === selected);
  const coming = shown.filter((c) => c.state !== "likely_charged");
  const past = shown.filter((c) => c.state === "likely_charged");
  const behind = u.accounts.filter((a) => a.predicts);
  return (
    <>
      <div className={styles.lead}>
        <p className={styles.headline}>{headline(u, shown)}</p>
        <Footnote id={popoverId("upcoming-about")} label="About this list" title="About this list" wide>
          <p>
            Bills come from Owed, with their exact amounts. Everything marked expected is a
            prediction: a charge that has come regularly (monthly, weekly or yearly) and has
            not stopped as far as your statements show. Shopping does not repeat regularly
            enough to count, and transfers between your own accounts are left out.
          </p>
          <p>
            A prediction rests on the statements loaded so far, so a charge after an
            account&apos;s latest statement is still expected, and one expected before today
            but past that statement shows as probably already charged.
          </p>
          {u.overdueCount ? (
            <p>
              {plural(u.overdueCount, "overdue bill is", "overdue bills are")} on Owed, not
              here.
            </p>
          ) : null}
          {behind.length ? (
            <>
              <h4>Statements loaded through</h4>
              <ul className={styles.plain}>
                {behind.map((a) => (
                  <li key={a.account}>
                    {ownLabel(a.account, a.accountLabel) ?? UNNAMED_ACCOUNT}:{" "}
                    {shortDate(a.dataThrough, year)}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <p className={styles.faint}>{u.asOf}.</p>
        </Footnote>
      </div>
      {accounts.size && new Set(u.rows.map((c) => c.account)).size > 1 ? (
        <nav className={styles.years} aria-label="Account">
          <a href={shell.href("upcoming")} aria-current={selected === null ? "page" : undefined}>
            All
          </a>
          {[...accounts.entries()]
            .sort(([, a], [, b]) => a.localeCompare(b))
            .map(([slug, name]) => (
              <a
                key={slug}
                href={shell.href("upcoming", { account: slug })}
                aria-current={selected === slug ? "page" : undefined}
              >
                {name}
              </a>
            ))}
        </nav>
      ) : null}
      {coming.length ? (
        <section className={styles.section} aria-labelledby="upcoming-coming">
          <h2 id="upcoming-coming" className={styles.sectionHeading}>
            Coming up <span className={styles.count}>{coming.length}</span>
          </h2>
          <ul className={styles.bills}>
            {coming.map((c, i) => (
              <Row key={i} c={c} year={year} place={`c${i}`} />
            ))}
          </ul>
        </section>
      ) : null}
      {past.length ? (
        <section className={styles.section} aria-labelledby="upcoming-past">
          <details className={styles.group}>
            <summary>
              <span id="upcoming-past" className={styles.groupLabel}>
                Probably already charged
              </span>{" "}
              <span className={styles.groupCount}>
                {past.length} <span className={styles.faint}>not in your statements yet</span>
              </span>
            </summary>
            <ul className={styles.bills}>
              {past.map((c, i) => (
                <Row key={i} c={c} year={year} place={`p${i}`} />
              ))}
            </ul>
          </details>
        </section>
      ) : null}
    </>
  );
}
