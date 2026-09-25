import type { ReactNode } from "react";

import styles from "./state-message.module.css";

/** The one error copy every screen falls back to for `unavailable` or an unnamed refusal. */
export const GENERIC_ERROR = "Memory is unavailable right now.";

/**
 * A named screen state: an empty result, a bound the view cannot show past, or an
 * error. `name` is written to `data-state` so each state stays distinguishable in the
 * markup even where two share a tone.
 */
export function StateMessage({
  name,
  tone = "neutral",
  children,
}: {
  name: string;
  tone?: "neutral" | "error";
  children: ReactNode;
}) {
  return (
    <div
      className={tone === "error" ? styles.error : styles.neutral}
      data-state={name}
      role="status"
    >
      {children}
    </div>
  );
}

export function GenericError() {
  return (
    <StateMessage name="error" tone="error">
      <p className={styles.text}>{GENERIC_ERROR}</p>
    </StateMessage>
  );
}
