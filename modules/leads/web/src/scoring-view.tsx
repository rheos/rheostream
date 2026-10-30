"use client";
import { useEffect, useRef, useState } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { row, rows, str, type Row } from "./data";
import { InfoTip } from "./info-tip";
import { timeAgo } from "./queue";
import {
  FAST_REFRESH_MS,
  SLOW_REFRESH_MS,
  batchSource,
  busy,
  clock,
  outcomeLabel,
  purposeLabel,
  readFailureLabel,
  scoreBand,
  since,
  span,
  stageChip,
  statusLine,
  stopLabel,
  tokens,
} from "./scoring";
import queueStyles from "./queue.module.css";
import styles from "./scoring.module.css";

type Submit = NonNullable<ShellApi["submit"]>;
export type ScoringTab = "queue" | "calls" | "feed";

export interface ScoringProps {
  /** The ``leads.ui.scoring`` answer's data. */
  view: Row;
  /** Server render time, so every relative time agrees with the markup. */
  now: number;
  zone: string;
  tab: ScoringTab;
  /** Job detail links by opportunity reference. */
  hrefs: Record<string, string>;
  tabHrefs: Record<ScoringTab, string>;
  reviewHref: string;
  /** Re-queueing failed jobs spends the owner's scoring account's time. */
  canRetry: boolean;
  submit?: Submit;
  /** The shell's in-place refresh: fresh server data, no page load (#443). */
  refresh?: () => Promise<void>;
}

function RetryIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  );
}

function Chip({ job, now, zone }: { job: Row; now: number; zone: string }) {
  const score = job.score;
  if (str(job.stage) === "scored" && typeof score === "number") {
    return (
      <span
        className={`${queueStyles.score} ${queueStyles[`score_${scoreBand(score)}`]} ${styles.chipScore}`}
        title={`Scored ${score} of 100`}
      >
        <span className={queueStyles.scoreValue}>{score}</span>
      </span>
    );
  }
  const chip = stageChip(job, now, zone);
  return (
    <span
      className={`${styles.chip} ${styles[`chip_${chip.tone}`] ?? ""}`}
      title={chip.title}
    >
      {chip.mark}
      <span className={styles.srOnly}>: {chip.title}</span>
    </span>
  );
}

function JobRow({
  job,
  now,
  zone,
  href,
}: {
  job: Row;
  now: number;
  zone: string;
  href?: string;
}) {
  const title = str(job.title) || "Untitled job";
  const scored = str(job.stage) === "scored" && job.assessed_at;
  return (
    <li className={styles.row}>
      <Chip job={job} now={now} zone={zone} />
      {href ? (
        <a className={styles.rowTitle} href={href}>
          {title}
        </a>
      ) : (
        <span className={styles.rowTitle}>{title}</span>
      )}
      <span className={styles.rowWhen}>
        {scored
          ? `scored ${timeAgo(job.assessed_at, now)}`
          : `arrived ${timeAgo(job.created_at, now)}`}
      </span>
    </li>
  );
}

function Batch({
  batch,
  now,
  zone,
  hrefs,
  reviewHref,
}: {
  batch: Row;
  now: number;
  zone: string;
  hrefs: Record<string, string>;
  reviewHref: string;
}) {
  const jobs = rows(batch.jobs);
  const held = jobs.filter((j) => str(j.stage) === "held");
  const active = jobs.filter((j) => str(j.stage) !== "held");
  const counts = row(batch.counts);
  const parts = (
    [
      ["scoring", "scoring"],
      ["queued", "queued"],
      ["scored", "scored"],
      ["retrying", "retrying"],
      ["paused", "paused"],
      ["needs_posting", "need the posting"],
      ["failed", "failed"],
      ["too_old", "too old"],
    ] as const
  )
    .filter(([key]) => typeof counts[key] === "number" && counts[key] > 0)
    .map(([key, words]) => `${str(counts[key])} ${words}`);
  const heading = `${batchSource(batch.sources)} · ${clock(batch.started_at, now, zone)}`;
  return (
    <section className={styles.batch} aria-label={heading}>
      <header className={styles.batchHead}>
        <h2 className={styles.batchTitle}>{heading}</h2>
        <span className={styles.batchMeta}>
          {jobs.length} {jobs.length === 1 ? "job" : "jobs"}
          {parts.length ? ` · ${parts.join(" · ")}` : ""}
        </span>
      </header>
      {active.length ? (
        <ul className={styles.rows}>
          {active.map((job) => (
            <JobRow
              key={str(job.id)}
              job={job}
              now={now}
              zone={zone}
              href={hrefs[str(job.ref)]}
            />
          ))}
        </ul>
      ) : null}
      {held.length ? (
        <details className={styles.held}>
          <summary>{held.length} held by the filters</summary>
          <ul className={styles.rows}>
            {held.map((job) => (
              <JobRow
                key={str(job.id)}
                job={job}
                now={now}
                zone={zone}
                href={hrefs[str(job.ref)]}
              />
            ))}
          </ul>
          <p className={styles.heldNote}>
            <a href={reviewHref}>Open the review queue</a> to promote one past
            the filters.
          </p>
        </details>
      ) : null}
    </section>
  );
}

