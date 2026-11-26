"use client";
import { useState, useTransition, type FormEvent } from "react";
import type { OperationOutcome, ShellApi } from "@rheo-stream/web-contract/screen";

import styles from "./finance.module.css";

export const REPORT_PAID = "finance.obligation.report_paid";
export const UNREPORT_PAID = "finance.obligation.unreport_paid";

type Submit = NonNullable<ShellApi["submit"]>;

export interface PaidFormProps {
  submit: Submit;
  /** Renders the screen again in place; without it the page reloads. */
  refresh?: () => Promise<void>;
  operation: typeof REPORT_PAID | typeof UNREPORT_PAID;
  obligationRef: string;
  /** What the button does, for assistive technology: "Mark Electricity Nov paid". */
  label: string;
}

/** What a refusal says. The codes are the module's own fixed ones. */
export function refusalText(outcome: OperationOutcome): string {
  if (outcome.state === "unavailable") return "No answer. Try again.";
  if (outcome.state !== "refused") return "";
  switch (outcome.code) {
    case "finance_writer_busy":
      return "Finance is updating. Try again in a minute.";
    case "finance_operation_refused":
      return "That changed since the page loaded. Reload.";
    default:
      return "Not saved. Reload and try again.";
  }
}

/**
 * One button that reports a bill paid, or undoes it. It posts, so a click before
 * the script loads sends nothing in a URL; once loaded it submits through the
 * shell's form action and renders the screen again.
 */
export function PaidForm({ submit, refresh, operation, obligationRef, label }: PaidFormProps) {
  const [failed, setFailed] = useState("");
  const [pending, startTransition] = useTransition();
  const undo = operation === UNREPORT_PAID;
  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFailed("");
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit(operation, { obligation_ref: obligationRef });
      } catch {
        outcome = { state: "unavailable" };
      }
      if (outcome.state !== "ok") {
        setFailed(refusalText(outcome));
        return;
      }
      if (refresh) await refresh();
      else window.location.reload();
    });
  }
  return (
    <form method="post" onSubmit={send} className={styles.paidForm} aria-busy={pending}>
      <button
        type="submit"
        className={undo ? styles.undo : styles.paid}
        disabled={pending}
        aria-label={label}
        title={label}
      >
        {pending ? "Saving…" : undo ? "Undo" : "Paid"}
      </button>
      <span aria-live="polite" className={styles.paidFailed}>
        {failed}
      </span>
    </form>
  );
}
