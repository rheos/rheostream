"use client";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useTransition,
  type ReactNode,
} from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import {
  PRIORITY_LABEL,
  SNOOZE_PICKS,
  customSnooze,
  refusalText,
  snoozeAt,
  type AreaIcon,
  type AreaTone,
  type Priority,
  type SnoozePick,
  type Status,
} from "./data";
import { ProjectPicker, type PickableProject } from "./forms";
import { Icon, type ActionIcon } from "./icons";
import styles from "./current.module.css";

type Submit = NonNullable<ShellApi["submit"]>;

/** What one desk row shows, all of it worked out on the server. */
export interface DeskRowView {
  ref: string;
  title: string;
  status: Status;
  priority: Priority;
  revision: number;
  href: string;
  areaName: string;
  areaIcon: AreaIcon;
  areaTone: AreaTone;
  tag: string | null;
  /** "Due Jan 5", or empty. */
  due: string;
  overdue: boolean;
  blocked: boolean;
  /** "Until Jan 5, 9:00 AM" for a snoozed item that has not woken. */
  until: string;
  signals: number;
  /** "26d ago". */
  age: string;
  projectRef: string | null;
}

interface Move {
  to: Status;
  label: string;
  icon: ActionIcon;
}
/** The swipe-right action: watch an item, or stop watching one. */
function leftMove(status: Status): Move | null {
  if (status === "done") return null;
  return status === "waiting"
    ? { to: "open", label: "Unwatch", icon: "eyeOff" }
    : { to: "waiting", label: "Watch", icon: "eye" };
}
/** The swipe-left action: finish an item, or reopen a finished one. */
function rightMove(status: Status): Move {
  return status === "done"
    ? { to: "open", label: "Reopen", icon: "undo" }
    : { to: "done", label: "Done", icon: "check" };
}

async function setStatus(
  submit: Submit,
  row: { ref: string; revision: number },
  to: Status,
  until?: Date,
): Promise<OperationOutcome> {
  try {
    return await submit("current.item.set_status", {
      ref: row.ref,
      revision: row.revision,
      status: to,
      snoozed_until: until ? until.toISOString() : null,
    });
  } catch {
    return { state: "unavailable" };
  }
}

/** How long the row must sit still at an end before the long swipe counts. */
const SETTLE_MS = 140;

/**
 * One queue row in the predecessor desk's shape: area tile, two-line title,
 * priority on the right, a meta line and a priority stripe.
 *
 * On touch the row is a horizontal scroller with snap points: swipe left to
 * reveal Project and Done, right to reveal Watch and Snooze; a long swipe to
 * either end runs Done or Watch. The tray buttons are real buttons, so Tab and
 * screen readers reach every action without swiping; focusing one scrolls it
 * into view. With a mouse the same buttons show as icons on hover or focus.
 * No inline style: the scroller moves by scroll position alone.
 */
