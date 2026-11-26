import type { ReactNode } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { words } from "./data";
import { sinkName, statusText } from "./names";
import type { Loaded, Strip } from "./load";
import styles from "./finance.module.css";

/**
 * The pieces every Finance screen shares: the frame and its tabs, the status strip,
 * the footnote popover, and the failure and owner-only states.
 *
 * No client script anywhere. A footnote is the platform's own popover
 * (`popover="auto"` opened by a `popovertarget` button), so it opens, closes on
 * Escape or a click outside, and returns focus with no JavaScript of ours.
 */

export const TABS = [
  ["owed", "Owed"],
  ["upcoming", "Upcoming"],
  ["attention", "Needs attention"],
  ["coverage", "Coverage"],
] as const;
export type Tab = (typeof TABS)[number][0];

/** A popover id from any key: the platform needs one per page, unique. Each
 * character outside [A-Za-z0-9] becomes `_` and its code point, so two keys never
 * share an id. */
export function popoverId(...parts: (string | number)[]): string {
  return (
    "fin-" +
    parts
      .join("-")
      .replace(/[^A-Za-z0-9]/g, (c) => `_${c.codePointAt(0)?.toString(16) ?? ""}`)
  );
}

export function InfoIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={styles.icon}>
      <circle cx="12" cy="12" r="9.25" />
      <path d="M12 16.5v-5" />
      <path d="M12 8h.01" />
    </svg>
  );
}

export function BellIcon({ title }: { title: string }) {
  return (
    <svg viewBox="0 0 24 24" role="img" aria-label={title} className={styles.icon}>
      <title>{title}</title>
      <path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" />
      <path d="M10 20.5a2 2 0 0 0 4 0" />
    </svg>
  );
}

/** A quiet info button and the note it opens. */
export function Footnote({
  id,
  label,
  title,
  children,
  wide = false,
}: {
  id: string;
  /** The button's accessible name, e.g. "About this bill". */
  label: string;
  /** The note's heading. */
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <>
      <button
        type="button"
        className={styles.tip}
        popoverTarget={id}
        aria-label={label}
        title={label}
      >
        <InfoIcon />
      </button>
      <div
        id={id}
        popover="auto"
        role="dialog"
        aria-labelledby={`${id}-title`}
        className={`${styles.note}${wide ? ` ${styles.noteWide}` : ""}`}
      >
        <div className={styles.noteHead}>
          <h3 id={`${id}-title`}>{title}</h3>
          <button
            type="button"
            className={styles.noteClose}
            popoverTarget={id}
            popoverTargetAction="hide"
          >
            Close
          </button>
        </div>
        {children}
      </div>
    </>
  );
}

export function Frame({
  shell,
  active,
  strip,
  children,
}: {
  shell: ShellApi;
  active: Tab;
  strip: Loaded<Strip>;
  children: ReactNode;
}) {
  return (
    <section className={styles.surface}>
      <header className={styles.header}>
        <h1 className={styles.heading}>Finance</h1>
        <StatusStrip strip={strip} />
      </header>
      <nav className={styles.tabs} aria-label="Finance">
        {TABS.map(([id, text]) => (
          <a
            key={id}
            href={shell.href(id)}
            aria-current={active === id ? "page" : undefined}
          >
            {text}
          </a>
        ))}
      </nav>
      {children}
    </section>
  );
}

type Tone = "ok" | "warn" | "bad" | "off";

function Chip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`${styles.chip} ${styles[tone]}`}>
      <span className={styles.dot} aria-hidden="true" />
      {children}
    </span>
  );
}

/** A module stamp ("2026-01-05 09:15 UTC") as "at 09:15 UTC" when it is today. */
export function sinceStamp(stamp: string, today: string | undefined): string {
  return today && stamp.startsWith(today + " ") ? `at ${stamp.slice(today.length + 1)}` : stamp;
}

/** Last tick, last sync to Current and the sinks, with every status line behind
 * the info button. */
