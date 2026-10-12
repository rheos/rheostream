"use client";
import { useId, useState, useTransition } from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { row, str } from "./data";
import styles from "./leads.module.css";

export interface Field {
  name: string;
  label: string;
  value?: string;
  required?: boolean;
  maxLength?: number;
  type?: "text" | "email" | "date" | "textarea" | "select";
  options?: { value: string; label: string }[];
}
export interface FormProps {
  submit: NonNullable<ShellApi["submit"]>;
  operation: string;
  base?: Record<string, unknown>;
  fields: Field[];
  button: string;
  receiptHref?: string;
  /** On success, open the URL in this result field instead of saying "Saved." */
  redirectField?: string;
  /** Plain copy for known refusal codes, in place of the generic refused text. */
  refusalCopy?: Record<string, string>;
}
type Shown = OperationOutcome | { state: "redirecting" };
type PageLocation = Pick<Location, "assign" | "protocol" | "hostname">;
/** The URL to open, or null. Only https, or http while the app itself is
 * served as plain http on localhost for development. */
export function safeRedirect(
  value: string,
  location: PageLocation,
): string | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol === "https:") return url.href;
  if (
    url.protocol === "http:" &&
    location.protocol === "http:" &&
    location.hostname === "localhost"
  )
    return url.href;
  return null;
}
/** Navigates for a redirecting form's success; an unsafe or missing URL reads
 * as a refusal with no text, so the generic refused message shows. */