export function DeskRow({
  row,
  submit,
  zone,
  projects,
  reloadHref,
}: {
  row: DeskRowView;
  submit?: Submit;
  zone?: string;
  /** Projects the row's project control offers; without them, no control. */
  projects?: readonly PickableProject[];
  reloadHref?: string;
}) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const leftTray = useRef<HTMLDivElement | null>(null);
  const touching = useRef(false);
  /** Where the scroller sat when a finger went down; null when no swipe is
   * armed. Only a touch that moves the row toward an end may run its action,
   * so focus, a tap, a re-layout or a programmatic scroll never does. */
  const swipeFrom = useRef<number | null>(null);
  const settle = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [result, setResult] = useState<OperationOutcome | null>(null);
  // The row version an action left from. The row reads as leaving only while
  // it is still that version, so one that stays in view after a success (the
  // All filter, snooze again) comes back live with its new revision.
  const version = `${row.revision}:${row.status}`;
  const [leftFrom, setLeftFrom] = useState<string | null>(null);
  const leaving = leftFrom === version;
  const [snoozing, setSnoozing] = useState(false);
  const [pending, startTransition] = useTransition();
  const left = leftMove(row.status);
  const right = rightMove(row.status);
  const actionable = Boolean(submit);
  // A timer reads the newest row, never the one from the render that set it.
  const latest = useRef({ row, pending, left, right });
  useLayoutEffect(() => {
    latest.current = { row, pending, left, right };
  });

  function close(smooth = true) {
    const el = scroller.current;
    const tray = leftTray.current;
    if (!el || !tray) return;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({
      left: tray.offsetWidth,
      behavior: smooth && !still ? "smooth" : "instant",
    });
  }
  // Start closed, before the first paint after hydration. Until then the trays
  // are not drawn, so the server's markup shows the row alone. Close again
  // whenever the row or its tray changes size (a folded section opening, a
  // status change that swaps the tray), unless a finger is on it.
  useLayoutEffect(() => {
    const el = scroller.current;
    const tray = leftTray.current;
    if (!el || !actionable) return;
    el.dataset.ready = "true";
    close(false);
    if (typeof ResizeObserver === "undefined") return;
    const watcher = new ResizeObserver(() => {
      if (!touching.current) close(false);
    });
    watcher.observe(el);
    if (tray) watcher.observe(tray);
    return () => watcher.disconnect();
  }, [actionable]);
  useEffect(
    () => () => {
      if (settle.current) clearTimeout(settle.current);
    },
    [],
  );
  function run(move: Move) {
    if (!submit) return;
    const target = latest.current.row;
    setResult(null);
    setLeftFrom(version);
    close(false);
    startTransition(async () => {
      const outcome = await setStatus(submit, target, move.to);
      setResult(outcome);
      // A success refreshes the screen, which drops or updates the row;
      // anything else brings it back with the reason.
      if (outcome.state !== "ok") setLeftFrom(null);
    });
  }
  function snooze(until: Date) {
    if (!submit) return;
    const target = latest.current.row;
    setSnoozing(false);
    setResult(null);
    setLeftFrom(version);
    close(false);
    startTransition(async () => {
      const outcome = await setStatus(submit, target, "snoozed", until);
      setResult(outcome);
      if (outcome.state !== "ok") setLeftFrom(null);
    });
  }
  function checkEnds() {
    const el = scroller.current;
    const from = swipeFrom.current;
    const now = latest.current;
    if (!el || touching.current || from === null || now.pending) return;
    // One check per swipe.
    swipeFrom.current = null;
    if (getComputedStyle(el).overflowX === "visible") return;
    const end = el.scrollWidth - el.clientWidth;
    if (end <= 0) return;
    if (el.scrollLeft <= 2 && from > 2 && now.left) run(now.left);
    else if (el.scrollLeft >= end - 2 && from < end - 2) run(now.right);
  }
  function scheduleCheck() {
    if (settle.current) clearTimeout(settle.current);
    if (swipeFrom.current === null) return;
    settle.current = setTimeout(checkEnds, SETTLE_MS);
  }

  const body = (
    <div className={styles.deskBody}>
      <span className={styles.stripe} aria-hidden="true" />
      <span className={`${styles.tile} ${styles[`tone_${row.areaTone}`] ?? ""}`}>
        <Icon name={row.areaIcon} />
      </span>
      <div className={styles.deskText}>
        <div className={styles.deskTop}>
          <a className={styles.deskTitle} href={row.href}>
            {row.title}
          </a>
          <span className={styles.deskPriority}>{PRIORITY_LABEL[row.priority]}</span>
        </div>
        <p className={styles.deskMeta}>
          {row.areaName ? (
            <span className={`${styles.deskArea} ${styles[`tone_${row.areaTone}`] ?? ""}`}>
              {row.areaName}
            </span>
          ) : null}
          {row.tag ? <span className={styles.deskTag}>{row.tag}</span> : null}
          {row.status === "waiting" ? (
            <span className={styles.deskFlag}>Watching</span>
          ) : null}
          {row.until ? <span className={styles.deskFlag}>{row.until}</span> : null}
          {row.status === "done" ? <span className={styles.deskFlag}>Done</span> : null}
          {row.blocked ? <span className={styles.deskFlag}>Blocked</span> : null}
          {row.signals > 1 ? (
            <span className={styles.deskSignals} title={`${row.signals} signals`}>
              <Icon name="signals" />
              {row.signals}
              <span className={styles.visuallyHidden}> signals</span>
            </span>
          ) : null}
          {/* A due date says more than an age, so it takes the age's place. */}
          {row.due ? (
            <span className={row.overdue ? styles.deskLate : styles.deskFixed}>
              {row.due}
            </span>
          ) : (
            <span className={styles.deskFixed}>{row.age}</span>
          )}
        </p>
      </div>
    </div>
  );

  if (!submit)
    return (
      <li className={styles.deskRow} data-priority={row.priority}>
        {body}
      </li>
    );

  const named = (text: string) => `${text}: ${row.title}`;
  return (
    <li
      className={styles.deskRow}
      data-priority={row.priority}
      data-leaving={leaving || undefined}
      aria-busy={pending}
    >
      <div
        ref={scroller}
        className={styles.swipe}
        onScroll={scheduleCheck}
        onTouchStart={(e) => {
          touching.current = true;
          swipeFrom.current = e.currentTarget.scrollLeft;
        }}
        onTouchEnd={() => {
          touching.current = false;
          scheduleCheck();
        }}
        onTouchCancel={() => {
          touching.current = false;
          swipeFrom.current = null;
        }}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) close();
        }}
      >
        {body}
        <div className={styles.trayRight}>
          {projects ? (
            <ProjectPicker
              submit={submit}
              itemRef={row.ref}
              revision={row.revision}
              subject={row.title}
              held={row.projectRef}
              projects={projects}
              reloadHref={reloadHref}
              buttonClass={`${styles.trayButton} ${styles.trayProject}`}
              buttonText="Project"
            />
          ) : null}
          <button
            type="button"
            className={`${styles.trayButton} ${styles.trayDone}`}
            aria-label={named(right.label)}
            title={right.label}
            disabled={pending}
            onClick={() => run(right)}
          >
            <Icon name={right.icon} />
            <span aria-hidden="true">{right.label}</span>
          </button>
          <span className={`${styles.trayReach} ${styles.trayDone}`} aria-hidden="true" />
        </div>
        <div ref={leftTray} className={styles.trayLeft}>
          {left ? (
            <>
              <span className={`${styles.trayReach} ${styles.trayWatch}`} aria-hidden="true" />
              <button
                type="button"
                className={`${styles.trayButton} ${styles.trayWatch}`}
                aria-label={named(left.label)}
                title={left.label}
                disabled={pending}
                onClick={() => run(left)}
              >
                <Icon name={left.icon} />
                <span aria-hidden="true">{left.label}</span>
              </button>
            </>
          ) : null}
          {row.status !== "done" ? (
            <button
              type="button"
              className={`${styles.trayButton} ${styles.traySnooze}`}
              aria-label={named(row.status === "snoozed" ? "Snooze again" : "Snooze")}
              aria-haspopup="dialog"
              title="Snooze"
              disabled={pending}
              onClick={() => setSnoozing(true)}
            >
              <Icon name="alarm" />
              <span aria-hidden="true">Snooze</span>
            </button>
          ) : null}
        </div>
      </div>
      {snoozing ? (
        <SnoozeDialog
          subject={row.title}
          zone={zone}
          onPick={snooze}
          onClose={() => {
            setSnoozing(false);
            close();
          }}
        />
      ) : null}
      <div aria-live="polite" aria-atomic="true" className={styles.deskNote}>
        {result?.state === "refused" ? (
          <p className={styles.error} role="alert">
            {refusalText(result.code, result.text)}
            {result.code === "record_stale" && reloadHref ? (
              <>
                {" "}
                <a href={reloadHref}>Reload</a>
              </>
            ) : null}
          </p>
        ) : null}
        {result?.state === "unavailable" ? (
          <p className={styles.error} role="alert">
            We could not confirm the change. Reload to see the item&apos;s status.
          </p>
        ) : null}
      </div>
    </li>
  );
}

