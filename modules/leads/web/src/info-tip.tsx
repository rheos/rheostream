"use client";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import styles from "./info-tip.module.css";

/** A footnote that stays out of the way: an info button that opens a small
 * panel under it. Escape or a click elsewhere closes it, and focus returns to
 * the button. The panel is in the markup while closed, hidden, so what it says
 * is never lost to a reader that skips scripts. A block, so it never sits
 * inside a paragraph. On a phone the panel is a sheet at the foot of the
 * screen, so a button at the end of a line never pushes it off the edge. */
export function InfoTip({
  label,
  children,
  align = "end",
  wide = false,
}: {
  /** The button's accessible name, e.g. "About pricing". */
  label: string;
  children: ReactNode;
  /** Which edge of the button the panel lines up with. */
  align?: "start" | "end";
  wide?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const root = useRef<HTMLDivElement | null>(null);
  const button = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // Only take focus back when it was here; Escape in a field elsewhere
      // still closes the tip but leaves the caret where it is.
      // A key event's target is the focused element, or body when none is.
      const target = event.target as HTMLElement | null;
      const inside =
        Boolean(target && root.current?.contains(target)) ||
        target?.tagName === "BODY";
      setOpen(false);
      if (inside) button.current?.focus();
    };
    const onPointer = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node))
        setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onPointer);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <div className={styles.root} ref={root}>
      <button
        ref={button}
        type="button"
        className={`${styles.trigger}${open ? ` ${styles.open}` : ""}`}
        aria-label={label}
        title={label}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((was) => !was)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9.25" />
          <path d="M12 16.5v-5" />
          <path d="M12 8h.01" />
        </svg>
      </button>
      <div
        id={id}
        role="note"
        className={`${styles.panel} ${align === "start" ? styles.start : styles.end}${wide ? ` ${styles.wide}` : ""}`}
        hidden={!open}
      >
        {children}
      </div>
    </div>
  );
}
