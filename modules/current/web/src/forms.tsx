"use client";
import {
  useId,
  useState,
  useTransition,
  type FormEvent,
} from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import {
  SNOOZE_PICKS,
  STATUS_LABEL,
  allowedTargets,
  areaKey,
  customSnooze,
  refusalText,
  snoozeAt,
  str,
  type SnoozePick,
  type Status,
} from "./data";
import styles from "./current.module.css";

type Submit = NonNullable<ShellApi["submit"]>;

export interface Field {
  name: string;
  label: string;
  /** The stored value. On an update form only fields that differ from it are sent. */
  value?: string;
  required?: boolean;
  maxLength?: number;
  type?: "text" | "date" | "textarea" | "select";
  options?: { value: string; label: string }[];
  /** An optional field the operation empties through its `clear` list. */
  clearable?: boolean;
  hint?: string;
  /** Keep the label for assistive technology only, where the row already names it. */
  hideLabel?: boolean;
}

/**
 * The operation input a form's values make, or null when an update changes nothing.
 *
 * A form whose `base` carries a `revision` is an update: a field is sent only when
 * it differs from its stored `value`, and blanking a clearable field names it in
 * `clear`, because Current reads a null or absent field as "leave it". A create
 * form sends every field that is filled in and leaves the rest to the defaults.
 */
export function formInput(
  operation: string,
  base: Record<string, unknown>,
  fields: readonly Field[],
  submitted: Record<string, string>,
): Record<string, unknown> | null {
  const updating = "revision" in base;
  const input: Record<string, unknown> = { ...base };
  const clear: string[] = [];
  let changed = false;
  for (const field of fields) {
    const value = submitted[field.name] ?? "";
    const stored = field.value ?? "";
    if (updating && value === stored) continue;
    if (value.trim() === "") {
      if (!updating) continue;
      if (field.clearable) clear.push(field.name);
      else if (!field.required) input[field.name] = "";
      else continue;
      changed = true;
      continue;
    }
    input[field.name] = value;
    changed = true;
  }
  if (clear.length) input.clear = clear;
  if (operation === "current.area.create" && !input.key)
    input.key = areaKey(str(input.label));
  return updating && !changed ? null : input;
}

function createdRef(result: unknown): string {
  if (typeof result !== "object" || result === null) return "";
  const r = result as Record<string, unknown>;
  const item = r.item as Record<string, unknown> | undefined;
  return str(item?.ref) || str(r.ref);
}

