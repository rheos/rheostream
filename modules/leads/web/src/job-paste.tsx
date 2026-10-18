"use client";
import { useId, useState, useTransition } from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { row, str } from "./data";
import styles from "./leads.module.css";

export type PasteKind = "search_page" | "posting";
export type PipelineChoice = { value: string; label: string };

/** Mirrors the operation's own limits, so an oversized paste is caught here. */
export const PAGE_LIMIT = 2_000_000;
export const POSTING_LIMIT = 16_000;

/** The operation input for one paste, or the reason it cannot be sent yet. */
export function pasteInput(
  kind: PasteKind,
  pipeline: string,
  content: string,
  url: string,
): Record<string, string> | string {
  if (!pipeline) return "Choose a job search pipeline.";
  if (!content.trim())
    return kind === "posting"
      ? "Paste the posting text."
      : "Paste the search results.";
  if (content.length > (kind === "posting" ? POSTING_LIMIT : PAGE_LIMIT))
    return kind === "posting"
      ? "The posting text exceeds 16,000 characters. Nothing was sent."
      : "The pasted page is too large. Copy just the job list.";
  if (kind === "posting" && !url.trim()) return "Add the posting's link.";
  return kind === "posting"
    ? { pipeline_ref: pipeline, kind, content, posting_url: url.trim() }
    : { pipeline_ref: pipeline, kind, content };
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** One line saying what the paste did, from the operation's counts. */
export function pasteSummary(result: unknown): string {
  const r = row(result);
  const n = (key: string) => Number(r[key] ?? 0) || 0;
  const parts = [
    n("accepted") ? `${plural(n("accepted"), "job", "jobs")} added` : "",
    n("duplicate") ? `${n("duplicate")} already here` : "",
    n("skipped") ? `${n("skipped")} skipped` : "",
  ].filter(Boolean);
  if (!parts.length) return "No jobs were found in what you pasted.";
  const queued = n("accepted")
    ? " Processing is queued; refresh to see the jobs."
    : "";
  return `${parts.join(", ")}.${queued}`;
}

export function JobPaste({
  submit,
  pipelines,
  kinds = ["search_page", "posting"],
  postingUrl = "",
}: {
  submit: NonNullable<ShellApi["submit"]>;
  pipelines: PipelineChoice[];
  /** Offer both kinds (the queue), or only a full posting (a job's detail). */
  kinds?: PasteKind[];
  postingUrl?: string;
}) {
  const id = useId();
  const [kind, setKind] = useState<PasteKind>(kinds[0] ?? "search_page");
  const [pipeline, setPipeline] = useState(pipelines[0]?.value ?? "");
  const [content, setContent] = useState("");
  const [url, setUrl] = useState(postingUrl);
  const [problem, setProblem] = useState("");
  const [outcome, setOutcome] = useState<OperationOutcome | null>(null);
  const [pending, startTransition] = useTransition();
  // A job's own page already knows the posting's link: no field to fill.
  const linkKnown =
    kinds.length === 1 && kinds[0] === "posting" && postingUrl.trim() !== "";
  const warnings =
    outcome?.state === "ok"
      ? (row(outcome.result).warnings as unknown[] | undefined)?.map(str) ?? []
      : [];
  function send(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const input = pasteInput(kind, pipeline, content, url);
    if (typeof input === "string") {
      setProblem(input);
      return;
    }
    setProblem("");
    startTransition(async () => {
      try {
        setOutcome(await submit("leads.jobfeed.paste", input));
      } catch {
        setOutcome({ state: "unavailable" });
      }
    });
  }
  return (
    <form
      className={styles.fieldset}
      onSubmit={send}
      aria-label={kind === "posting" ? "Paste a full posting" : "Paste jobs"}
    >
      {kinds.length > 1 ? (
        <fieldset className={styles.fieldset}>
          <legend>What are you pasting?</legend>
          {kinds.map((value) => (
            <label key={value}>
              <input
                type="radio"
                name={`${id}-kind`}
                value={value}
                checked={kind === value}
                onChange={() => setKind(value)}
              />{" "}
              {value === "posting" ? "One full posting" : "A search results page"}
            </label>
          ))}
        </fieldset>
      ) : null}
      {pipelines.length > 1 ? (
        <div className={styles.field}>
          <label htmlFor={`${id}-pipeline`}>Pipeline</label>
          <select
            id={`${id}-pipeline`}
            value={pipeline}
            onChange={(event) => setPipeline(event.target.value)}
          >
            {pipelines.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {kind === "posting" && !linkKnown ? (
        <div className={styles.field}>
          <label htmlFor={`${id}-url`}>Posting link</label>
          <input
            id={`${id}-url`}
            type="url"
            value={url}
            maxLength={2048}
            onChange={(event) => setUrl(event.target.value)}
          />
        </div>
      ) : null}
      <div className={styles.field}>
        <label htmlFor={`${id}-content`}>
          {kind === "posting" ? "Posting text" : "Search results"}
        </label>
        <textarea
          id={`${id}-content`}
          rows={8}
          value={content}
          aria-describedby={`${id}-hint`}
          onChange={(event) => setContent(event.target.value)}
          onPaste={(event) => {
            // A copied selection of the results carries its HTML; the tiles are
            // only recognisable in it, so it is kept instead of the plain text.
            const html = event.clipboardData.getData("text/html");
            if (kind === "search_page" && html) {
              event.preventDefault();
              setContent(html);
            }
          }}
        />
        <p id={`${id}-hint`} className={styles.meta}>
          {kind === "posting"
            ? "Copy the whole posting page, from its title down to the activity section. Its text becomes the job's description."
            : "Select the job list on a search results page and copy it, or copy the list's HTML from the browser's developer tools. Jobs already here are matched by their link, never duplicated."}
        </p>
      </div>
      <button className={styles.primary} disabled={pending}>
        {pending ? "Adding…" : kind === "posting" ? "Add posting" : "Add jobs"}
      </button>
      <div aria-live="polite" aria-atomic="true">
        {problem ? (
          <p role="alert" className={styles.error}>
            {problem}
          </p>
        ) : null}
        {outcome?.state === "ok" ? (
          <>
            <p className={styles.success}>{pasteSummary(outcome.result)}</p>
            {warnings.length ? (
              <details className={styles.evidence}>
                <summary>What was skipped</summary>
                <ul>
                  {warnings.map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        ) : null}
        {outcome?.state === "refused" ? (
          <p role="alert" className={styles.error}>
            {outcome.text || "The paste was refused. Nothing was added."}
          </p>
        ) : null}
        {outcome?.state === "unavailable" ? (
          <p role="alert" className={styles.error}>
            We could not confirm the paste. Pasting the same content again is
            safe: jobs already received are not added twice.
          </p>
        ) : null}
      </div>
    </form>
  );
}
