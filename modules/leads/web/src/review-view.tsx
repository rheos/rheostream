"use client";
import { useState } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { row, str, type Row } from "./data";
import { holdsOf, without } from "./queue";
import { Meta, StageActions, type Move } from "./queue-view";
import styles from "./queue.module.css";

type Submit = NonNullable<ShellApi["submit"]>;

export interface ReviewProps {
  items: Row[];
  /** Server render time, so every relative time agrees with the markup. */
  now: number;
  hrefs: Record<string, string>;
  /** The server's answer had as many items as it may send. */
  limited: boolean;
  submit?: Submit;
}

function PromoteIcon() {
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
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  );
}

function ReviewCard({
  item,
  href,
  now,
  busy,
  error,
  onPromote,
  onMove,
}: {
  item: Row;
  href: string;
  now: number;
  busy: boolean;
  error?: string;
  onPromote?: (item: Row) => void;
  onMove?: (item: Row, to: Move) => void;
}) {
  const title = str(item.title) || "Untitled job";
  const holds = holdsOf(item);
  return (
    <article className={styles.card}>
      <div className={styles.cardHeader}>
        <div>
          <h2 className={styles.title}>
            <a href={href}>{title}</a>
          </h2>
          {holds.length ? (
            <div className={styles.holdBadges}>
              {holds.map((hold, index) => (
                <span
                  key={`${hold.code}-${index}`}
                  className={`${styles.badge} ${styles.badgeHold}`}
                  title={hold.text}
                >
                  {hold.label}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      <Meta item={item} now={now} />
      {holds.length ? (
        <ul className={styles.holdReasons} aria-label="Why the filters held it">
          {holds.map((hold, index) => (
            <li key={`${hold.code}-${index}`}>{hold.text}</li>
          ))}
        </ul>
      ) : null}
      {item.preview ? (
        <a href={href} className={styles.preview}>
          {str(item.preview)}
        </a>
      ) : null}
      <StageActions
        item={item}
        onMove={onMove}
        copyId
        disabled={busy}
        stageSelect={false}
        lead={
          onPromote ? (
            <button
              type="button"
              className={styles.iconButton}
              aria-label={`Promote ${title}: score it past the filters`}
              title="Promote: score it past the filters"
              disabled={busy}
              onClick={() => onPromote(item)}
            >
              <PromoteIcon />
            </button>
          ) : null
        }
      />
      {error ? (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

/** The review queue: jobs the client filters held back, newest first, each with
 * the clauses that held it. Promote sends one to the model past the filters;
 * Dismiss closes it as the queue's Dismiss does. Either way the card leaves. */
export function ReviewQueue(props: ReviewProps) {
  const [items, setItems] = useState(props.items);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");

  async function act(
    item: Row,
    run: (submit: Submit) => ReturnType<Submit>,
    done: string,
    failed: (code: string | undefined) => string,
  ) {
    const submit = props.submit;
    const id = str(item.id);
    if (!submit || busy[id]) return;
    setBusy((current) => ({ ...current, [id]: true }));
    setErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => key !== id)),
    );
    let outcome;
    try {
      outcome = await run(submit);
    } catch {
      outcome = { state: "unavailable" as const };
    }
    setBusy((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => key !== id)),
    );
    if (outcome.state === "ok") {
      setItems((current) => without(current, id));
      setNotice(done);
      return;
    }
    setErrors((current) => ({
      ...current,
      [id]: failed(outcome.state === "refused" ? outcome.code : undefined),
    }));
  }

  function promote(item: Row) {
    const title = str(item.title) || "The job";
    return act(
      item,
      (submit) => submit("leads.jobreview.promote", { ref: item.ref }),
      `${title} is promoted: it is scored shortly and then shows in the job queue.`,
      (code) =>
        code === "not_held"
          ? "The filters no longer hold this job. Reload to see the queue as it is now."
          : "Could not promote it. Try again.",
    );
  }

  function move(item: Row, to: Move) {
    const title = str(item.title) || "The job";
    return act(
      item,
      (submit) =>
        submit("leads.opportunity.transition", {
          ref: item.ref,
          revision: item.revision,
          to_stage_id: to.stage_id,
        }),
      `${title} is dismissed (moved to ${to.label}).`,
      (code) =>
        code === "record_stale"
          ? "This changed since the page loaded. Reload to dismiss it."
          : `Could not move it to ${to.label}. Try again.`,
    );
  }

  return (
    <div className={styles.queue}>
      <div className={styles.heading}>
        <h1 className={styles.headingTitle}>Review queue</h1>
        <span className={styles.count} aria-live="polite">
          {items.length} held {items.length === 1 ? "job" : "jobs"}
        </span>
      </div>
      <p className={styles.reviewIntro}>
        Jobs the client filters held back before scoring. Promote one to have
        it scored and drafted like any other job; the clauses that held it stay
        on its assessment as things to check.
      </p>
      <p className={styles.srOnly} role="status">
        {notice}
      </p>
      {!items.length ? (
        <div className={styles.empty}>
          <p>Nothing is held back right now.</p>
        </div>
      ) : (
        <>
          {items.map((item) => {
            const id = str(item.id);
            return (
              <ReviewCard
                key={id}
                item={item}
                href={props.hrefs[id] ?? ""}
                now={props.now}
                busy={busy[id] === true}
                error={errors[id]}
                onPromote={props.submit ? promote : undefined}
                onMove={
                  props.submit && row(item.dismiss).stage_id ? move : undefined
                }
              />
            );
          })}
          {props.limited ? (
            <p className={styles.footnote}>
              Showing the newest {props.items.length}. Dismiss or promote some
              to see the rest.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
