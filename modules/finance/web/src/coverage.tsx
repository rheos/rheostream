import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { COVERAGE_STATES, monthLabel, words, type CoverageMonth, type CoverageState } from "./data";
import { Failure, Footnote, Frame, popoverId } from "./frame";
import { ownLabel, UNNAMED_ACCOUNT } from "./names";
import type { CoverageState as Loaded } from "./load";
import styles from "./finance.module.css";

const MONTH_NUMBERS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"];

const STATE_TEXT: Record<CoverageState, string> = {
  have: "Statement on file",
  partial: "Part of the month",
  missing: "Missing",
  rejected: "Rejected",
  unparsed: "Unparsed",
  not_applicable: "Not applicable",
};

const STATE_CLASS: Record<CoverageState, string> = {
  have: styles.cellHave ?? "",
  partial: styles.cellPartial ?? "",
  missing: styles.cellMissing ?? "",
  rejected: styles.cellBad ?? "",
  unparsed: styles.cellBad ?? "",
  not_applicable: styles.cellNa ?? "",
};

export function CoverageView({ shell, state }: { shell: ShellApi; state: Loaded }) {
  const { year, coverage } = state;
  return (
    <Frame shell={shell} active="coverage" strip={state.strip}>
      <div className={styles.lead}>
        <nav className={styles.years} aria-label="Year">
          <a href={shell.href("coverage", { year: String(year - 1) })} aria-label={`Coverage for ${year - 1}`}>
            ‹ {year - 1}
          </a>
          <span className={styles.headline} aria-current="page">
            {year}
          </span>
          <a href={shell.href("coverage", { year: String(year + 1) })} aria-label={`Coverage for ${year + 1}`}>
            {year + 1} ›
          </a>
        </nav>
        <Footnote id={popoverId("coverage-key")} label="What the colours mean" title="Coverage key">
          <ul className={styles.legend}>
            {COVERAGE_STATES.map((s) => (
              <li key={s}>
                <span className={`${styles.cell} ${STATE_CLASS[s]}`} aria-hidden="true" />
                {STATE_TEXT[s]}
              </li>
            ))}
          </ul>
          <p>Every month that is not on file is listed under the grid, with its reason.</p>
        </Footnote>
      </div>
      {coverage.state !== "ok" ? (
        <Failure what="coverage" outcome={coverage} />
      ) : (
        <Grid months={coverage.value.months} year={year} noData={coverage.value.noDataAccounts} />
      )}
    </Frame>
  );
}

function Grid({
  months,
  year,
  noData,
}: {
  months: readonly CoverageMonth[];
  year: number;
  noData: number;
}) {
  const accounts = new Map<string, { label: string; byMonth: Map<string, CoverageMonth> }>();
  for (const m of months) {
    const entry = accounts.get(m.account) ?? {
      label: ownLabel(m.account, m.accountLabel) ?? UNNAMED_ACCOUNT,
      byMonth: new Map(),
    };
    entry.byMonth.set(m.month.slice(5, 7), m);
    accounts.set(m.account, entry);
  }
  if (!accounts.size)
    return (
      <div className={styles.empty}>
        <h2>No coverage for {year}</h2>
        <p>No account has a statement month in this year.</p>
      </div>
    );
  const missing = months.filter((m) => m.state === "missing").length;
  return (
    <>
      <p className={styles.faint}>
        {accounts.size} accounts, {missing} missing {missing === 1 ? "month" : "months"}
        {noData ? `; ${noData} ${noData === 1 ? "account has" : "accounts have"} no data at all` : ""}.
      </p>
      <div className={styles.gridWrap}>
        <table className={styles.grid}>
          <caption className={styles.visuallyHidden}>Statement coverage by account and month, {year}</caption>
          <thead>
            <tr>
              <th scope="col">Account</th>
              {MONTH_NUMBERS.map((n) => (
                <th key={n} scope="col">
                  {monthLabel(`${year}-${n}-01`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...accounts.entries()].map(([account, { label, byMonth }]) => (
              <tr key={account}>
                <th scope="row">{label}</th>
                {MONTH_NUMBERS.map((n) => {
                  const m = byMonth.get(n);
                  const text = m
                    ? `${STATE_TEXT[m.state]}${m.reason ? `: ${words(m.reason)}` : ""}`
                    : "No statement month";
                  return (
                    <td key={n}>
                      <span
                        className={`${styles.cell} ${m ? STATE_CLASS[m.state] : styles.cellNone}`}
                        title={text}
                        role="img"
                        aria-label={`${monthLabel(`${year}-${n}-01`)}: ${text}`}
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Problems accounts={accounts} />
    </>
  );
}

/** The months that are not on file, by account, each with its state and reason. */
function Problems({
  accounts,
}: {
  accounts: Map<string, { label: string; byMonth: Map<string, CoverageMonth> }>;
}) {
  const rows = [...accounts.entries()].flatMap(([account, { label, byMonth }]) =>
    [...byMonth.values()]
      .filter((m) => m.state !== "have" && m.state !== "not_applicable")
      .map((m) => (
        <li key={`${account}-${m.month}`}>
          <span>{label}</span>
          <span>{monthLabel(m.month)}</span>
          <span className={styles.faint}>
            {STATE_TEXT[m.state]}
            {m.reason ? `: ${words(m.reason)}` : ""}
          </span>
        </li>
      )),
  );
  if (!rows.length) return null;
  return (
    <section className={styles.section} aria-labelledby="coverage-gaps">
      <h2 id="coverage-gaps" className={styles.sectionHeading}>
        Not on file <span className={styles.count}>{rows.length}</span>
      </h2>
      <ul className={styles.rows}>{rows}</ul>
    </section>
  );
}
