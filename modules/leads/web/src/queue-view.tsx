"use client";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { date, JOBSEARCH_PRESET_ID, row, str, type Row } from "./data";
import {
  afterDismiss,
  age,
  fitFor,
  followupFor,
  formatBudget,
  formatProposals,
  formatRating,
  formatSpend,
  isNewClient,
  MAX_PROPOSALS,
  overCap,
  partyName,
  paymentVerified,
  parsePrefs,
  postingUrl,
  readPrefsText,
  savePrefs,
  shortId,
  SORTS,
  sourceLabel,
  visible,
  PREFS_KEY,
  type Prefs,
  type Sort,
} from "./queue";
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
export function FitPill({ assessment }: { assessment: unknown }) {
  const fit = fitFor(assessment);
  if (!fit) return null;
  return (
    <div
      className={`${styles.score} ${styles[`score_${fit.band}`]}`}
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
function Meta({
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
      {party ? <span>{party}</span> : null}
      <span>{age(item, now)}</span>
    </div>
  );
}

function FollowupLine({ item, now }: { item: Row; now: number }) {
  const followup = followupFor(item, now);
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

function Card({
  item,
  href,
  now,
  pipeline,
  error,
  onDismiss,
}: {
  item: Row;
  href: string;
  now: number;
  pipeline?: string;
  error?: string;
  onDismiss?: (item: Row) => void;
}) {
  const fields = row(item.fields);
  const title = str(item.title) || "Untitled opportunity";
  const posting = postingUrl(fields);
  const summary = str(row(item.assessment).summary);
  const target = row(item.dismiss);
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
      <FollowupLine item={item} now={now} />
      {summary ? <p className={styles.reason}>{summary}</p> : null}
      {item.preview ? (
        <a href={href} className={styles.preview}>
          {str(item.preview)}
        </a>
      ) : null}
      <div className={styles.actions}>
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
        <CopyButton text={shortId(item.id)} label="Copy short ID" />
        {onDismiss && target.stage_id ? (
          <button
            type="button"
            className={`${styles.iconButton} ${styles.danger}`}
            aria-label={`Dismiss ${title} (move to ${str(target.label)})`}
            title={`Dismiss: move to ${str(target.label)}`}
            onClick={() => onDismiss(item)}
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
  pipelines: Choice[];
  stages: Choice[];
  /** The effective selections (the URL's, or the defaults). */
  pipeline: string;
  stage: string;
  /** Whether the URL named a pipeline or stage; if not, saved ones apply. */
  explicit: boolean;
  /** The GET target for the pipeline and stage selects. */
  action: string;
  hrefs: Record<string, string>;
  /** Pipeline names per id, given only when several pipelines are shown. */
  pipelineNames?: Record<string, string>;
  /** Count wording: jobs when only job-search pipelines are shown. */
  noun: [string, string];
  /** The server's first answer had as many items as it may send. */
  limited: boolean;
  /** A pipeline or stage other than the defaults is selected. */
  filtered: boolean;
  /** Shown when the default view has nothing at all. */
  nothingYet: ReactNode;
  submit?: Submit;
}

/** The job queue: heading with a live count, the controls, and the cards. */
export function JobQueue(props: QueueProps) {
  const [items, setItems] = useState(props.items);
  const saved = parsePrefs(
    useSyncExternalStore(subscribe, savedText, () => null),
  );
  const { hideFull, sort } = saved;
  const [errors, setErrors] = useState<Record<string, string>>({});
  const form = useRef<HTMLFormElement>(null);
  // Only a choice made in the controls is saved; a default is never pinned.
  const prefs = (changes: Partial<Prefs>): Prefs => ({ ...saved, ...changes });

  useEffect(() => {
    // A bare queue URL opens the pipeline and stage chosen last time.
    if (props.explicit || !form.current) return;
    const last = parsePrefs(savedText());
    const pipeline = last.pipeline || props.pipeline;
    const stage = last.stage;
    if (pipeline === props.pipeline && stage === props.stage) return;
    const url = new URL(form.current.action);
    url.searchParams.set("pipeline", pipeline);
    url.searchParams.set("stage", stage);
    window.location.replace(url.href);
    // Runs once: the saved view is applied on arrival, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function choose(changes: Partial<Prefs>) {
    remember(prefs(changes));
    if (changes.pipeline !== undefined || changes.stage !== undefined)
      form.current?.requestSubmit();
  }

  async function dismiss(item: Row) {
    const submit = props.submit;
    const id = str(item.id);
    const target = row(item.dismiss);
    if (!submit || !target.stage_id) return;
    const before = items;
    setErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => key !== id)),
    );
    setItems((current) => afterDismiss(current, id, props.stage));
    let outcome;
    try {
      outcome = await submit("leads.opportunity.transition", {
        ref: item.ref,
        revision: item.revision,
        to_stage_id: target.stage_id,
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
    // Put back only this card, where it was, so another dismiss in flight keeps
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
          ? "This changed since the page loaded. Reload to dismiss it."
          : "Could not dismiss. Try again.",
    }));
  }

  const shown = visible(items, hideFull, sort);
  const hidden = items.length - shown.length;
  const [one, many] = props.noun;
  return (
    <div className={styles.queue}>
      <div className={styles.heading}>
        <h1 className={styles.headingTitle}>Job queue</h1>
        <span className={styles.count} aria-live="polite">
          {shown.length} {shown.length === 1 ? one : many}
        </span>
      </div>
      <form
        ref={form}
        className={styles.controls}
        action={props.action}
        method="get"
        aria-label="Job queue filters"
      >
        <div className={styles.controlGroup}>
          <label className={styles.select}>
            <span>Pipeline</span>
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
          </label>
          <label className={styles.select}>
            <span>Show</span>
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
          </label>
        </div>
        <div className={`${styles.controlGroup} ${styles.controlRight}`}>
          <button
            type="button"
            className={`${styles.chip}${hideFull ? ` ${styles.chipOn}` : ""}`}
            aria-pressed={hideFull}
            onClick={() => choose({ hideFull: !hideFull })}
          >
            Hide 50+ proposals
          </button>
          <label className={styles.select}>
            <span>Sort</span>
            <select
              value={sort}
              onChange={(e) => choose({ sort: e.target.value as Sort })}
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </form>
      {!items.length ? (
        <div className={styles.empty}>
          {props.filtered ? (
            <p>Nothing matches these filters. Try another pipeline or stage.</p>
          ) : (
            props.nothingYet
          )}
        </div>
      ) : !shown.length ? (
        <div className={styles.empty}>
          <p>
            {hidden === 1
              ? "The one job here has 50+ proposals."
              : `All ${hidden} jobs here have 50+ proposals.`}{" "}
            <button
              type="button"
              className={styles.linkButton}
              onClick={() => choose({ hideFull: false })}
            >
              Show them
            </button>
          </p>
        </div>
      ) : (
        <>
          {shown.map((item) => (
            <Card
              key={str(item.id)}
              item={item}
              href={props.hrefs[str(item.id)] ?? ""}
              now={props.now}
              pipeline={props.pipelineNames?.[str(item.pipeline_id)]}
              error={errors[str(item.id)]}
              onDismiss={props.submit ? dismiss : undefined}
            />
          ))}
          {hidden > 0 ? (
            <p className={styles.footnote}>
              {hidden} hidden with 50+ proposals.
            </p>
          ) : null}
          {props.limited ? (
            <p className={styles.footnote}>
              Showing the newest {props.items.length}. Narrow the pipeline or
              stage to see older ones.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

/** The detail page's header for a job-search opportunity. */
export function JobHeader({ item, now }: { item: Row; now: number }) {
  const fields = row(item.fields);
  const title = str(item.title) || "Untitled opportunity";
  const posting = postingUrl(fields);
  const summary = str(row(item.assessment).summary);
  const id = shortId(item.id);
  return (
    <div className={styles.detail}>
      <div className={styles.cardHeader}>
        <div>
          <h1 className={styles.title}>
            {posting ? (
              <a href={posting} target="_blank" rel="noopener noreferrer">
                {title}
              </a>
            ) : (
              title
            )}
          </h1>
          <Badges item={item} />
          <div className={styles.idRow}>
            <CopyButton text={id} label="Copy short ID" className={styles.idChip}>
              <span className={styles.idLabel}>ID</span>
              <span className={styles.idValue}>{id}</span>
            </CopyButton>
          </div>
        </div>
        <FitPill assessment={item.assessment} />
      </div>
      <Meta item={item} now={now} detail />
      {overCap(fields) ? (
        <div className={styles.capWarning} role="alert">
          Over {MAX_PROPOSALS} proposals. Check the current count on the posting
          before you spend anything applying.
        </div>
      ) : null}
      <FollowupLine item={item} now={now} />
      {summary ? <p className={styles.reason}>{summary}</p> : null}
    </div>
  );
}
