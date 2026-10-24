"use client";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import {
  date,
  JOBSEARCH_PRESET_ID,
  row,
  rows,
  str,
  unwrapped,
  type Row,
} from "./data";
import {
  afterMove,
  age,
  fitFor,
  followupFor,
  formatBudget,
  formatHireRate,
  formatProposals,
  formatRating,
  formatReviews,
  formatSpend,
  isInvited,
  isNewClient,
  MAX_PROPOSALS,
  overCap,
  partyName,
  paymentVerified,
  parsePrefs,
  postingUrl,
  readPrefsText,
  savePrefs,
  jobId,
  SORTS,
  sourceLabel,
  PREFS_KEY,
  type Prefs,
  type Sort,
} from "./queue";
import { InfoTip } from "./info-tip";
import styles from "./queue.module.css";

type Submit = NonNullable<ShellApi["submit"]>;
export interface Choice {
  value: string;
  label: string;
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Saved queue preferences as an external store: the browser's storage, with
 * this tab's last choice kept in memory for when storage refuses a write. The
 * server snapshot is null, so the first render always uses the defaults. */
const listeners = new Set<() => void>();
let remembered: string | null = null;
function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === PREFS_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}
function savedText(): string | null {
  return readPrefsText(storage()) ?? remembered;
}
function remember(prefs: Prefs) {
  remembered = savePrefs(storage(), prefs);
  for (const listener of listeners) listener();
}

function OpenIcon() {
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
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <path d="M15 3h6v6" />
      <path d="M10 14 21 3" />
    </svg>
  );
}

