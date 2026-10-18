"use client";
import { useEffect, useId, useRef, useState } from "react";
import { ActionForm, type FormProps } from "./action-form";
import styles from "./leads.module.css";

/** A small button that opens one form in a modal dialog, so a rare edit adds
 * no text box to the page until it is wanted (#418). A native modal dialog:
 * focus moves into it, Escape closes it, and the page behind is inert. It
 * closes itself once the form saves; the screen then refreshes with the change. */
export function ModalForm({
  trigger,
  title,
  ...form
}: Omit<FormProps, "onResult"> & {
  /** The button's text, e.g. "Add note". */
  trigger: string;
  /** The dialog's heading. */
  title: string;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const heading = useId();
  // A fresh form each time it opens, so a saved entry never reappears.
  const [round, setRound] = useState(0);
  // Once the fresh form is in, straight to its first field, not Close.
  useEffect(() => {
    if (round)
      dialog.current
        ?.querySelector<HTMLElement>("textarea, input, select")
        ?.focus();
  }, [round]);

  return (
    <>
      <button
        type="button"
        className={styles.textButton}
        onClick={() => {
          setRound((n) => n + 1);
          dialog.current?.showModal();
        }}
      >
        {trigger}
      </button>
      <dialog
        ref={dialog}
        className={styles.modal}
        aria-labelledby={heading}
      >
        <div className={styles.modalHead}>
          <h2 id={heading} className={styles.modalTitle}>
            {title}
          </h2>
          <button
            type="button"
            className={styles.modalClose}
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            <svg
              width="16"
              height="16"
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
        </div>
        <ActionForm
          key={round}
          {...form}
          onResult={(shown) => {
            if (shown.state === "ok") dialog.current?.close();
          }}
        />
      </dialog>
    </>
  );
}