export interface FormProps {
  submit: Submit;
  operation: string;
  base?: Record<string, unknown>;
  fields: Field[];
  button: string;
  /** Empty the fields after a successful save (new records, comments). */
  reset?: boolean;
  /** After a create, link to the new record: `?ref=` is appended. */
  createdHref?: string;
  createdLabel?: string;
  /** Where "reload" goes when the record changed underneath the form. */
  reloadHref?: string;
  inline?: boolean;
}
export function ActionForm({
  submit,
  operation,
  base = {},
  fields,
  button,
  reset = false,
  createdHref,
  createdLabel = "Open it",
  reloadHref,
  inline = false,
}: FormProps) {
  const id = useId();
  const [result, setResult] = useState<OperationOutcome | "unchanged" | null>(
    null,
  );
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((f) => [
        f.name,
        f.value ??
          (f.type === "select" && f.required
            ? (f.options?.[0]?.value ?? "")
            : ""),
      ]),
    ),
  );
  const [pending, startTransition] = useTransition();
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const submitted = Object.fromEntries(
      fields.map((f) => [f.name, String(form.get(f.name) ?? "")]),
    );
    const input = formInput(operation, base, fields, submitted);
    if (input === null) {
      setResult("unchanged");
      return;
    }
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit(operation, input);
      } catch {
        outcome = { state: "unavailable" };
      }
      setResult(outcome);
      if (outcome.state === "ok" && reset)
        setValues(
          Object.fromEntries(
            fields.map((f) => [
              f.name,
              f.type === "select" ? (submitted[f.name] ?? "") : "",
            ]),
          ),
        );
    });
  }
  const created =
    result !== null && result !== "unchanged" && result.state === "ok"
      ? createdRef(result.result)
      : "";
  return (
    <form
      onSubmit={save}
      // A submit that lands before hydration must not put typed text in a URL.
      method="post"
      className={inline ? styles.inlineForm : styles.form}
      aria-busy={pending}
    >
      <fieldset disabled={pending} className={styles.fieldset}>
        {fields.map((f) => {
          const fieldId = `${id}-${f.name}`;
          const hintId = f.hint ? `${fieldId}-hint` : undefined;
          const common = {
            id: fieldId,
            name: f.name,
            value: values[f.name] ?? "",
            required: f.required,
            "aria-describedby": hintId,
          };
          const change = (value: string) =>
            setValues((before) => ({ ...before, [f.name]: value }));
          return (
            <div className={styles.field} key={f.name}>
              <label
                htmlFor={fieldId}
                className={f.hideLabel ? styles.visuallyHidden : undefined}
              >
                {f.label}
                {f.required || f.type === "select" ? "" : " (optional)"}
              </label>
              {f.type === "textarea" ? (
                <textarea
                  {...common}
                  onChange={(e) => change(e.target.value)}
                  maxLength={f.maxLength ?? 20000}
                  rows={5}
                />
              ) : f.type === "select" ? (
                <select {...common} onChange={(e) => change(e.target.value)}>
                  {f.options?.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  {...common}
                  type={f.type ?? "text"}
                  onChange={(e) => change(e.target.value)}
                  maxLength={f.type === "date" ? undefined : (f.maxLength ?? 500)}
                />
              )}
              {f.hint ? (
                <p id={hintId} className={styles.hint}>
                  {f.hint}
                </p>
              ) : null}
            </div>
          );
        })}
        <button
          type="submit"
          className={inline ? styles.secondary : styles.primary}
        >
          {pending ? "Saving…" : button}
        </button>
      </fieldset>
      <div aria-live="polite" aria-atomic="true">
        {result === "unchanged" ? (
          <p className={styles.meta}>Nothing changed, so nothing was saved.</p>
        ) : result?.state === "ok" ? (
          <p className={styles.success}>
            Saved.
            {created && createdHref ? (
              <>
                {" "}
                <a
                  href={`${createdHref}?ref=${encodeURIComponent(created)}`}
                >
                  {createdLabel}
                </a>
              </>
            ) : null}
          </p>
        ) : null}
        {result !== "unchanged" && result?.state === "refused" ? (
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
        {result !== "unchanged" && result?.state === "unavailable" ? (
          <p className={styles.error} role="alert">
            We could not confirm the save. Check the record before retrying so
            nothing is saved twice.
          </p>
        ) : null}
      </div>
    </form>
  );
}

/** An active item a project page can attach, with the project it sits in now. */
export interface AttachCandidate {
  ref: string;
  title: string;
  revision: number;
  /** The title of the project the item is in now, if any: attaching moves it. */
  elsewhere: string | null;
}

/** The candidates whose title (or current project) holds every typed word. */
export function attachChoices(
  candidates: readonly AttachCandidate[],
  words: string,
): AttachCandidate[] {
  const terms = words.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [...candidates];
  return candidates.filter((c) => {
    const text = `${c.title} ${c.elsewhere ?? ""}`.toLowerCase();
    return terms.every((term) => text.includes(term));
  });
}

/** The update that puts the chosen item in the project, or null with no choice. */
export function attachInput(
  candidates: readonly AttachCandidate[],
  chosen: string,
  projectRef: string,
): Record<string, unknown> | null {
  const picked = candidates.find((c) => c.ref === chosen);
  return picked
    ? { ref: picked.ref, revision: picked.revision, project_ref: projectRef }
    : null;
}

/**
 * Attach an existing item to a project. The select opens on an empty choice, so
 * a stray click attaches nothing; the filter narrows a long list by words.
 */
export function AttachItem({
  submit,
  projectRef,
  candidates,
  reloadHref,
}: {
  submit: Submit;
  projectRef: string;
  candidates: readonly AttachCandidate[];
  reloadHref?: string;
}) {
  const id = useId();
  const [words, setWords] = useState("");
  const [chosen, setChosen] = useState("");
  const [result, setResult] = useState<OperationOutcome | null>(null);
  const [pending, startTransition] = useTransition();
  const shown = attachChoices(candidates, words);
  function filter(value: string) {
    setWords(value);
    if (!attachChoices(candidates, value).some((c) => c.ref === chosen))
      setChosen("");
  }
  function attach(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = attachInput(candidates, chosen, projectRef);
    if (input === null) return;
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit("current.item.update", input);
      } catch {
        outcome = { state: "unavailable" };
      }
      setResult(outcome);
      if (outcome.state === "ok") {
        setChosen("");
        setWords("");
      }
    });
  }
  return (
    <form
      onSubmit={attach}
      method="post"
      className={styles.attachForm}
      aria-label="Attach an existing item"
      aria-busy={pending}
    >
      <fieldset disabled={pending} className={styles.attachFields}>
        {candidates.length > 8 ? (
          <div className={styles.field}>
            <label htmlFor={`${id}-words`} className={styles.visuallyHidden}>
              Filter items by words
            </label>
            <input
              id={`${id}-words`}
              type="search"
              value={words}
              onChange={(e) => filter(e.target.value)}
              placeholder="Filter by words"
              maxLength={200}
            />
          </div>
        ) : null}
        <div className={`${styles.field} ${styles.grow}`}>
          <label htmlFor={`${id}-item`} className={styles.visuallyHidden}>
            Item to attach
          </label>
          <select
            id={`${id}-item`}
            value={chosen}
            onChange={(e) => setChosen(e.target.value)}
          >
            <option value="">
              {shown.length ? "Choose an item to attach" : "No item matches"}
            </option>
            {shown.map((c) => (
              <option key={c.ref} value={c.ref}>
                {c.elsewhere ? `${c.title} (in ${c.elsewhere})` : c.title}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className={styles.secondary} disabled={!chosen}>
          {pending ? "Saving…" : "Attach"}
        </button>
      </fieldset>
      <div aria-live="polite" aria-atomic="true">
        {result?.state === "ok" ? (
          <p className={styles.success}>Attached.</p>
        ) : null}
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
            We could not confirm the change. Reload to check the project.
          </p>
        ) : null}
      </div>
    </form>
  );
}