/** Copies on click and shows a check for a moment; the label is the name. */
export function CopyButton({
  text,
  label,
  className,
  children,
}: {
  text: string;
  label: string;
  className?: string;
  children?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard refused (permissions or an insecure page): nothing to undo.
    }
  }
  return (
    <button
      type="button"
      className={`${className ?? styles.iconButton}${copied ? ` ${styles.copied}` : ""}`}
      onClick={copy}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
    >
      {children}
      {copied ? (
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M20 6 9 17l-5-5" />
        </svg>
      ) : (
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <rect width="13" height="13" x="9" y="9" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}

/** The fit pill: a short rating, its band's colour, the verdict in the name. */
export function FitPill({
  assessment,
  large = false,
}: {
  assessment: unknown;
  /** The job page's size: the score is the first thing read there. */
  large?: boolean;
}) {
  const fit = fitFor(assessment);
  if (!fit) return null;
  return (
    <div
      className={`${styles.score} ${styles[`score_${fit.band}`]}${large ? ` ${styles.scoreLarge}` : ""}`}
      title={fit.description}
      aria-label={fit.description}
      role="img"
    >
      <span className={styles.scoreValue} aria-hidden="true">
        {fit.mark}
      </span>
    </div>
  );
}

/** Stage, source, pipeline and the flags, in the reference card's order. */
function Badges({ item, pipeline }: { item: Row; pipeline?: string }) {
  const fields = row(item.fields);
  const source = sourceLabel(item.source);
  return (
    <>
      <span
        className={`${styles.badge} ${item.stage_kind === "open" ? styles.stageOpen : styles.stageClosed}`}
      >
        {str(item.stage_label) || str(item.stage_id)}
      </span>
      {source ? <span className={styles.badge}>{source}</span> : null}
      {pipeline ? (
        <span className={styles.badge}>{pipeline}</span>
      ) : null}
      {isInvited(fields) ? (
        <span className={`${styles.badge} ${styles.badgeAccent}`}>Invited</span>
      ) : null}
      {isNewClient(fields) ? (
        <span className={`${styles.badge} ${styles.badgeInfo}`}>New client</span>
      ) : null}
      {item.has_draft === true ? (
        <span className={`${styles.badge} ${styles.badgeAccent}`}>Draft</span>
      ) : null}
      {item.has_notes === true ? (
        <span className={styles.badge}>Notes</span>
      ) : null}
    </>
  );
}

/** Budget · proposals · spend · rating · contact · age, each only when known. */
export function Meta({
  item,
  now,
  detail = false,
}: {
  item: Row;
  now: number;
  detail?: boolean;
}) {
  const fields = row(item.fields);
  const job = item.preset_id === JOBSEARCH_PRESET_ID;
  const spend = formatSpend(fields);
  const rating = formatRating(fields);
  const party = partyName(item);
  return (
    <div className={styles.meta}>
      {job ? <span>{formatBudget(fields)}</span> : null}
      {job ? <span>{formatProposals(fields)}</span> : null}
      {spend ? <span>{spend}</span> : null}
      {rating ? <span>{rating}</span> : null}
      {detail && paymentVerified(fields) ? <span>Payment verified</span> : null}
      {detail && isNewClient(fields) ? (
        <span className={styles.flag}>New client</span>
      ) : null}
      {party ? <span>{party}</span> : null}
      <span>{age(item, now)}</span>
    </div>
  );
}

function FollowupLine({
  item,
  now,
  zone,
}: {
  item: Row;
  now: number;
  zone: string;
}) {
  const followup = followupFor(item, now, zone);
  if (!followup) return null;
  return (
    <p
      className={`${styles.followup}${followup.overdue ? ` ${styles.overdue}` : ""}`}
    >
      {followup.overdue ? "Overdue follow-up" : "Follow up"}{" "}
      {date(followup.due)}
      {followup.action ? `: ${followup.action}` : ""}
    </p>
  );
}

export interface Move {
  stage_id: string;
  label: string;
  kind: string;
}

/** The stage select, the open-the-posting link, the optional ID copy and Dismiss:
 * one row of controls for the queue card, the review card and the job detail
 * header alike. ``lead`` goes first in the row (the review card's Promote). */
export function StageActions({
  item,
  onMove,
  copyId = false,
  disabled = false,
  stageSelect = true,
  lead,
}: {
  item: Row;
  onMove?: (item: Row, to: Move) => void;
  copyId?: boolean;
  disabled?: boolean;
  stageSelect?: boolean;
  lead?: ReactNode;
}) {
  const title = str(item.title) || "Untitled opportunity";
  const posting = postingUrl(row(item.fields));
  const target = row(item.dismiss);
  const next = rows(item.transitions).map(
    (m): Move => ({
      stage_id: str(m.stage_id),
      label: str(m.label),
      kind: str(m.kind),
    }),
  );
  return (
    <div className={styles.actions}>
      {lead}
      {stageSelect && onMove && next.length ? (
        <span className={styles.selectWrap}>
          <select
            className={styles.stageSelect}
            aria-label={`Stage for ${title}`}
            value={str(item.stage_id)}
            disabled={disabled}
            onChange={(e) => {
              const to = next.find((m) => m.stage_id === e.target.value);
              if (to) onMove(item, to);
            }}
          >
            <option value={str(item.stage_id)}>
              {str(item.stage_label) || str(item.stage_id)}
            </option>
            {next.map((m) => (
              <option key={m.stage_id} value={m.stage_id}>
                {m.label}
              </option>
            ))}
          </select>
        </span>
      ) : null}
      {posting ? (
        <a
          className={styles.iconButton}
          href={posting}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Open the posting"
          title="Open the posting"
        >
          <OpenIcon />
        </a>
      ) : null}
      {copyId ? (
        <CopyButton text={jobId(item)} label={`Copy job ID ${jobId(item)}`} />
      ) : null}
      {onMove && target.stage_id ? (
        <button
          type="button"
          className={`${styles.iconButton} ${styles.danger}`}
          aria-label={`Dismiss ${title} (move to ${str(target.label)})`}
          title={`Dismiss: move to ${str(target.label)}`}
          disabled={disabled}
          onClick={() =>
            onMove(item, {
              stage_id: str(target.stage_id),
              label: str(target.label),
              kind: "terminal",
            })
          }
        >
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
            <path d="M18 6 6 18M6 6l12 12" />
          </svg>
        </button>
      ) : null}
    </div>
  );
}

function Card({
  item,
  href,
  now,
  zone,
  pipeline,
  error,
  onMove,
}: {
  item: Row;
  href: string;
  now: number;
  zone: string;
  pipeline?: string;
  error?: string;
  onMove?: (item: Row, to: Move) => void;
}) {
  const title = str(item.title) || "Untitled opportunity";
  const summary = str(row(item.assessment).summary);
  return (
    <article className={styles.card}>
      <div className={styles.cardHeader}>
        <div>
          <h2 className={styles.title}>
            <a href={href}>{title}</a>
          </h2>
          <Badges item={item} pipeline={pipeline} />
        </div>
        <FitPill assessment={item.assessment} />
      </div>
      <Meta item={item} now={now} />
      <FollowupLine item={item} now={now} zone={zone} />
      {summary ? (
        <p className={`${styles.reason} ${styles.clamped}`} title={summary}>
          {summary}
        </p>
      ) : null}
      {item.preview ? (
        <a href={href} className={styles.preview}>
          {unwrapped(str(item.preview))}
        </a>
      ) : null}
      <StageActions item={item} onMove={onMove} copyId />
      {error ? (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

export interface QueueProps {
  items: Row[];
  /** Server render time, so every relative time agrees with the markup. */
  now: number;
  /** The viewer's zone, so "overdue" means before today where they are. */
  zone: string;
  pipelines: Choice[];
  stages: Choice[];
  /** The effective selections (the URL's, or the defaults). */
  pipeline: string;
  stage: string;
  hideFull: boolean;
  sort: Sort;
  /** Whether the URL named any control; if not, saved choices apply. */
  explicit: boolean;
  /** The GET target the controls submit to. */
  action: string;
  hrefs: Record<string, string>;
  /** Pipeline names per id, given only when several pipelines are shown. */
  pipelineNames?: Record<string, string>;
  /** Count wording: jobs when only job-search pipelines are shown. */
  noun: [string, string];
  /** The server's answer had as many items as it may send. */
  limited: boolean;
  /** A pipeline or stage other than the defaults is selected. */
  filtered: boolean;
  /** Nothing is shown, but postings over the proposal cap exist here. */
  hiddenOnly: boolean;
  /** Shown when the default view has nothing at all. */
  nothingYet: ReactNode;
  submit?: Submit;
}

/** The saved choices to open a bare queue URL with, or null to stay put. Saved
 * values the current catalog no longer offers fall back to the defaults, so a
 * removed pipeline never redirects again on every visit. */
export function savedView(
  last: Prefs,
  props: Pick<
    QueueProps,
    "pipeline" | "stage" | "hideFull" | "sort" | "pipelines" | "stages"
  >,
): Record<string, string> | null {
  const pipeline = props.pipelines.some((p) => p.value === last.pipeline)
    ? last.pipeline
    : props.pipeline;
  // The stage list belongs to the current pipeline; a stage saved for another
  // pipeline is left for the server to check against that one.
  const stage =
    pipeline === props.pipeline &&
    !props.stages.some((s) => s.value === last.stage)
      ? props.stage
      : last.stage;
  if (
    pipeline === props.pipeline &&
    stage === props.stage &&
    last.hideFull === props.hideFull &&
    last.sort === props.sort
  )
    return null;
  return {
    pipeline,
    stage,
    full: last.hideFull ? "hide" : "show",
    sort: last.sort,
  };
}

/** The job queue: heading with a live count, the controls, and the cards. */
export function JobQueue(props: QueueProps) {
  const [items, setItems] = useState(props.items);
  const saved = parsePrefs(
    useSyncExternalStore(subscribe, savedText, () => null),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const form = useRef<HTMLFormElement>(null);
  const full = useRef<HTMLInputElement>(null);
  const current: Prefs = {
    pipeline: props.pipeline,
    stage: props.stage,
    hideFull: props.hideFull,
    sort: props.sort,
  };

  useEffect(() => {
    // A bare queue URL opens the view chosen last time, once a choice exists.
    if (props.explicit || !form.current || savedText() === null) return;
    const view = savedView(parsePrefs(savedText()), props);
    if (!view) return;
    const url = new URL(form.current.action);
    for (const [name, value] of Object.entries(view))
      url.searchParams.set(name, value);
    window.location.replace(url.href);
    // Runs once: the saved view is applied on arrival, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Saves the choice and reloads the view with it; the server filters and
   * sorts, so the count and the first page always agree with the controls. */
  function choose(changes: Partial<Prefs>) {
    // The whole view the controls now show is saved, so a bare URL reopens it.
    remember({ ...saved, ...current, ...changes });
    if (changes.hideFull !== undefined && full.current)
      full.current.value = changes.hideFull ? "hide" : "show";
    form.current?.requestSubmit();
  }

  async function move(item: Row, to: Move) {
    const submit = props.submit;
    const id = str(item.id);
    if (!submit) return;
    const before = items;
    setErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => key !== id)),
    );
    setItems((current) => afterMove(current, id, to, props.stage));
    let outcome;
    try {
      outcome = await submit("leads.opportunity.transition", {
        ref: item.ref,
        revision: item.revision,
        to_stage_id: to.stage_id,
      });
    } catch {
      outcome = { state: "unavailable" as const };
    }
    if (outcome.state === "ok") {
      const revision = row(outcome.result).revision;
      setItems((current) =>
        current.map((each) =>
          str(each.id) === id && revision !== undefined
            ? { ...each, revision }
            : each,
        ),
      );
      return;
    }
    // Put back only this card, where it was, so another move in flight keeps
    // its own optimistic state.
    const index = before.findIndex((each) => str(each.id) === id);
    setItems((current) => {
      const rest = current.filter((each) => str(each.id) !== id);
      rest.splice(Math.min(Math.max(index, 0), rest.length), 0, item);
      return rest;
    });
    setErrors((current) => ({
      ...current,
      [id]:
        outcome.state === "refused" && outcome.code === "record_stale"
          ? "This changed since the page loaded. Reload to change its stage."
          : `Could not move it to ${to.label}. Try again.`,
    }));
  }

  const [one, many] = props.noun;
  return (
    <div className={styles.queue}>
      <div className={styles.heading}>
        <h1 className={styles.headingTitle}>Job queue</h1>
        <span className={styles.count} aria-live="polite">
          {items.length} {items.length === 1 ? one : many}
        </span>
      </div>
      <form
        ref={form}
        className={styles.controls}
        action={props.action}
        method="get"
        aria-label="Job queue filters"
      >
        <input
          ref={full}
          type="hidden"
          name="full"
          defaultValue={props.hideFull ? "hide" : "show"}
        />
        <div className={styles.controlGroup}>
          <label className={styles.select}>
            <span>Pipeline</span>
            <span className={styles.selectWrap}>
              <select
                name="pipeline"
                value={props.pipeline}
                onChange={(e) => choose({ pipeline: e.target.value })}
              >
                {props.pipelines.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </span>
          </label>
          <label className={styles.select}>
            <span>Show</span>
            <span className={styles.selectWrap}>
              <select
                name="stage"
                value={props.stage}
                onChange={(e) => choose({ stage: e.target.value })}
              >
                {props.stages.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </span>
          </label>
        </div>
        <div className={`${styles.controlGroup} ${styles.controlRight}`}>
          <button
            type="button"
            className={`${styles.chip}${props.hideFull ? ` ${styles.chipOn}` : ""}`}
            aria-pressed={props.hideFull}
            onClick={() => choose({ hideFull: !props.hideFull })}
          >
            Hide 50+ proposals
          </button>
          <label className={styles.select}>
            <span>Sort</span>
            <span className={styles.selectWrap}>
              <select
                name="sort"
                value={props.sort}
                onChange={(e) => choose({ sort: e.target.value as Sort })}
              >
                {SORTS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </span>
          </label>
        </div>
      </form>
      {!items.length ? (
        <div className={styles.empty}>
          {props.hiddenOnly ? (
            <p>
              Every job here has 50+ proposals.{" "}
              <button
                type="button"
                className={styles.linkButton}
                onClick={() => choose({ hideFull: false })}
              >
                Show them
              </button>
            </p>
          ) : props.filtered ? (
            <p>Nothing matches these filters. Try another pipeline or stage.</p>
          ) : (
            props.nothingYet
          )}
        </div>
      ) : (
        <>
          {items.map((item) => (
            <Card
              key={str(item.id)}
              item={item}
              href={props.hrefs[str(item.id)] ?? ""}
              now={props.now}
              zone={props.zone}
              pipeline={props.pipelineNames?.[str(item.pipeline_id)]}
              error={errors[str(item.id)]}
              onMove={props.submit ? move : undefined}
            />
          ))}
          {props.limited ? (
            <p className={styles.footnote}>
              Showing the first {props.items.length}. Narrow the pipeline or
              stage to see the rest.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

/** The detail page's header for a job-search opportunity: what deciding on the
 * job needs (title, figures, verdict, score, the stage controls) in view, and
 * the evidence and record facts behind one info button (#408). */
export function JobHeader({
  item,
  now,
  zone = "UTC",
  submit,
  back,
}: {
  item: Row;
  now: number;
  zone?: string;
  /** Absent on a read-only host: then only the open link shows. */
  submit?: Submit;
  /** Where Dismiss returns to: the job queue. */
  back?: string;
}) {
  const [moving, setMoving] = useState(false);
  const [error, setError] = useState("");

  /** Moves the job like a queue card does; the page then shows the new stage,
   * and a dismissed job returns to the queue. */
  async function move(_: Row, to: Move, dismiss = false) {
    if (!submit) return;
    setMoving(true);
    setError("");
    let outcome;
    try {
      outcome = await submit("leads.opportunity.transition", {
        ref: item.ref,
        revision: item.revision,
        to_stage_id: to.stage_id,
      });
    } catch {
      outcome = { state: "unavailable" as const };
    }
    if (outcome.state === "ok") {
      // Replace: Back from the queue should not reopen a dismissed job.
      if (dismiss && back) window.location.replace(back);
      else window.location.reload();
      return;
    }
    setMoving(false);
    setError(
      outcome.state === "refused" && outcome.code === "record_stale"
        ? "This changed since the page loaded. Reload to change its stage."
        : `Could not move it to ${to.label}. Try again.`,
    );
  }
  const fields = row(item.fields);
  const title = str(item.title) || "Untitled opportunity";
  const posting = postingUrl(fields);
  const assessment = row(item.assessment);
  const summary = str(assessment.summary);
  const reasons = Array.isArray(assessment.reasons)
    ? assessment.reasons.map(str).filter(Boolean)
    : [];
  // The first reason is the verdict ("Fit: strong. Economics: uncertain."); the
  // rest is evidence, which waits behind the info button with the record facts.
  const verdict = reasons[0] ?? summary;
  const evidence = reasons.length > 1 ? reasons.slice(1) : [];
  const source = sourceLabel(item.source);
  // Client history the facts line has no room for.
  const client = [formatHireRate(fields), formatReviews(fields)]
    .filter(Boolean)
    .join(", ");
  return (
    <header className={styles.job}>
      <div className={styles.jobMain}>
        <h1 className={styles.jobTitle}>
          {posting ? (
            <a href={posting} target="_blank" rel="noopener noreferrer">
              {title}
            </a>
          ) : (
            title
          )}
        </h1>
        <Meta item={item} now={now} detail />
        <div className={styles.verdictRow}>
          {verdict ? <p className={styles.verdict}>{verdict}</p> : null}
          <InfoTip label="Why this score, and the record details" align="start" wide>
            {evidence.length ? (
              <ul>
                {evidence.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            ) : !verdict ? (
              <p>Not assessed yet.</p>
            ) : null}
            <dl className={styles.tipFacts}>
              {client ? (
                <div>
                  <dt>Client</dt>
                  <dd>{client}</dd>
                </div>
              ) : null}
              <div>
                <dt>Stage</dt>
                <dd>{str(item.stage_label) || str(item.stage_id)}</dd>
              </div>
              {source ? (
                <div>
                  <dt>Source</dt>
                  <dd>{source}</dd>
                </div>
              ) : null}
            </dl>
          </InfoTip>
        </div>
      </div>
      <div className={styles.jobSide}>
        <FitPill assessment={item.assessment} large />
        {/* Without a stage select (a closed job, or a read-only host) the stage
            still shows, so a dismissed job never looks open. */}
        {!submit || !rows(item.transitions).length ? (
          <span
            className={`${styles.badge} ${item.stage_kind === "open" ? styles.stageOpen : styles.stageClosed}`}
          >
            {str(item.stage_label) || str(item.stage_id)}
          </span>
        ) : null}
        <StageActions
          item={item}
          copyId
          disabled={moving}
          onMove={
            submit
              ? (each, to) =>
                  move(
                    each,
                    to,
                    to.stage_id === str(row(item.dismiss).stage_id) &&
                      to.kind === "terminal",
                  )
              : undefined
          }
        />
      </div>
      {error ? (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      ) : null}
      {overCap(fields) ? (
        <div className={styles.capWarning} role="alert">
          Over {MAX_PROPOSALS} proposals. Check the current count on the posting
          before you spend anything applying.
        </div>
      ) : null}
      <FollowupLine item={item} now={now} zone={zone} />
    </header>
  );
}
