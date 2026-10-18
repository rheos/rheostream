"use client";

import { useEffect, useRef, type ReactNode } from "react";

import styles from "./settings-menu.module.css";

/**
 * The header's one settings button (#408). Workspace, appearance and sign-out
 * used to sit in the header itself; they are rarely touched, so they wait here.
 *
 * A native disclosure, so it opens and its forms (sign-out is a plain POST) work
 * before hydration and without scripts. Scripts add the menu manners: Escape or a
 * click outside closes it, and Escape returns focus to the button.
 */
export function SettingsMenu({ children }: { children: ReactNode }) {
  const root = useRef<HTMLDetailsElement | null>(null);

  useEffect(() => {
    const details = root.current;
    if (!details) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || !details.open) return;
      const target = event.target as Node | null;
      if (!target || !details.contains(target)) return;
      details.open = false;
      details.querySelector("summary")?.focus();
    };
    const onPointer = (event: PointerEvent) => {
      if (details.open && !details.contains(event.target as Node)) details.open = false;
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onPointer);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onPointer);
    };
  }, []);

  return (
    <details className={styles.root} ref={root}>
      <summary className={styles.trigger} title="Settings">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        <span className={styles.label}>Settings</span>
      </summary>
      <div className={styles.panel}>{children}</div>
    </details>
  );
}

/** One titled block inside the menu. */
export function SettingsSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.section}>
      <h2 className={styles.sectionTitle}>{title}</h2>
      {children}
    </section>
  );
}