function Queue({
  view,
  now,
  zone,
  hrefs,
  reviewHref,
}: {
  view: Row;
  now: number;
  zone: string;
  hrefs: Record<string, string>;
  reviewHref: string;
}) {
  const batches = rows(view.batches);
  if (!batches.length) {
    return (
      <div className={queueStyles.empty}>
        <p>No jobs arrived in the last {str(view.window_hours)} hours.</p>
      </div>
    );
  }
  return (
    <>
      {batches.map((batch) => (
        <Batch
          key={str(batch.started_at)}
          batch={batch}
          now={now}
          zone={zone}
          hrefs={hrefs}
          reviewHref={reviewHref}
        />
      ))}
    </>
  );
}

function Calls({
  view,
  now,
  zone,
  hrefs,
}: {
  view: Row;
  now: number;
  zone: string;
  hrefs: Record<string, string>;
}) {
  const summary = rows(view.summary);
  const calls = rows(view.calls);
  const total = (key: string) =>
    summary.reduce(
      (sum, s) => sum + (typeof s[key] === "number" ? (s[key] as number) : 0),
      0,
    );
  return (
    <>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>
          Last {str(view.summary_days)} days
        </h2>
        <InfoTip label="About the model call log" wide>
          <p>
            One row for each model call Leads makes: scoring a job, the steps of
            drafting a proposal, and pricing. Tokens and time only; the calls
            run on your subscription login, not a metered API.
          </p>
          <p>
            No posting text, prompt or answer is kept. Rows older than 90 days
            are removed.
          </p>
        </InfoTip>
      </div>
      {!summary.length ? (
        <p className={styles.quiet}>No model calls in this period.</p>
      ) : (
        <>
          <p className={styles.totals}>
            <span>
              <strong>{total("calls").toLocaleString("en")}</strong> calls
            </span>
            <span>{tokens(total("input_tokens"))} tokens in</span>
            <span>{tokens(total("output_tokens"))} out</span>
          </p>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Purpose</th>
                  <th scope="col">Model</th>
                  <th scope="col" className={styles.num}>
                    Calls
                  </th>
                  <th scope="col" className={styles.num}>
                    Tokens in
                  </th>
                  <th scope="col" className={styles.num}>
                    Tokens out
                  </th>
                  <th scope="col" className={styles.num}>
                    Median time
                  </th>
                  <th scope="col" className={styles.num}>
                    Failed
                  </th>
                  <th scope="col" className={styles.num}>
                    Deferred
                  </th>
                </tr>
              </thead>
              <tbody>
                {summary.map((s) => (
                  <tr key={`${str(s.purpose)}-${str(s.model_id)}`}>
                    <td>{purposeLabel(s.purpose)}</td>
                    <td className={styles.dim}>{str(s.model_id)}</td>
                    <td className={styles.num}>{tokens(s.calls)}</td>
                    <td className={styles.num}>{tokens(s.input_tokens)}</td>
                    <td className={styles.num}>{tokens(s.output_tokens)}</td>
                    <td className={styles.num}>
                      {typeof s.median_seconds === "number"
                        ? span(s.median_seconds)
                        : "–"}
                    </td>
                    <td className={styles.num}>{tokens(s.failed)}</td>
                    <td className={styles.num}>{tokens(s.deferred)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>Recent calls</h2>
      </div>
      {!calls.length ? (
        <p className={styles.quiet}>No model calls recorded yet.</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Purpose</th>
                <th scope="col">Job</th>
                <th scope="col">Model</th>
                <th scope="col" className={styles.num}>
                  Tokens in / out
                </th>
                <th scope="col" className={styles.num}>
                  Time
                </th>
                <th scope="col">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => {
                const outcome = str(c.outcome);
                const running = outcome === "running";
                const elapsed = running ? since(c.started_at, now) : null;
                const seconds =
                  typeof c.seconds === "number" ? c.seconds : elapsed;
                const href = hrefs[str(c.ref)];
                return (
                  <tr key={str(c.id)}>
                    <td className={styles.dim}>{clock(c.started_at, now, zone)}</td>
                    <td>{purposeLabel(c.purpose)}</td>
                    <td className={styles.jobCell}>
                      {href ? (
                        <a href={href}>{str(c.title) || "Untitled job"}</a>
                      ) : (
                        <span className={styles.dim}>
                          {c.ref ? str(c.title) || "Untitled job" : "–"}
                        </span>
                      )}
                    </td>
                    <td className={styles.dim}>{str(c.model_id)}</td>
                    <td className={styles.num}>
                      {c.input_tokens == null && c.output_tokens == null
                        ? "–"
                        : `${tokens(c.input_tokens)} / ${tokens(c.output_tokens)}`}
                    </td>
                    <td className={styles.num}>
                      {seconds === null ? "–" : span(seconds)}
                    </td>
                    <td
                      className={
                        outcome === "failed" || outcome === "interrupted"
                          ? styles.bad
                          : outcome === "deferred"
                            ? styles.warn
                            : undefined
                      }
                    >
                      {outcomeLabel(c)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** The job feed's recent runs: what each brought in, and why any detail read
 * failed, so a bad stretch can be read after the fact (#377). */
function FeedRuns({ view, now, zone }: { view: Row; now: number; zone: string }) {
  const runs = rows(view.feed_runs);
  return (
    <>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>Recent runs</h2>
        <InfoTip label="About the job feed runs" wide>
          <p>
            The feed runs every two hours. Each new job it finds is read in
            full; a job whose read fails is kept as a refresh only, with no
            description, until a later run reads it.
          </p>
          <p>
            Only counts and fixed reasons are kept, never a posting. Runs older
            than 90 days are removed.
          </p>
        </InfoTip>
      </div>
      {!runs.length ? (
        <p className={styles.quiet}>No runs recorded yet.</p>
      ) : (
        <ul className={styles.runs}>
          {runs.map((run, index) => {
            const failures = Object.entries(row(run.read_failures)).filter(
              ([, n]) => typeof n === "number" && n > 0,
            ) as [string, number][];
            const stopped = run.outcome === "stopped";
            return (
              <li key={str(run.id) || `${str(run.finished_at)}-${index}`} className={styles.run}>
                <span className={styles.runWhen}>{clock(run.finished_at, now, zone)}</span>
                <span className={styles.runWhat}>
                  {stopped ? (
                    <span className={styles.runStopped}>
                      Stopped: {stopLabel(run.stop_code)}
                    </span>
                  ) : (
                    <>
                      {tokens(run.imported)} new, {tokens(run.refreshed)} updated
                      {typeof run.detail_reads === "number" && run.detail_reads > 0
                        ? `, ${tokens(run.detail_reads)} tried`
                        : ""}
                    </>
                  )}
                </span>
                {failures.length ? (
                  <span className={styles.runFailures}>
                    {failures.map(([code, n]) => (
                      <span key={code} className={styles.chip}>
                        {n} {readFailureLabel(code)}
                      </span>
                    ))}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/** The status line, with its own one-second clock for the elapsed counter. */
function Status({
  status,
  now: initial,
  zone,
}: {
  status: Row;
  now: number;
  zone: string;
}) {
  const [now, setNow] = useState(initial);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const line = statusLine(status, now, zone);
  return (
    <p className={`${styles.status} ${styles[`status_${line.tone}`]}`}>
      <span className={styles.dot} aria-hidden="true" />
      <span role="status">{line.text}</span>
    </p>
  );
}

/** The Scoring view: what scoring is doing now, recent jobs by import with each
 * one's stage, and the model call log. It looks again while work is in flight
 * (and slowly otherwise), unless the window is in the background or a panel is
 * open. Looking again renders in place when the shell can, a reload otherwise. */
export function ScoringView(props: ScoringProps) {
  const { view, tab, now, zone, refresh } = props;
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState("");
  const root = useRef<HTMLDivElement | null>(null);
  const live = busy(view, now);

  // A window in the background (another tab or app has focus) waits until it
  // is back in front. Kept outside the loop, which re-arms on every refresh.
  const away = useRef(false);
  useEffect(() => {
    const leave = () => {
      away.current = true;
    };
    const back = () => {
      away.current = false;
    };
    window.addEventListener("blur", leave);
    window.addEventListener("focus", back);
    return () => {
      window.removeEventListener("blur", leave);
      window.removeEventListener("focus", back);
    };
  }, []);

  useEffect(() => {
    const delay = live ? FAST_REFRESH_MS : SLOW_REFRESH_MS;
    // An open panel is never reloaded away.
    let id = 0;
    let stopped = false;
    const tick = () => {
      const engaged =
        away.current ||
        Boolean(
          root.current?.querySelector("details[open], [aria-expanded='true']"),
        );
      if (engaged) {
        id = window.setTimeout(tick, delay);
        return;
      }
      if (!refresh) {
        window.location.reload();
        return;
      }
      refresh()
        .catch(() => {})
        .finally(() => {
          if (!stopped) id = window.setTimeout(tick, delay);
        });
    };
    id = window.setTimeout(tick, delay);
    return () => {
      stopped = true;
      window.clearTimeout(id);
    };
  }, [live, refresh]);

  async function retry() {
    const submit = props.submit;
    if (!submit || retrying) return;
    setRetrying(true);
    setError("");
    let outcome;
    try {
      outcome = await submit("leads.jobscoring.retry_failed", {});
    } catch {
      outcome = { state: "unavailable" as const };
    }
    if (outcome.state === "ok") {
      // A successful submit already renders the screen again in place.
      if (refresh) setRetrying(false);
      else window.location.reload();
      return;
    }
    setRetrying(false);
    setError("Could not put the failed jobs back in line. Try again.");
  }

  const queued = typeof view.queued_total === "number" ? view.queued_total : 0;
  const waiting =
    typeof view.waiting_total === "number" ? view.waiting_total : 0;
  const failed = typeof view.failed_total === "number" ? view.failed_total : 0;
  const tooOld =
    typeof view.too_old_total === "number" ? view.too_old_total : 0;
  const maxAge =
    typeof view.max_age_hours === "number" ? view.max_age_hours : 0;
  const eta = typeof view.eta_seconds === "number" ? view.eta_seconds : null;
  const perJob =
    typeof view.median_call_seconds === "number"
      ? view.median_call_seconds
      : null;
  const tabs: [ScoringTab, string][] = [
    ["queue", "Queue"],
    ["calls", "Model calls"],
    ["feed", "Job feed"],
  ];

  return (
    <div className={styles.view} ref={root}>
      <div className={queueStyles.heading}>
        <h1 className={queueStyles.headingTitle}>Scoring</h1>
        <InfoTip label="About scoring" wide>
          <p>
            New jobs are scored one at a time, newest first. The client filters
            decide first; a job they let through goes to the model with your
            profile, and its score lands on the job.
          </p>
          <p>
            When the usage limit stops a call, every waiting job pauses and
            scoring tries again on its own. A job that fails five times is
            marked failed until its posting changes, or until an owner puts it
            back in line here.
          </p>
          {maxAge > 0 ? (
            <p>
              A job still waiting {maxAge} {maxAge === 1 ? "hour" : "hours"}{" "}
              after it arrived is set aside unscored, so scoring that resumes
              after a pause starts with the recent jobs. Promoted and invited
              jobs are always scored.
            </p>
          ) : null}
        </InfoTip>
      </div>
      <Status status={row(view.status)} now={now} zone={zone} />
      <p className={styles.totals}>
        <span>
          <strong>{queued}</strong> queued
          {eta !== null ? ` · up to about ${span(eta)} to clear` : ""}
        </span>
        {perJob !== null ? <span>{span(perJob)} per job lately</span> : null}
        {waiting > 0 ? <span>{waiting} waiting to retry</span> : null}
        {failed > 0 ? (
          <span className={styles.failedTotal}>
            <strong>{failed}</strong> failed
            {props.canRetry && props.submit ? (
              <button
                type="button"
                className={styles.iconButton}
                aria-label={`Put ${failed} failed ${failed === 1 ? "job" : "jobs"} back in line`}
                title={`Put ${failed} failed ${failed === 1 ? "job" : "jobs"} back in line`}
                disabled={retrying}
                onClick={retry}
              >
                <RetryIcon />
              </button>
            ) : null}
          </span>
        ) : null}
        {tooOld > 0 ? <span>{tooOld} too old to score</span> : null}
      </p>
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      <nav className={styles.tabs} aria-label="Scoring">
        {tabs.map(([id, text]) => (
          <a
            key={id}
            href={props.tabHrefs[id]}
            className={`${styles.tab}${tab === id ? ` ${styles.tabActive}` : ""}`}
            aria-current={tab === id ? "page" : undefined}
          >
            {text}
          </a>
        ))}
      </nav>
      {tab === "calls" ? (
        <Calls view={view} now={now} zone={zone} hrefs={props.hrefs} />
      ) : tab === "feed" ? (
        <FeedRuns view={view} now={now} zone={zone} />
      ) : (
        <Queue
          view={view}
          now={now}
          zone={zone}
          hrefs={props.hrefs}
          reviewHref={props.reviewHref}
        />
      )}
    </div>
  );
}
