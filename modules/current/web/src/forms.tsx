"use client";
import {
  useEffect,
  useId,
  useRef,
  useState,
  useTransition,
  type FormEvent,
  type ReactNode,
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

/** A project the picker offers. */
export interface PickableProject {
  ref: string;
  title: string;
  status: string;
}

/**
 * The projects an item can move to: live ones, paused marked as such, and a
 * done one only when the item already sits in it.
 */
export function pickableProjects<T extends PickableProject>(
  projects: readonly T[],
  held: string | null,
): T[] {
  return projects.filter((p) => p.status !== "done" || p.ref === held);
}

/** The update a pick makes, or null when it names the project already held. */
export function projectInput(
  itemRef: string,
  revision: number,
  held: string | null,
  picked: string | null,
): Record<string, unknown> | null {
  if (picked === held) return null;
  return picked === null
    ? { ref: itemRef, revision, clear: ["project_ref"] }
    : { ref: itemRef, revision, project_ref: picked };
}

function FolderIcon({ plus }: { plus: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
      {plus ? <path d="M12 10v6M9 13h6" /> : null}
    </svg>
  );
}

/**
 * The project control for one item: an icon that opens a modal list of
 * projects. Picking one saves and closes; "No project" takes the item out.
 * A native modal dialog, like the Leads edit dialogs: focus moves in, Escape
 * closes, the page behind is inert.
 */
export function ProjectPicker({
  submit,
  itemRef,
  revision,
  subject,
  held,
  projects,
  reloadHref,
}: {
  submit: Submit;
  itemRef: string;
  revision: number;
  /** The item's title, for the button's accessible name in a list. */
  subject: string;
  /** The project the item is in now. */
  held: string | null;
  projects: readonly PickableProject[];
  reloadHref?: string;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const heading = useId();
  const [result, setResult] = useState<OperationOutcome | null>(null);
  // The list renders only while open: a queue has a picker on every row.
  const [isOpen, setIsOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  // The opening a save belongs to, so a slow save cannot touch a later one.
  const opening = useRef(0);
  const offered = pickableProjects(projects, held);
  const verb = held ? "Change project" : "Add to project";
  // Once the list is in, straight to it, on the project held if any, not Close.
  // By class, not "li button": a selector matches against the whole page, and a
  // row's dialog sits inside the row's <li>, so Close would match first.
  useEffect(() => {
    if (!isOpen) return;
    const box = dialog.current;
    (
      box?.querySelector<HTMLElement>('[aria-current="true"]') ??
      box?.querySelector<HTMLElement>(`.${styles.choice}`)
    )?.focus();
  }, [isOpen]);
  function open() {
    opening.current += 1;
    setResult(null);
    setIsOpen(true);
    if (!dialog.current?.open) dialog.current?.showModal();
  }
  function pick(picked: string | null) {
    const input = projectInput(itemRef, revision, held, picked);
    if (input === null) {
      dialog.current?.close();
      return;
    }
    const mine = opening.current;
    startTransition(async () => {
      let outcome: OperationOutcome;
      try {
        outcome = await submit("current.item.update", input);
      } catch {
        outcome = { state: "unavailable" };
      }
      if (mine !== opening.current) return;
      setResult(outcome);
      if (outcome.state === "ok") dialog.current?.close();
    });
  }
  return (
    <>
      <button
        type="button"
        className={styles.iconButton}
        aria-label={`${verb}: ${subject}`}
        title={verb}
        aria-haspopup="dialog"
        onClick={open}
      >
        <FolderIcon plus={!held} />
      </button>
      <dialog
        ref={dialog}
        className={styles.modal}
        aria-labelledby={heading}
        onClose={() => setIsOpen(false)}
        // Escape while a save is in flight would hide its outcome.
        onCancel={(e) => {
          if (pending) e.preventDefault();
        }}
      >
        <div className={styles.modalHead}>
          <h2 id={heading} className={styles.modalTitle}>
            {verb}
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
        {!isOpen ? null : offered.length || held ? (
          <ul className={styles.choices} aria-busy={pending}>
            {offered.map((p) => (
              <li key={p.ref}>
                <button
                  type="button"
                  className={styles.choice}
                  aria-current={p.ref === held ? "true" : undefined}
                  disabled={pending}
                  onClick={() => pick(p.ref)}
                >
                  <span>{p.title}</span>
                  {p.ref === held ? (
                    <span className={styles.choiceNote}>Now in</span>
                  ) : p.status !== "active" ? (
                    <span className={styles.choiceNote}>
                      {p.status === "paused" ? "Paused" : "Done"}
                    </span>
                  ) : null}
                </button>
              </li>
            ))}
            {held ? (
              <li>
                <button
                  type="button"
                  className={`${styles.choice} ${styles.choiceNone}`}
                  disabled={pending}
                  onClick={() => pick(null)}
                >
                  No project
                </button>
              </li>
            ) : null}
          </ul>
        ) : (
          <p className={styles.meta}>No projects yet. Create one under Projects.</p>
        )}
        <div aria-live="polite" aria-atomic="true">
          {pending ? <p className={styles.meta}>Saving…</p> : null}
          {result?.state === "refused" ? (
            <p className={styles.error} role="alert">
              {result.code === "record_stale"
                ? "This item changed since the page loaded. Reload, then pick again."
                : refusalText(result.code, result.text)}
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
              We could not confirm the change. Reload to check the item.
            </p>
          ) : null}
        </div>
      </dialog>
    </>
  );
}

/** One button that submits a fixed input, for small reversible actions. */
export function ActionButton({
  submit,
  operation,
  input,
  label,
  description,
}: {
  submit: Submit;
  operation: string;
  input: Record<string, unknown>;
  label: string;
  /** The accessible name when the visible label alone is ambiguous in a list. */
  description?: string;
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
        className={styles.quiet}
        onClick={run}
        disabled={pending}
        aria-busy={pending}
        aria-label={description}
      >
        {pending ? "Saving…" : label}
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
  extra,
}: {
  submit: Submit;
  itemRef: string;
  revision: number;
  status: Status;
  /** One more control at the end of the button row (the row's project icon). */
  extra?: ReactNode;
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
        {extra}
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
