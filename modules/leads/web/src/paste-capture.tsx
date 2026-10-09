"use client";
import { useId, useRef, useState, useTransition } from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { row, str } from "./data";
import { PASTE_LIMIT, pastedInput, suggestInquiry } from "./paste";
import styles from "./leads.module.css";

const CONTACT_FIELDS = [
  ["person.name", "Name", "text"],
  ["person.email", "Email", "email"],
  ["person.phone", "Phone", "text"],
  ["organization.name", "Organization", "text"],
] as const;
export function PasteCapture({
  submit,
  funnels,
  receiptHref,
}: {
  submit: NonNullable<ShellApi["submit"]>;
  funnels: { value: string; label: string }[];
  receiptHref: string;
}) {
  const id = useId();
  const [source, setSource] = useState("");
  const [review, setReview] = useState<Record<string, string> | null>(null);
  const [funnel, setFunnel] = useState(funnels[0]?.value ?? "");
  const [result, setResult] = useState<OperationOutcome | null>(null);
  const [pending, startTransition] = useTransition();
  const title = useRef<HTMLInputElement>(null);
  const sourceInput = useRef<HTMLTextAreaElement>(null);
  const saved = result?.state === "ok";
  const receipt = saved ? str(row(result.result).receipt_ref) : "";
  function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review || saved || pending) return;
    startTransition(async () => {
      try {
        setResult(
          await submit(
            "leads.intake.capture",
            pastedInput(source, { ...review, funnel_ref: funnel }),
          ),
        );
      } catch {
        setResult({ state: "unavailable" });
      }
    });
  }
  function reset() {
    setSource("");
    setReview(null);
    setResult(null);
    requestAnimationFrame(() => sourceInput.current?.focus());
  }
  return (
    <section className={styles.capture} aria-label="Paste an inquiry">
      {!review ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!source.trim()) return;
            setReview(suggestInquiry(source));
            requestAnimationFrame(() => title.current?.focus());
          }}
          className={styles.fieldset}
        >
          <div className={styles.field}>
            <label htmlFor={`${id}-source`}>
              Paste an email, message, or conversation
            </label>
            <textarea
              ref={sourceInput}
              id={`${id}-source`}
              value={source}
              onChange={(event) => setSource(event.target.value)}
              required
              maxLength={PASTE_LIMIT}
              rows={10}
              aria-describedby={`${id}-hint`}
            />
            <p id={`${id}-hint`} className={styles.meta}>
              Keep the original wording. Nothing is saved until you review and
              capture it. Up to 16,000 characters.
            </p>
          </div>
          <button className={styles.primary} disabled={!source.trim()}>
            Review inquiry
          </button>
        </form>
      ) : (
        <>
          <h2>{saved ? "Inquiry received" : "Review your inquiry"}</h2>
          <p className={styles.intro}>
            The original text stays attached. Check the suggested details;
            ambiguous information is left blank.
          </p>
          <details className={styles.evidence}>
            <summary>Original text</summary>
            <p className={styles.note}>{source}</p>
          </details>
          <form onSubmit={save} className={styles.form}>
            <fieldset disabled={pending || saved} className={styles.fieldset}>
              <div className={styles.field}>
                <label htmlFor={`${id}-title`}>Inquiry title</label>
                <input
                  ref={title}
                  id={`${id}-title`}
                  value={review.subject}
                  required
                  maxLength={512}
                  onChange={(event) =>
                    setReview({ ...review, subject: event.target.value })
                  }
                />
              </div>
              <div className={styles.field}>
                <label htmlFor={`${id}-funnel`}>Funnel</label>
                <select
                  id={`${id}-funnel`}
                  value={funnel}
                  onChange={(event) => setFunnel(event.target.value)}
                >
                  {funnels.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.label}
                    </option>
                  ))}
                </select>
              </div>
              <details className={styles.evidence} open>
                <summary>Contact details (optional)</summary>
                <div className={styles.fieldset}>
                  {CONTACT_FIELDS.map(([key, label, type]) => (
                    <div className={styles.field} key={key}>
                      <label htmlFor={`${id}-${key}`}>{label}</label>
                      <input
                        id={`${id}-${key}`}
                        type={type}
                        value={review[key]}
                        maxLength={key === "person.phone" ? 128 : 512}
                        onChange={(event) =>
                          setReview({ ...review, [key]: event.target.value })
                        }
                      />
                    </div>
                  ))}
                </div>
              </details>
              <p className={styles.meta}>
                Contact details remain unverified. Your connection’s routing
                decides whether to create an opportunity.
              </p>
              <div className={styles.captureActions}>
                <button className={styles.primary}>
                  {pending ? "Saving…" : "Capture inquiry"}
                </button>
                <button
                  type="button"
                  className={styles.secondary}
                  onClick={() => {
                    setReview(null);
                    setResult(null);
                    requestAnimationFrame(() => sourceInput.current?.focus());
                  }}
                >
                  Edit original text
                </button>
              </div>
            </fieldset>
          </form>
          <div aria-live="polite" aria-atomic="true">
            {saved ? (
              <p className={styles.success}>
                Processing is queued.{" "}
                {receipt ? (
                  <a href={`${receiptHref}?ref=${encodeURIComponent(receipt)}`}>
                    Check processing and open the opportunity
                  </a>
                ) : null}
              </p>
            ) : null}
            {result?.state === "refused" ? (
              <p role="alert" className={styles.error}>
                {result.text ||
                  "The capture was refused. Your text is still here; check your access before retrying."}
              </p>
            ) : null}
            {result?.state === "unavailable" ? (
              <p role="alert" className={styles.error}>
                We could not confirm the save. Your text is still here. Check
                recent opportunities before retrying to avoid a duplicate.
              </p>
            ) : null}
          </div>
          {saved ? (
            <button type="button" className={styles.secondary} onClick={reset}>
              Capture another inquiry
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