/**
 * The snooze quick picks as a native modal: a sheet along the bottom of a phone,
 * a small dialog elsewhere. The picks are worked out when it opens, on the
 * viewer's clock in the shell's zone.
 */
export function SnoozeDialog({
  subject,
  zone,
  onPick,
  onClose,
}: {
  subject: string;
  zone?: string;
  onPick: (until: Date) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const id = useId();
  const [openedAt] = useState(() => new Date());
  const [custom, setCustom] = useState("");
  const [customError, setCustomError] = useState(false);
  useEffect(() => {
    const box = dialog.current;
    if (box && !box.open) box.showModal();
    box?.querySelector<HTMLElement>("[data-pick]")?.focus();
  }, []);
  function pick(choice: SnoozePick) {
    const until = snoozeAt(choice, new Date(), zone);
    if (until) onPick(until);
  }
  function pickCustom() {
    const until = customSnooze(custom, new Date(), zone);
    if (until) onPick(until);
    else setCustomError(true);
  }
  return (
    <dialog
      ref={dialog}
      className={`${styles.modal} ${styles.sheet}`}
      aria-labelledby={`${id}-title`}
      onClose={onClose}
      onClick={(e) => {
        // A tap on the backdrop (the dialog box itself, outside its content).
        if (e.target === e.currentTarget) dialog.current?.close();
      }}
    >
      <div>
        <div className={styles.modalHead}>
          <h2 id={`${id}-title`} className={styles.modalTitle}>
            <Icon name="alarm" className={styles.sheetIcon} /> Snooze until
          </h2>
          <button
            type="button"
            className={styles.modalClose}
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            <Icon name="close" />
          </button>
        </div>
        <p className={styles.modalSubject}>{subject}</p>
        <ul className={styles.choices}>
          {SNOOZE_PICKS.map((p) => {
            const when = snoozeAt(p.id, openedAt, zone);
            if (!when) return null;
            return (
              <li key={p.id}>
                <button
                  type="button"
                  className={styles.choice}
                  data-pick={p.id}
                  onClick={() => pick(p.id)}
                >
                  <span>{p.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
        <div className={styles.customPick}>
          <div className={styles.field}>
            <label htmlFor={`${id}-custom`}>Or pick a date and time</label>
            <input
              id={`${id}-custom`}
              type="datetime-local"
              value={custom}
              onChange={(e) => {
                setCustom(e.target.value);
                setCustomError(false);
              }}
              aria-invalid={customError}
              aria-describedby={customError ? `${id}-custom-error` : undefined}
            />
          </div>
          <button
            type="button"
            className={styles.secondary}
            disabled={!custom}
            onClick={pickCustom}
          >
            Snooze until then
          </button>
        </div>
        {customError ? (
          <p id={`${id}-custom-error`} className={styles.error} role="alert">
            Choose a date and time in the future.
          </p>
        ) : null}
      </div>
    </dialog>
  );
}

/**
 * Items whose snooze has passed, in a section of their own that folds away.
 * Wake all moves each back to open, one call per item: there is no batch
 * operation, and each still checks its own revision.
 */
export function WokeSection({
  items,
  submit,
  children,
}: {
  items: readonly { ref: string; revision: number }[];
  submit?: Submit;
  children: ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(true);
  const [failed, setFailed] = useState(0);
  const [pending, startTransition] = useTransition();
  function wakeAll() {
    if (!submit) return;
    setFailed(0);
    startTransition(async () => {
      let misses = 0;
      for (const item of items) {
        const outcome = await setStatus(submit, item, "open");
        if (outcome.state !== "ok") misses += 1;
      }
      setFailed(misses);
    });
  }
  return (
    <section className={styles.woke} aria-labelledby={`${id}-head`}>
      <div className={styles.wokeHead}>
        <h2 id={`${id}-head`} className={styles.wokeTitle}>
          <button
            type="button"
            className={styles.wokeToggle}
            aria-expanded={open}
            aria-controls={`${id}-rows`}
            onClick={() => setOpen((o) => !o)}
          >
            <Icon name="chevron" className={styles.wokeChevron} />
            <Icon name="alarm" />
            Woke up ({items.length})
          </button>
        </h2>
        {submit ? (
          <button
            type="button"
            className={styles.wokeAll}
            onClick={wakeAll}
            disabled={pending}
            aria-busy={pending}
          >
            <Icon name="sunrise" />
            {pending ? "Waking…" : "Wake all"}
          </button>
        ) : null}
      </div>
      <div aria-live="polite" aria-atomic="true">
        {failed ? (
          <p className={`${styles.error} ${styles.wokeError}`} role="alert">
            {failed === 1 ? "One item" : `${failed} items`} could not be woken.
            Reload to see where they stand.
          </p>
        ) : null}
      </div>
      <div id={`${id}-rows`} hidden={!open}>
        {children}
      </div>
    </section>
  );
}

export interface ChipOption {
  label: string;
  href: string;
  current: boolean;
  tone?: AreaTone;
  priority?: Priority;
}
/**
 * A filter chip that opens a short list of links. A native disclosure, so it
 * works before the page's scripts load; once they have, a tap outside (on a
 * backdrop drawn behind the open list) or Escape folds it.
 */
export function ChipMenu({
  label,
  value,
  icon,
  options,
}: {
  label: string;
  /** The chosen value's label, shown on the chip, or null for the default. */
  value: string | null;
  icon: ActionIcon;
  options: readonly ChipOption[];
}) {
  const box = useRef<HTMLDetailsElement | null>(null);
  function fold(refocus: boolean) {
    const el = box.current;
    if (!el?.open) return;
    el.open = false;
    if (refocus) el.querySelector("summary")?.focus();
  }
  return (
    <details
      ref={box}
      className={styles.chipMenu}
      onKeyDown={(e) => {
        if (e.key === "Escape") fold(true);
      }}
    >
      <summary
        className={styles.chip}
        data-active={value !== null || undefined}
        aria-label={value ? `${label}: ${value}` : label}
      >
        <Icon name={icon} />
        <span aria-hidden="true">{value ?? label}</span>
        <Icon name="chevron" className={styles.chipChevron} />
      </summary>
      <span
        className={styles.chipBackdrop}
        aria-hidden="true"
        onClick={() => fold(false)}
      />
      <ul className={styles.chipList}>
        {options.map((o) => (
          <li key={o.href}>
            <a
              href={o.href}
              className={styles.chipOption}
              aria-current={o.current ? "true" : undefined}
            >
              {o.tone ? (
                <span
                  className={`${styles.swatch} ${styles[`tone_${o.tone}`] ?? ""}`}
                  aria-hidden="true"
                />
              ) : o.priority ? (
                <span
                  className={styles.swatchSquare}
                  data-priority={o.priority}
                  aria-hidden="true"
                />
              ) : null}
              {o.label}
              {o.current ? <Icon name="check" className={styles.chipCheck} /> : null}
            </a>
          </li>
        ))}
      </ul>
    </details>
  );
}
