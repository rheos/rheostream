"use client";
import { useEffect, useId, useRef, useState, useTransition } from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { ITEM_ID, itemSummary, page, refusalText, type ItemSummary } from "./data";
import styles from "./current.module.css";

type Submit = NonNullable<ShellApi["submit"]>;
type Read = (operation: string, input: unknown) => Promise<OperationOutcome>;

/** Which list a pick joins: this item's blockers, or the items it blocks. */
export type LinkSide = "blocker" | "blocked";

const SIDE: Record<LinkSide, string> = {
  blocker: "Add a blocker",
  blocked: "Add an item this blocks",
};

/** How many matches the dialog lists. */
export const PICK_SHOWN = 8;
/** Wait this long after the last key before searching. */
export const PICK_DELAY_MS = 200;

/** The dependency an item and a pick make, the right way round for the side. */
export function linkInput(side: LinkSide, itemRef: string, picked: string) {
  return side === "blocker"
    ? { item_ref: itemRef, blocker_ref: picked }
    : { item_ref: picked, blocker_ref: itemRef };
}

/** The answer to one search: what was typed, and what it found or that it failed. */
type Answer = { words: string; items: ItemSummary[] } | { words: string; failed: true };

/**
 * Link another item to this one (#645): a text button that opens a modal like
 * the project picker. Typing searches item titles as you go; picking a match
 * links it and closes. Nothing is listed until something is typed, and closing
 * leaves the page as it was.
 */
export function LinkPicker({
  submit,
  read,
  itemRef,
  subject,
  side,
  linked,
  reloadHref,
}: {
  submit: Submit;
  read: Read;
  itemRef: string;
  /** This item's title, shown under the dialog's heading. */
  subject: string;
  side: LinkSide;
  /** Refs already linked either way, and this item's own. */
  linked: readonly string[];
  reloadHref?: string;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const box = useRef<HTMLInputElement | null>(null);
  const heading = useId();
  const field = useId();
  const [isOpen, setIsOpen] = useState(false);
  const [words, setWords] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [result, setResult] = useState<OperationOutcome | null>(null);
  const [pending, startTransition] = useTransition();
  // The latest search; an answer to an older one is dropped.
  const asked = useRef(0);
  const title = SIDE[side];
  // A string, so a new array with the same refs does not restart the search.
  const linkedKey = linked.join(" ");

  const typed = words.trim();
  useEffect(() => {
    // Only the timer's callback sets state; what shows for an empty box or a
    // search still on its way is worked out from `typed` while rendering.
    const mine = ++asked.current;
    if (!isOpen || !typed) return;
    const skip = new Set(linkedKey.split(" "));
    const timer = setTimeout(async () => {
      let found: Answer;
      try {
        const [list, named] = await Promise.all([
          read("current.item.list", {
            status: "active",
            query: typed,
            match: "title",
            limit: PICK_SHOWN + skip.size,
          }),
          ITEM_ID.test(typed)
            ? read("current.item.get", { ref: typed })
            : Promise.resolve(null),
        ]);
        if (list.state !== "ok" && named?.state !== "ok") throw new Error("read");
        const byId =
          named?.state === "ok"
            ? [itemSummary(named.result)].filter((i) => i.status !== "done")
            : [];
        found = {
          words: typed,
          items: [...byId, ...page(list, itemSummary).items]
            .filter((i, n, all) => all.findIndex((a) => a.ref === i.ref) === n)
            .filter((i) => !skip.has(i.ref))
            .slice(0, PICK_SHOWN),
        };
      } catch {
        found = { words: typed, failed: true };
      }
      if (mine === asked.current) setAnswer(found);
    }, PICK_DELAY_MS);
    return () => clearTimeout(timer);
  }, [isOpen, typed, read, linkedKey]);

  // The answer for what is typed now; while a newer search is on its way, the
  // last list stays up (marked busy) rather than flashing empty.
  const current = typed && answer ? answer : null;
  const searching = typed !== "" && answer?.words !== typed;
  const items = current && "items" in current ? current.items : [];

  function open() {
    setWords("");
    setAnswer(null);
    setResult(null);
    setIsOpen(true);
    if (!dialog.current?.open) dialog.current?.showModal();
    box.current?.focus();
  }
  function pick(picked: ItemSummary) {
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit("current.dependency.add", linkInput(side, itemRef, picked.ref));
      } catch {
        outcome = { state: "unavailable" };
      }
      setResult(outcome);
      if (outcome.state === "ok") dialog.current?.close();
    });
  }

  return (
    <>
      <button
        type="button"
        className={styles.linkOpen}
        aria-haspopup="dialog"
        onClick={open}
      >
        {title}
      </button>
      <dialog
        ref={dialog}
        className={`${styles.modal} ${styles.pickModal}`}
        aria-labelledby={heading}
        onClose={() => {
          setIsOpen(false);
          asked.current += 1;
        }}
        onCancel={(e) => {
          if (pending) e.preventDefault();
        }}
      >
        <div className={styles.modalHead}>
          <h2 id={heading} className={styles.modalTitle}>
            {title}
          </h2>
          <button
            type="button"
            className={styles.modalClose}
            aria-label="Close"
            disabled={pending}
            onClick={() => dialog.current?.close()}
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              aria-hidden="true"
              focusable="false"
            >
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
        <p className={styles.modalSubject}>{subject}</p>
        <label htmlFor={field} className={styles.visuallyHidden}>
          Words in the item&apos;s title, or its ID
        </label>
        <input
          ref={box}
          id={field}
          className={styles.pickBox}
          value={words}
          onChange={(e) => setWords(e.target.value)}
          maxLength={512}
          autoComplete="off"
          spellCheck={false}
          placeholder="Type part of a title or an ID"
          aria-controls={`${field}-list`}
        />
        <div className={styles.pickList} aria-live="polite" aria-busy={searching}>
          {!current ? null : "failed" in current ? (
            searching ? null : (
              <p className={styles.error}>The search did not answer. Try again.</p>
            )
          ) : items.length ? (
            <ul id={`${field}-list`} className={styles.choices}>
              {items.map((i) => (
                <li key={i.ref}>
                  <button
                    type="button"
                    className={styles.choice}
                    disabled={pending}
                    onClick={() => pick(i)}
                  >
                    <span>{i.title}</span>
                    <span className={styles.choiceNote}>{i.shortId}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : searching ? null : (
            <p className={styles.meta}>No unfinished item has that in its title.</p>
          )}
        </div>
        <div aria-live="polite" aria-atomic="true">
          {pending ? <p className={styles.meta}>Linking…</p> : null}
          {result?.state === "refused" ? (
            <p className={styles.error} role="alert">
              {refusalText(result.code, result.text)}
              {reloadHref ? (
                <>
                  {" "}
                  <a href={reloadHref}>Reload</a>
                </>
              ) : null}
            </p>
          ) : null}
          {result?.state === "unavailable" ? (
            <p className={styles.error} role="alert">
              We could not confirm the link. Reload to check the item.
            </p>
          ) : null}
        </div>
      </dialog>
    </>
  );
}
