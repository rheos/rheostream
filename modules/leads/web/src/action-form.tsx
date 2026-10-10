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
    // "$10,000" and "10 000" both mean 10000; a blank amount clears the value.
    const amount = (values.value_amount ?? "").replace(/[\s,$]/g, "");
    return amount
      ? {
          ...base,
          value_amount: amount,
          value_currency: (values.value_currency ?? "").trim().toUpperCase(),
          value_basis: values.value_basis,
        }
      : {
          ...base,
          value_amount: null,
          value_currency: null,
          value_basis: null,
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
}: FormProps) {
  const id = useId();
  const [result, setResult] = useState<OperationOutcome | null>(null);
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
      setResult(outcome);
      if (outcome.state === "ok")
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
  const receipt =
    result?.state === "ok" ? str(row(result.result).receipt_ref) : "";
  return (
    <form onSubmit={save} className={styles.form}>
      <fieldset disabled={pending} className={styles.fieldset}>
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
          {pending ? "Saving…" : button}
        </button>
      </fieldset>
      <div aria-live="polite" aria-atomic="true">
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
            {result.code === "record_stale"
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
    </form>
  );
}