export function StatusStrip({ strip }: { strip: Loaded<Strip> }) {
  if (strip.state !== "ok")
    return (
      <p className={styles.strip}>
        <Chip tone="bad">Status unavailable</Chip>
      </p>
    );
  const { facts, lines, sinks } = strip.value;
  const reminders = lines.find((l) => l.lineNo === 6)?.text ?? "";
  const today = facts.asOf?.match(/\d{4}-\d{2}-\d{2}/)?.[0];
  const tick: [Tone, string] = facts.noTickYet
    ? ["warn", "No tick yet"]
    : facts.tickMinutes === null
      ? ["warn", "Tick unknown"]
      : [
          facts.tickStale ? "bad" : "ok",
          `Ticked ${facts.tickMinutes === 0 ? "just now" : `${facts.tickMinutes} min ago`}`,
        ];
  const sync: [Tone, string] = facts.syncFailedSince
    ? ["bad", `Current sync failing since ${facts.syncFailedSince}`]
    : facts.lastSync
      ? ["ok", `Synced to Current ${sinceStamp(facts.lastSync, today)}`]
      : reminders.endsWith("current off")
        ? ["off", "Current sync off"]
        : reminders.endsWith("current not configured")
          ? ["warn", "Current not set up"]
          : ["warn", "No completed sync yet"];
  const on = sinks?.filter((s) => s.enabled) ?? [];
  const stale = sinks?.filter((s) => s.approval === "stale") ?? [];
  const sinkText =
    sinks === null
      ? "Sinks unknown"
      : on.length === 0
        ? "All sinks off"
        : `Sending to ${on.map((s) => sinkName(s.sink)).join(", ")}`;
  return (
    <div className={styles.strip}>
      {facts.asOf && !facts.databaseCurrent ? (
        <Chip tone="bad">Database needs an upgrade</Chip>
      ) : null}
      <Chip tone={tick[0]}>{tick[1]}</Chip>
      <Chip tone={sync[0]}>{sync[1]}</Chip>
      <Chip tone={sinks === null ? "warn" : stale.length ? "warn" : "ok"}>
        {sinkText}
        {stale.length
          ? `, ${stale.length} ${stale.length === 1 ? "approval" : "approvals"} stale`
          : ""}
      </Chip>
      <Footnote id={popoverId("status")} label="All status lines" title="Status" wide>
        {facts.asOf ? <p className={styles.faint}>{facts.asOf}</p> : null}
        <ol className={styles.lines}>
          {lines.map((line) => (
            <li key={line.lineNo} value={line.lineNo}>
              {statusText(line)}
            </li>
          ))}
        </ol>
        {sinks?.length ? (
          <>
            <h4>Sinks</h4>
            <ul className={styles.plain}>
              {sinks.map((s) => (
                <li key={s.sink}>
                  <strong>{sinkName(s.sink)}</strong>: {s.enabled ? "sending" : "off"}
                  {s.settingEnabled && !s.enabled ? " (setting on, approval " + s.approval + ")" : ""}
                  {!s.settingEnabled && s.approval !== "none" ? ` (approval ${s.approval})` : ""}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </Footnote>
    </div>
  );
}

/** A read that did not answer, in words the owner can act on. */
export function Failure({ what, outcome }: { what: string; outcome: Loaded<unknown> }) {
  if (outcome.state === "ok") return null;
  const code = outcome.state === "refused" ? outcome.code : null;
  return (
    <div className={styles.empty} role="alert">
      <h2>Could not load {what}</h2>
      <p>
        {code === "finance_layout_invalid" || code === "finance_layout_missing"
          ? "Finance's data folder is not mounted where the server runs."
          : code === "finance_schema_behind"
            ? "Finance's database is behind the code; the module needs an upgrade."
            : code
              ? `Finance refused the read (${words(code)}).`
              : "The service did not respond. Reload to try again."}
      </p>
    </div>
  );
}

export function OwnerOnly() {
  return (
    <section className={styles.surface}>
      <div className={styles.empty}>
        <h2>Finance is for the workspace owner</h2>
        <p>Only the owner can see the ledger, bills and documents.</p>
      </div>
    </section>
  );
}