/** A remove mark for an icon button; the button carries the words. */
function DetachIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
    >
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

/** One button that submits a fixed input, for small reversible actions. */
export function ActionButton({
  submit,
  operation,
  input,
  label,
  description,
  icon = false,
}: {
  submit: Submit;
  operation: string;
  input: Record<string, unknown>;
  label: string;
  /** The accessible name when the visible label alone is ambiguous in a list. */
  description?: string;
  /** Show a remove mark instead of the label; the label becomes the tooltip. */
  icon?: boolean;
}) {
  const [result, setResult] = useState<OperationOutcome | null>(null);
  const [pending, startTransition] = useTransition();
  function run() {
    startTransition(async () => {
      try {
        setResult(await submit(operation, input));
      } catch {
        setResult({ state: "unavailable" });
      }
    });
  }
  return (
    <span className={styles.buttonCell}>
      <button
        type="button"
        className={icon ? `${styles.quiet} ${styles.iconButton}` : styles.quiet}
        onClick={run}
        disabled={pending}
        aria-busy={pending}
        aria-label={description ?? (icon ? label : undefined)}
        title={icon ? label : undefined}
      >
        {icon ? <DetachIcon /> : pending ? "Saving…" : label}
      </button>
      {result?.state === "refused" ? (
        <span className={styles.error} role="alert">
          {refusalText(result.code, result.text)}
        </span>
      ) : null}
      {result?.state === "unavailable" ? (
        <span className={styles.error} role="alert">
          Not confirmed. Reload to check.
        </span>
      ) : null}
    </span>
  );
}

/**
 * A moment in the viewer's time zone, which the shell learns from the browser
 * (#465). With no zone it shows UTC, labelled.
 */
export function LocalTime({ iso, zone = "UTC" }: { iso: string; zone?: string }) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.valueOf())) return null;
  const text = new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: zone,
  }).format(d);
  return <time dateTime={iso}>{zone === "UTC" ? `${text} UTC` : text}</time>;
}

function savedStatus(result: unknown): string {
  const saved =
    typeof result === "object" && result !== null
      ? str((result as Record<string, unknown>).status)
      : "";
  return saved in STATUS_LABEL
    ? `Status saved: ${STATUS_LABEL[saved as Status]}.`
    : "Status saved.";
}

const VERB: Record<Status, string> = {
  open: "Open",
  waiting: "Wait",
  snoozed: "Snooze",
  done: "Done",
};
function verb(from: Status, to: Status): string {
  if (to === "open") return from === "done" ? "Reopen" : "Back to open";
  return VERB[to];
}