export function settle(
  outcome: OperationOutcome,
  redirectField: string | undefined,
  location: PageLocation,
): Shown {
  if (!redirectField || outcome.state !== "ok") return outcome;
  const target = safeRedirect(
    str(row(outcome.result)[redirectField]),
    location,
  );
  if (!target) return { state: "refused", code: "redirect_refused", text: "" };
  location.assign(target);
  return { state: "redirecting" };
}
export function formInput(
  operation: string,
  base: Record<string, unknown>,
  values: Record<string, string>,
): Record<string, unknown> {
  if (operation === "leads.intake.capture") {
    const { funnel_ref, ...body } = values;
    return {
      funnel_ref,
      body: Object.fromEntries(
        Object.entries(body).filter(([, value]) => value.trim() !== ""),
      ),
    };
  }
  if (operation === "leads.connection.set_routing")
    return {
      ...base,
      rules: values.pipeline_id
        ? [{ action: "create_opportunity", pipeline_id: values.pipeline_id }]
        : [{ action: "record_only" }],
    };
  if (operation === "leads.opportunity.update" && "value_amount" in values) {
    // Only a blank amount clears the value. "$10,000" and "10 000" are read as
    // 10000; anything else is sent as typed so the operation refuses it.
    const raw = (values.value_amount ?? "").trim();
    if (!raw)
      return {
        ...base,
        value_amount: null,
        value_currency: null,
        value_basis: null,
      };
    const grouped = raw.match(/^\$?\s*(\d{1,3}(?:[ ,]\d{3})+|\d+)(\.\d+)?$/);
    return {
      ...base,
      value_amount: grouped
        ? `${(grouped[1] ?? "").replace(/[ ,]/g, "")}${grouped[2] ?? ""}`
        : raw,
      value_currency: (values.value_currency ?? "").trim().toUpperCase(),
      value_basis: values.value_basis,
    };
  }
  if (operation === "leads.qualification.assess" && "fit" in values) {
    const { objective, ...assessment } = values;
    return {
      ...base,
      objective,
      assessment: { ...assessment, author: "human" },
    };
  }
  return { ...base, ...values };
}
export function ActionForm({
  submit,
  operation,
  base = {},
  fields,
  button,
  receiptHref,
  redirectField,
  refusalCopy,
}: FormProps) {
  const id = useId();
  const [result, setResult] = useState<Shown | null>(null);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((field) => [
        field.name,
        field.value ??
          (field.type === "select" ? (field.options?.[0]?.value ?? "") : ""),
      ]),
    ),
  );
  const [pending, startTransition] = useTransition();
  function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const submitted = Object.fromEntries(
      fields.map((field) => [field.name, String(form.get(field.name) ?? "")]),
    );
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit(
          operation,
          formInput(operation, base, submitted),
        );
      } catch {
        outcome = { state: "unavailable" };
      }
      const shown = settle(outcome, redirectField, window.location);
      setResult(shown);
      if (shown.state === "ok")
        setValues(
          Object.fromEntries(
            fields.map((field) => [
              field.name,
              field.value === undefined && field.type !== "select"
                ? ""
                : (submitted[field.name] ?? ""),
            ]),
          ),
        );
    });
  }
  const update = (name: string, value: string) =>
    setValues((before) => ({ ...before, [name]: value }));
  const redirecting = result?.state === "redirecting";
  return (
    <form onSubmit={save} className={styles.form}>
      <fieldset disabled={pending || redirecting} className={styles.fieldset}>
        {fields.map((field) => (
          <div className={styles.field} key={field.name}>
            <label htmlFor={`${id}-${field.name}`}>
              {field.label}
              {field.required ? "" : " (optional)"}
            </label>
            {field.type === "textarea" ? (
              <textarea
                id={`${id}-${field.name}`}
                name={field.name}
                value={values[field.name] ?? ""}
                onChange={(event) => update(field.name, event.target.value)}
                required={field.required}
                maxLength={field.maxLength ?? 16000}
                rows={5}
              />
            ) : field.type === "select" ? (
              <select
                id={`${id}-${field.name}`}
                name={field.name}
                value={values[field.name] ?? ""}
                onChange={(event) => update(field.name, event.target.value)}
                required={field.required}
              >
                {field.options?.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={`${id}-${field.name}`}
                name={field.name}
                type={field.type ?? "text"}
                value={values[field.name] ?? ""}
                onChange={(event) => update(field.name, event.target.value)}
                required={field.required}
                maxLength={field.maxLength ?? 2000}
              />
            )}
          </div>
        ))}
        <button type="submit" className={styles.primary}>
          {pending || redirecting
            ? redirectField
              ? "Opening authorization…"
              : "Saving…"
            : button}
        </button>
      </fieldset>
      <FormResult
        result={result}
        receiptHref={receiptHref}
        refusalCopy={refusalCopy}
      />
    </form>
  );
}
/** The live region under a form. It renders fixed copy only, never a URL. */
export function FormResult({
  result,
  receiptHref,
  refusalCopy,
}: {
  result: Shown | null;
  receiptHref?: string;
  refusalCopy?: Record<string, string>;
}) {
  const receipt =
    result?.state === "ok" ? str(row(result.result).receipt_ref) : "";
  const mapped =
    result?.state === "refused" && refusalCopy
      ? Object.hasOwn(refusalCopy, result.code)
        ? refusalCopy[result.code]
        : undefined
      : undefined;
  return (
    <div aria-live="polite" aria-atomic="true">
      {result?.state === "redirecting" ? (
        <p className={styles.meta}>Opening authorization…</p>
      ) : null}
      {result?.state === "ok" ? (
        <p className={styles.success}>
          {receipt ? "Inquiry received. Processing is queued." : "Saved."}
          {receipt && receiptHref ? (
            <>
              {" "}
              <a href={`${receiptHref}?ref=${encodeURIComponent(receipt)}`}>
                Check processing and open the opportunity
              </a>
            </>
          ) : null}
        </p>
      ) : null}
      {result?.state === "refused" ? (
        <p className={styles.error} role="alert">
          {mapped
            ? mapped
            : result.code === "record_stale"
              ? "This record changed. Your entry is still here; copy it before reloading, then try saving again."
              : result.text ||
                "The change was refused. Check your access and try again."}
        </p>
      ) : null}
      {result?.state === "unavailable" ? (
        <p className={styles.error} role="alert">
          We could not confirm the save. Check the record before retrying to
          avoid a duplicate.
        </p>
      ) : null}
    </div>
  );
}