/**
 * The status controls for one item: only the moves the service allows from its
 * current status, so no button leads to a refusal by construction. Snoozing opens
 * a picker whose quick picks are computed in the browser at the moment of the
 * click, in the zone times are shown in (the browser's own when none is given).
 */
export function StatusActions({
  submit,
  itemRef,
  revision,
  status,
  subject,
  compact = false,
  reloadHref,
  zone,
}: {
  submit: Submit;
  itemRef: string;
  revision: number;
  status: Status;
  /** The item's title, for accessible names on repeated row controls. */
  subject: string;
  compact?: boolean;
  reloadHref?: string;
  /** The viewer's zone from the shell (#465). */
  zone?: string;
}) {
  const id = useId();
  const [result, setResult] = useState<OperationOutcome | null>(null);
  const [pickerOpenedAt, setPickerOpenedAt] = useState<Date | null>(null);
  const [custom, setCustom] = useState("");
  const [customError, setCustomError] = useState(false);
  const [pending, startTransition] = useTransition();
  const targets = allowedTargets(status);
  // Waiting first, done last; the snooze toggle sits between them.
  const order: Status[] = ["open", "waiting", "snoozed", "done"];
  const moves = order.filter((s) => targets.includes(s));

  function move(to: Status, until?: Date) {
    setCustomError(false);
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit("current.item.set_status", {
          ref: itemRef,
          revision,
          status: to,
          snoozed_until: until ? until.toISOString() : null,
        });
      } catch {
        outcome = { state: "unavailable" };
      }
      setResult(outcome);
      if (outcome.state === "ok") {
        setPickerOpenedAt(null);
        setCustom("");
      }
    });
  }
  function pick(choice: SnoozePick) {
    const until = snoozeAt(choice, new Date(), zone);
    if (until) move("snoozed", until);
  }
  function snoozeCustom() {
    const until = customSnooze(custom, new Date(), zone);
    if (until) move("snoozed", until);
    else setCustomError(true);
  }
  const named = (text: string) => (compact ? `${text}: ${subject}` : undefined);
  const pickerId = `${id}-snooze`;
  return (
    <div
      className={compact ? styles.rowActions : styles.statusActions}
      aria-busy={pending}
    >
      <div className={styles.actionButtons}>
        {moves.map((to) =>
          to === "snoozed" ? (
            <button
              key={to}
              type="button"
              className={styles.quiet}
              aria-expanded={pickerOpenedAt !== null}
              aria-controls={pickerId}
              aria-label={named(status === "snoozed" ? "Snooze again" : "Snooze")}
              disabled={pending}
              onClick={() =>
                setPickerOpenedAt((open) => (open ? null : new Date()))
              }
            >
              {status === "snoozed" ? "Snooze again" : "Snooze"}
            </button>
          ) : (
            <button
              key={to}
              type="button"
              className={to === "done" ? styles.done : styles.quiet}
              aria-label={named(verb(status, to))}
              disabled={pending}
              onClick={() => move(to)}
            >
              {verb(status, to)}
            </button>
          ),
        )}
      </div>
      {pickerOpenedAt ? (
        <div
          className={styles.picker}
          id={pickerId}
          role="group"
          aria-label={`Snooze ${subject} until`}
        >
          <div className={styles.picks}>
            {SNOOZE_PICKS.filter(
              (p) => snoozeAt(p.id, pickerOpenedAt, zone) !== null,
            ).map((p) => (
              <button
                key={p.id}
                type="button"
                className={styles.pick}
                disabled={pending}
                onClick={() => pick(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className={styles.customPick}>
            <div className={styles.field}>
              <label htmlFor={`${id}-custom`}>Or pick a date and time</label>
              <input
                id={`${id}-custom`}
                type="datetime-local"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                aria-invalid={customError}
                aria-describedby={customError ? `${id}-custom-error` : undefined}
              />
            </div>
            <button
              type="button"
              className={styles.secondary}
              disabled={pending || !custom}
              onClick={snoozeCustom}
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
      ) : null}
      <div aria-live="polite" aria-atomic="true">
        {result?.state === "ok" && !compact ? (
          <p className={styles.success}>{savedStatus(result.result)}</p>
        ) : null}
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
    </div>
  );
}
