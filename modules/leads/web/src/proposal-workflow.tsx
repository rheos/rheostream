"use client";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type ReactNode,
  type SetStateAction,
} from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { row, str, type Row } from "./data";
import {
  busy,
  draftBusy,
  draftState,
  failureCopy,
  findings as readFindings,
  formRate,
  milestoneLine,
  milestones as readMilestones,
  pricingOf,
  pricingState,
  pricingWaitingLine,
  progressLine,
  refreshDelay,
  refusalCopy,
  rowsFor,
  sections as readSections,
  STAGE_IDS,
  STAGE_LABELS,
  stageFromHash,
  stageWords,
  type Findings,
  type MilestoneView,
  type SectionView,
  type StageId,
} from "./proposal";
import { InfoTip } from "./info-tip";
import { CopyButton } from "./queue-view";
import styles from "./proposal.module.css";

type Submit = NonNullable<ShellApi["submit"]>;
type SetView = Dispatch<SetStateAction<Row>>;

export interface WorkflowProps {
  submit?: Submit;
  opportunityRef: string;
  /** The ``leads.jobproposal.get`` result. */
  proposal: Row;
  /** The posting text the job holds now. */
  posting: string;
  /** The paste-the-full-posting form, rendered by the screen. */
  paste?: ReactNode;
  /** Reloads the page; tests pass their own. */
  reload?: () => void;
}

const UNREACHABLE =
  "Could not reach the server. Check the connection and try again.";

/** An operation's answer, in the words a person reads: ok with the result, or
 * the message to show. */
interface Called {
  outcome: OperationOutcome | null;
  message: string;
  code: string;
}

function isView(result: unknown): boolean {
  const r = row(result);
  return typeof r.state === "string" && Array.isArray(r.sections);
}

function isField(target: EventTarget | null): boolean {
  const name = (target as { tagName?: unknown } | null)?.tagName;
  return name === "INPUT" || name === "TEXTAREA" || name === "SELECT";
}

/** Typing anywhere on the page. A reload would throw the text away, so the
 * page stops reloading itself once someone types and offers a button instead. */
function useTyped(): boolean {
  const [typed, setTyped] = useState(false);
  useEffect(() => {
    const onInput = (event: Event) => {
      if (isField(event.target)) setTyped(true);
    };
    window.addEventListener("input", onInput, true);
    return () => window.removeEventListener("input", onInput, true);
  }, []);
  return typed;
}

/** The job detail's proposal workflow: Posting, Questions and Proposal. */
export function ProposalWorkflow(props: WorkflowProps) {
  const [view, setView] = useState<Row>(props.proposal);
  const [questions, setQuestions] = useState(str(props.proposal.questions));
  const [stage, setStage] = useState<StageId>(
    draftState(props.proposal) === "idle" ? "posting" : "proposal",
  );
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  const typed = useTyped();
  const reload = props.reload;

  useEffect(() => {
    const sync = () => {
      if (window.location.hash.startsWith("#stage-"))
        setStage(stageFromHash(window.location.hash));
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  // A queued or running draft: reload until it settles, so the stage line and
  // then the draft appear on their own. Never while someone has typed on the
  // page, and only once a minute while the account holds the draft.
  const running = busy(view);
  const delay = refreshDelay(view);
  useEffect(() => {
    if (!running || typed) return;
    const timer = setTimeout(
      () => (reload ? reload() : window.location.reload()),
      delay,
    );
    return () => clearTimeout(timer);
  }, [running, typed, delay, view, reload]);

  function select(next: StageId) {
    setStage(next);
    window.history.replaceState(null, "", `#stage-${next}`);
  }

  function onKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const last = STAGE_IDS.length - 1;
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? last
          : event.key === "ArrowLeft"
            ? (index + last) % STAGE_IDS.length
            : (index + 1) % STAGE_IDS.length;
    const id = STAGE_IDS[next] ?? "posting";
    select(id);
    tabs.current[id]?.focus();
  }

  const words = stageWords(view, props.posting, questions);
  return (
    <section className={styles.workflow} aria-label="Proposal workflow">
      <div className={styles.strip} role="tablist" aria-label="Application stages">
        {STAGE_IDS.map((id, index) => {
          const selected = id === stage;
          return (
            <button
              key={id}
              ref={(node) => {
                tabs.current[id] = node;
              }}
              type="button"
              id={`stage-tab-${id}`}
              role="tab"
              aria-selected={selected}
              aria-controls={`stage-panel-${id}`}
              aria-label={`${STAGE_LABELS[id]}: ${words[id]}`}
              tabIndex={selected ? 0 : -1}
              className={`${styles.tab}${selected ? ` ${styles.tabActive}` : ""}`}
              onClick={() => select(id)}
              onKeyDown={(event) => onKey(event, index)}
            >
              <span className={styles.tabCopy}>
                <span className={styles.tabLabel}>{STAGE_LABELS[id]}</span>
                <span
                  className={`${styles.tabState}${words[id] === "Unsaved" || words[id] === "Failed" ? ` ${styles.tabStateWarn}` : ""}`}
                >
                  {words[id]}
                </span>
              </span>
            </button>
          );
        })}
      </div>

      <div
        className={styles.panel}
        id="stage-panel-posting"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby="stage-tab-posting"
        hidden={stage !== "posting"}
      >
        <PostingPanel posting={props.posting} paste={props.paste} />
      </div>
      <div
        className={styles.panel}
        id="stage-panel-questions"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby="stage-tab-questions"
        hidden={stage !== "questions"}
      >
        <QuestionsPanel
          questions={questions}
          saved={str(view.questions)}
          locked={draftBusy(view)}
          onChange={setQuestions}
          onDraft={() => select("proposal")}
        />
      </div>
      <div
        className={styles.panel}
        id="stage-panel-proposal"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby="stage-tab-proposal"
        hidden={stage !== "proposal"}
      >
        <ProposalPanel
          submit={props.submit}
          opportunityRef={props.opportunityRef}
          view={view}
          questions={questions}
          setView={setView}
          paused={running && typed}
          reload={() => (reload ? reload() : window.location.reload())}
        />
      </div>
      <div
        className={styles.panel}
        id="stage-panel-pricing"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby="stage-tab-pricing"
        hidden={stage !== "pricing"}
      >
        <PricingPanel
          submit={props.submit}
          opportunityRef={props.opportunityRef}
          view={view}
          setView={setView}
        />
      </div>
    </section>
  );
}

function PostingPanel({
  posting,
  paste,
}: {
  posting: string;
  paste?: ReactNode;
}) {
  return (
    <>
      {posting.trim() ? (
        <div className={styles.posting}>
          <div className={styles.postingText}>{posting}</div>
          <div className={styles.postingCopy}>
            <CopyButton text={posting} label="Copy the posting" />
          </div>
        </div>
      ) : (
        <p className={styles.empty}>
          No posting text yet. Paste the full posting below.
        </p>
      )}
      {paste ? (
        <details className={styles.paste}>
          <summary>Paste the full posting</summary>
          <p className={styles.hint}>
            Feed entries often carry only a summary, so the score and any draft
            may rest on thin text. Paste the whole posting, activity section
            included, and the job is scored again on the real text.
          </p>
          {paste}
        </details>
      ) : null}
    </>
  );
}

function QuestionsPanel(props: {
  questions: string;
  saved: string;
  locked: boolean;
  onChange: (text: string) => void;
  onDraft: () => void;
}) {
  const dirty = props.questions.trim() !== props.saved.trim();
  const field = useId();
  return (
    <>
      <div className={styles.fieldHead}>
        <label className={styles.fieldLabel} htmlFor={field}>
          Screening questions, one per line
        </label>
        <InfoTip label="About screening questions">
          <p>
            The draft answers each one after the cover letter, from your profile
            only. Leave this empty for a cover letter alone. Questions the posting
            itself asks are picked up when you draft.
          </p>
        </InfoTip>
      </div>
      <textarea
        id={field}
        className={styles.textarea}
        placeholder={
          "For example:\nHow many years have you worked with FastAPI?\nWhen could you start?"
        }
        rows={rowsFor(props.questions, 6)}
        value={props.questions}
        disabled={props.locked}
        onChange={(event) => props.onChange(event.target.value)}
      />
      {dirty ? (
        <p className={styles.note} role="status">
          Not saved yet: questions are saved when you draft.{" "}
          <button
            type="button"
            className={styles.linkButton}
            onClick={props.onDraft}
          >
            Go to the proposal
          </button>
        </p>
      ) : null}
    </>
  );
}

function ProposalPanel(props: {
  submit?: Submit;
  opportunityRef: string;
  view: Row;
  questions: string;
  setView: SetView;
  paused: boolean;
  reload: () => void;
}) {
  const { submit, opportunityRef, view, setView } = props;
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [confirmForce, setConfirmForce] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editingAll, setEditingAll] = useState(false);
  const state = draftState(view);
  const parts = readSections(view);
  const whole = str(view.draft);
  // The whole-draft text lives here, so leaving the editor or drafting again
  // cannot drop it unnoticed.
  const [wholeText, setWholeText] = useState(whole);
  const [wholeSaved, setWholeSaved] = useState(false);
  const wholeDirty = editingAll && wholeText !== whole;
  const found = readFindings(view.findings);

  /** Runs one operation. A refusal is worded; ``local`` keeps the message for
   * the caller instead of the panel's own error line. */
  async function call(
    operation: string,
    input: Record<string, unknown>,
    local = false,
  ): Promise<Called> {
    if (!submit) return { outcome: null, message: UNREACHABLE, code: "" };
    setPending(operation);
    if (!local) setError("");
    let outcome: OperationOutcome;
    try {
      outcome = await submit(operation, input);
    } catch {
      outcome = { state: "unavailable" };
    }
    setPending("");
    if (outcome.state === "ok") {
      if (isView(outcome.result)) setView(row(outcome.result));
      return { outcome, message: "", code: "" };
    }
    const code = outcome.state === "refused" ? outcome.code : "";
    const message = code ? refusalCopy(code) : UNREACHABLE;
    if (!local && code !== "draft_edited") setError(message);
    return { outcome: null, message, code };
  }

  async function draft(force: boolean) {
    setConfirmForce(false);
    const done = await call("leads.jobproposal.request", {
      ref: opportunityRef,
      questions: props.questions,
      force,
    });
    if (done.code === "draft_edited") setConfirmForce(true);
    if (done.outcome) setEditingAll(false);
  }

  async function remove() {
    setConfirmDelete(false);
    const done = await call("leads.jobproposal.delete", {
      ref: opportunityRef,
    });
    if (done.outcome) setEditingAll(false);
  }

  function openWhole() {
    setWholeText(whole);
    setWholeSaved(false);
    setEditingAll(true);
  }

  async function saveWhole() {
    const done = await call("leads.jobproposal.update", {
      ref: opportunityRef,
      draft: wholeText,
    });
    if (done.outcome) {
      setWholeText(str(row(done.outcome.state === "ok" ? done.outcome.result : {}).draft));
      setWholeSaved(true);
    }
  }

  const working = pending !== "";
  const running = state === "queued" || state === "drafting";
  // The stored draft shows whenever one exists and no new one is being written:
  // after a failed regenerate it is still the draft, so it stays usable.
  const hasDraft = !running && parts.length > 0;
  return (
    <>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          onClick={() => draft(false)}
          disabled={!submit || working || running || wholeDirty}
        >
          {running
            ? "Drafting…"
            : pending === "leads.jobproposal.request"
              ? "Asking…"
              : state === "idle"
                ? "Draft proposal"
                : "Regenerate proposal"}
        </button>
        {hasDraft ? (
          <>
            <button
              type="button"
              className={styles.ghost}
              onClick={() => (editingAll ? setEditingAll(false) : openWhole())}
              disabled={working || wholeDirty}
            >
              {editingAll ? "Back to sections" : "Edit the whole draft"}
            </button>
            <CopyButton text={whole} label="Copy the whole draft" />
          </>
        ) : null}
        {hasDraft || running ? (
          <button
            type="button"
            className={styles.ghostDanger}
            onClick={() => setConfirmDelete(true)}
            disabled={!submit || working}
          >
            {running ? "Stop drafting" : "Delete draft"}
          </button>
        ) : null}
        <span className={styles.push}>
          <InfoTip label="About drafts">
            <p>
              Drafts come from your proposal profile, and nothing is sent. You
              submit the application yourself, on the platform.
            </p>
            <p>
              Click any section to edit it. The book icon swaps in a saved library
              answer; the plus saves an answer to the library.
            </p>
          </InfoTip>
        </span>
      </div>
      {wholeDirty ? (
        <p className={styles.note} role="status">
          Save or undo your changes to the whole draft before drafting again or
          going back to the sections.
        </p>
      ) : null}
      {confirmForce ? (
        <div
          className={styles.confirm}
          role="alertdialog"
          aria-label="Replace edited sections"
        >
          <p>{refusalCopy("draft_edited")} Draft again anyway?</p>
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.danger}
              onClick={() => draft(true)}
            >
              Replace my edits
            </button>
            {/* Focus lands on the safe choice. */}
            <button
              type="button"
              className={styles.ghost}
              onClick={() => setConfirmForce(false)}
              autoFocus
            >
              Keep my edits
            </button>
          </div>
        </div>
      ) : null}
      {confirmDelete ? (
        <div
          className={styles.confirm}
          role="alertdialog"
          aria-label={running ? "Stop drafting" : "Delete the draft"}
        >
          <p>
            {running
              ? "Stop drafting? Any earlier draft of this job is deleted too. The screening questions stay."
              : "Delete this draft? The screening questions stay."}
          </p>
          <div className={styles.actions}>
            <button type="button" className={styles.danger} onClick={remove}>
              {running ? "Stop and delete" : "Delete the draft"}
            </button>
            <button
              type="button"
              className={styles.ghost}
              onClick={() => setConfirmDelete(false)}
              autoFocus
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      {running ? (
        <div className={styles.progress} role="status" aria-live="polite">
          <p>{progressLine(view)}</p>
          {props.paused ? (
            <p className={styles.note}>
              The page stopped following the draft while you type, so nothing you
              type is lost.{" "}
              <button
                type="button"
                className={styles.linkButton}
                onClick={props.reload}
              >
                Check progress
              </button>
            </p>
          ) : null}
        </div>
      ) : null}
      {state === "failed" ? (
        <p className={styles.error} role="alert">
          {failureCopy(str(view.failure_kind))} Draft again to retry.
          {parts.length ? " The earlier draft below is unchanged." : ""}
        </p>
      ) : null}
      {state === "idle" ? (
        <p className={styles.empty}>
          No draft yet. Drafting takes a few minutes; the page follows it.
        </p>
      ) : null}
      {hasDraft && state === "failed" ? (
        <h3 className={styles.subhead}>Earlier draft</h3>
      ) : null}
      {hasDraft && editingAll ? (
        <WholeDraft
          text={wholeText}
          saved={wholeSaved && !wholeDirty}
          dirty={wholeDirty}
          saving={pending === "leads.jobproposal.update"}
          onChange={(text) => {
            setWholeText(text);
            setWholeSaved(false);
          }}
          onUndo={() => setWholeText(whole)}
          onSave={saveWhole}
        />
      ) : null}
      {hasDraft && !editingAll ? (
        <div className={styles.cards}>
          {parts.map((part) => (
            <AnswerCard
              key={`${part.id}:${part.body}`}
              part={part}
              disabled={!submit || working}
              onSave={(body) =>
                call(
                  "leads.proposalsection.set",
                  { ref: opportunityRef, section_id: part.id, body },
                  true,
                )
              }
              onUseLibrary={() =>
                call(
                  "leads.proposalsection.use_library",
                  { ref: opportunityRef, section_id: part.id },
                  true,
                )
              }
              onSaveToLibrary={async () => {
                const done = await call(
                  "leads.answerlibrary.save",
                  { question: part.question, answer: part.body },
                  true,
                );
                if (done.outcome?.state === "ok") {
                  const entry = str(row(done.outcome.result).id);
                  // The library now matches this question; the newest view wins.
                  setView((current) => ({
                    ...current,
                    sections: (Array.isArray(current.sections)
                      ? current.sections
                      : []
                    ).map((s) =>
                      str(row(s).id) === part.id
                        ? { ...row(s), library_match_id: entry }
                        : s,
                    ),
                  }));
                }
                return done;
              }}
            />
          ))}
        </div>
      ) : null}
      {found && hasDraft ? <FindingsPanel found={found} /> : null}
    </>
  );
}

function WholeDraft(props: {
  text: string;
  saved: boolean;
  dirty: boolean;
  saving: boolean;
  onChange: (text: string) => void;
  onUndo: () => void;
  onSave: () => void;
}) {
  return (
    <div className={styles.whole}>
      <textarea
        className={styles.textarea}
        aria-label="The whole draft"
        rows={rowsFor(props.text, 12)}
        value={props.text}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          disabled={!props.dirty || props.saving || !props.text.trim()}
          onClick={props.onSave}
        >
          {props.saving ? "Saving…" : "Save the draft"}
        </button>
        {props.dirty ? (
          <button type="button" className={styles.ghost} onClick={props.onUndo}>
            Undo changes
          </button>
        ) : null}
        {props.saved ? (
          <span className={styles.note} role="status">
            Saved.
          </span>
        ) : null}
        <span className={styles.push}>
          <InfoTip label="About the whole-draft format">
            <p>
              The letter, then a line with three dashes, then each question in
              double asterisks with its answer under it. Text with no dashes line
              replaces only the cover letter.
            </p>
          </InfoTip>
        </span>
      </div>
    </div>
  );
}

function BookIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  );
}

function EditIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}

function SaveIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12h14" />
      <path d="M12 5v14" />
    </svg>
  );
}

/** One section of the draft: plain text, a click or the Edit button to edit,
 * saved on blur. A save that fails keeps the text and stays open. */
function AnswerCard(props: {
  part: SectionView;
  disabled: boolean;
  onSave: (body: string) => Promise<Called>;
  onUseLibrary: () => Promise<Called>;
  onSaveToLibrary: () => Promise<Called>;
}) {
  const { part } = props;
  const [text, setText] = useState(part.body);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState("");
  const edit = useRef<HTMLButtonElement | null>(null);
  const answer = part.kind === "answer";

  async function commit() {
    if (text === part.body) {
      setEditing(false);
      return;
    }
    if (!text.trim()) {
      setProblem("An answer cannot be empty. Type one, or press Escape to undo.");
      return;
    }
    setProblem("");
    const done = await props.onSave(text);
    if (done.outcome) setEditing(false);
    // A failed save keeps what was typed, open, with the reason beside it.
    else setProblem(`Not saved: ${done.message}`);
  }

  function cancel() {
    setText(part.body);
    setProblem("");
    setEditing(false);
    edit.current?.focus();
  }

  return (
    <article className={styles.card} aria-label={part.label}>
      <div className={styles.cardHead}>
        <h3 className={styles.cardTitle}>{part.label}</h3>
        <div className={styles.cardActions}>
          {part.fromLibrary ? (
            <span className={styles.badge}>From the library</span>
          ) : part.edited ? (
            <span className={styles.badge}>Edited</span>
          ) : null}
          {!editing ? (
            <button
              ref={edit}
              type="button"
              className={styles.iconButton}
              onClick={() => setEditing(true)}
              disabled={props.disabled}
              aria-label={`Edit: ${part.label}`}
              title="Edit"
            >
              <EditIcon />
            </button>
          ) : null}
          {answer && part.libraryMatch ? (
            <button
              type="button"
              className={styles.iconButton}
              onClick={async () => {
                const done = await props.onUseLibrary();
                setProblem(done.outcome ? "" : done.message);
              }}
              disabled={props.disabled}
              aria-label="Use the saved library answer"
              title="Use the saved library answer"
            >
              <BookIcon />
            </button>
          ) : null}
          {answer ? (
            <button
              type="button"
              className={styles.iconButton}
              onClick={async () => {
                const done = await props.onSaveToLibrary();
                if (done.outcome) {
                  setProblem("");
                  setNote("Saved to the library for this question.");
                } else setProblem(done.message);
              }}
              disabled={props.disabled || !part.body.trim()}
              aria-label="Save this answer to the library (strip anything specific to this client first)"
              title="Save this answer to the library (strip anything specific to this client first)"
            >
              <SaveIcon />
            </button>
          ) : null}
          <CopyButton text={text} label={`Copy: ${part.label}`} />
        </div>
      </div>
      {editing ? (
        <textarea
          className={styles.cardTextarea}
          aria-label={`Edit: ${part.label}`}
          aria-invalid={problem ? true : undefined}
          rows={rowsFor(text, 5)}
          value={text}
          disabled={props.disabled}
          autoFocus
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              cancel();
            }
          }}
        />
      ) : (
        // Plain text for every reader; a click edits it too, and the Edit button
        // is the keyboard's way in.
        <div
          className={styles.cardRead}
          title="Click to edit"
          onClick={() => !props.disabled && setEditing(true)}
        >
          {text || (
            <span className={styles.empty}>No answer yet. Click to add one.</span>
          )}
        </div>
      )}
      {problem ? (
        <p className={styles.error} role="alert">
          {problem}
        </p>
      ) : null}
      {note ? (
        <p className={styles.note} role="status">
          {note}
        </p>
      ) : null}
    </article>
  );
}

function FindingsPanel({ found }: { found: Findings }) {
  const argues = found.analysis;
  return (
    <section className={styles.findings} aria-label="Draft findings">
      <h3 className={styles.cardTitle}>Draft findings</h3>
      {found.clean ? <p className={styles.note}>No issues found.</p> : null}
      {found.figuresStale ? (
        <p className={styles.warn}>
          The pricing changed after this draft was written, so the letter may quote
          figures that differ from the Pricing tab. Regenerate the proposal to match.
        </p>
      ) : null}
      {!found.checked ? (
        <p className={styles.warn}>
          The accuracy check did not run on this draft. Read every claim against
          your profile before you send it.
        </p>
      ) : null}
      {found.retried || found.flagged.length ? (
        <div className={styles.group}>
          <h4 className={styles.groupTitle}>Accuracy</h4>
          {found.retried ? (
            <p className={styles.note}>
              These claims were questioned, and the draft was written again:
            </p>
          ) : null}
          {found.retried && found.questioned.length ? (
            <ul className={styles.list}>
              {found.questioned.map((claim, index) => (
                <li key={`q-${index}`}>{claim}</li>
              ))}
            </ul>
          ) : null}
          {found.flagged.length ? (
            <>
              <p className={styles.warn}>Still not backed by your profile:</p>
              <ul className={styles.list}>
                {found.flagged.map((claim, index) => (
                  <li key={`f-${index}`}>
                    <span className={styles.claim}>{claim.claim}</span>
                    {claim.kind === "attribution"
                      ? " (not in that project's entry)"
                      : claim.kind === "process"
                        ? " (an incident no anecdote backs)"
                        : null}
                    {claim.context ? (
                      <span className={styles.context}>{claim.context}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
      {found.style.length || found.repaired ? (
        <div className={styles.group}>
          <h4 className={styles.groupTitle}>Style</h4>
          {found.repaired ? (
            <p className={styles.note}>
              {found.repairedCount} style{" "}
              {found.repairedCount === 1 ? "issue was" : "issues were"} found and
              repaired.
            </p>
          ) : null}
          {found.style.length ? (
            <ul className={styles.list}>
              {found.style.map((item, index) => (
                <li key={`s-${index}`}>
                  <span className={styles.claim}>{item.rule}</span>
                  <span className={styles.context}>
                    {item.where}: {item.excerpt}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {argues.angle || argues.hiringFor ? (
        <details className={styles.argues}>
          <summary>What the draft argues</summary>
          <dl>
            {argues.hiringFor ? (
              <>
                <dt>Hiring for</dt>
                <dd>{argues.hiringFor}</dd>
              </>
            ) : null}
            {argues.angle ? (
              <>
                <dt>Angle</dt>
                <dd>{argues.angle}</dd>
              </>
            ) : null}
            {argues.mustAddress.length ? (
              <>
                <dt>Must address</dt>
                <dd>{argues.mustAddress.join("; ")}</dd>
              </>
            ) : null}
            {argues.gaps.length ? (
              <>
                <dt>Outside your profile</dt>
                <dd>{argues.gaps.join(", ")}</dd>
              </>
            ) : null}
          </dl>
        </details>
      ) : null}
    </section>
  );
}

function PricingPanel(props: {
  submit?: Submit;
  opportunityRef: string;
  view: Row;
  setView: SetView;
}) {
  const { submit, opportunityRef, view, setView } = props;
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const pricing = pricingOf(view);
  const state = pricingState(view);
  const rows = readMilestones(view);
  const rate = formRate(view);
  const suggestion = row(pricing.suggestion);
  const running = state === "queued" || state === "running";
  const available = pricing.available !== false;

  async function call(
    operation: string,
    input: Record<string, unknown>,
  ): Promise<string> {
    if (!submit) return UNREACHABLE;
    let outcome: OperationOutcome;
    try {
      outcome = await submit(operation, input);
    } catch {
      outcome = { state: "unavailable" };
    }
    if (outcome.state === "ok") {
      if (isView(outcome.result)) setView(row(outcome.result));
      return "";
    }
    return outcome.state === "refused" ? refusalCopy(outcome.code) : UNREACHABLE;
  }

  async function suggest() {
    setConfirm(false);
    setPending(true);
    setError(await call("leads.jobpricing.request", { ref: opportunityRef }));
    setPending(false);
  }

  return (
    <>
      {!available ? (
        <p className={styles.empty}>{refusalCopy("pricing_not_set")}</p>
      ) : null}
      {rate ? (
        <div className={styles.formRate}>
          <div className={styles.formRateLine}>
            <span className={styles.formRateLabel}>Form rate</span>
            <span className={styles.formRateValue}>{rate.rate}</span>
            {rate.detail ? (
              <span className={styles.note}>{rate.detail}</span>
            ) : null}
            <InfoTip label="How the form rate was worked out" align="start">
              <p>
                Milestones are priced from your day rate per honest working day,
                never from the budget. The form rate is what to type into an
                hourly apply form so it agrees with the letter.
              </p>
              {str(suggestion.basis) ? <p>{str(suggestion.basis)}</p> : null}
              {str(suggestion.note) ? <p>{str(suggestion.note)}</p> : null}
            </InfoTip>
          </div>
        </div>
      ) : null}
      {available ? (
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.primary}
            onClick={() => (rows.length ? setConfirm(true) : suggest())}
            disabled={!submit || pending || running}
          >
            {running
              ? "Suggesting…"
              : rows.length
                ? "Regenerate milestones"
                : "Suggest milestones"}
          </button>
          {rows.length ? (
            <CopyButton
              text={rows.map(milestoneLine).join("\n")}
              label="Copy every milestone"
            />
          ) : null}
        </div>
      ) : null}
      {confirm ? (
        <div className={styles.confirm} role="alertdialog" aria-label="Regenerate milestones">
          <p>Regenerate the milestones? Your edits to them are replaced.</p>
          <div className={styles.actions}>
            <button type="button" className={styles.danger} onClick={suggest}>
              Regenerate
            </button>
            <button
              type="button"
              className={styles.ghost}
              onClick={() => setConfirm(false)}
              autoFocus
            >
              Keep these
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      {running ? (
        <p className={styles.progress} role="status" aria-live="polite">
          {pricingWaitingLine(view) || "Suggesting milestones. This takes a minute or two."}
        </p>
      ) : null}
      {state === "failed" ? (
        <p className={styles.error} role="alert">
          {failureCopy(str(pricing.failure_kind))} Suggest again to retry.
        </p>
      ) : null}
      {str(pricing.warning) ? (
        <p className={styles.warn}>Note: {str(pricing.warning)}</p>
      ) : null}
      {str(pricing.too_vague) && !rows.length ? (
        <p className={styles.warn}>
          The posting is too vague to stage: {str(pricing.too_vague)} Add detail to
          the posting and suggest again.
        </p>
      ) : null}
      {rows.length ? (
        <div className={styles.cards}>
          {rows.map((m) => (
            <MilestoneCard
              key={m.id}
              milestone={m}
              disabled={!submit || running}
              onSave={(changes) =>
                call("leads.proposalmilestone.set", {
                  ref: opportunityRef,
                  milestone_id: m.id,
                  ...changes,
                })
              }
            />
          ))}
        </div>
      ) : null}
    </>
  );
}

/** One milestone: each field saves when it loses focus; a failed save keeps
 * what was typed, with the reason. */
function MilestoneCard(props: {
  milestone: MilestoneView;
  disabled: boolean;
  onSave: (changes: Record<string, string | null>) => Promise<string>;
}) {
  const m = props.milestone;
  const [title, setTitle] = useState(m.title);
  const [amount, setAmount] = useState(m.amount);
  const [dueOn, setDueOn] = useState(m.dueOn);
  const [description, setDescription] = useState(m.description);
  const [problem, setProblem] = useState("");

  async function save(field: string, value: string, saved: string) {
    if (value === saved) return;
    if (field === "title" && !value.trim()) {
      setProblem("A milestone needs a title.");
      return;
    }
    if (field === "amount" && !(Number(value) >= 0 && value.trim() !== "")) {
      setProblem("The amount must be a number of zero or more.");
      return;
    }
    setProblem("");
    // A cleared due date is sent as no date, so the server forgets it too.
    const message = await props.onSave({
      [field]: field === "due_on" && !value ? null : value,
    });
    if (message) setProblem(`Not saved: ${message}`);
  }

  return (
    <article className={styles.card} aria-label={`Milestone: ${m.title}`}>
      <div className={styles.cardHead}>
        <input
          className={styles.milestoneTitle}
          aria-label="Milestone title"
          value={title}
          disabled={props.disabled}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={() => save("title", title, m.title)}
        />
        <div className={styles.cardActions}>
          <CopyButton
            text={milestoneLine({ ...m, title, amount, dueOn })}
            label={`Copy: ${title}`}
          />
        </div>
      </div>
      <div className={styles.milestoneFields}>
        <label className={styles.milestoneField}>
          <span>Amount ($)</span>
          <input
            type="number"
            min="0"
            step="0.01"
            inputMode="decimal"
            value={amount}
            disabled={props.disabled}
            onChange={(event) => setAmount(event.target.value)}
            onBlur={() => save("amount", amount, m.amount)}
          />
        </label>
        <label className={styles.milestoneField}>
          <span>Due date</span>
          <input
            type="date"
            value={dueOn}
            disabled={props.disabled}
            onChange={(event) => setDueOn(event.target.value)}
            onBlur={() => save("due_on", dueOn, m.dueOn)}
          />
        </label>
      </div>
      <textarea
        className={styles.cardTextarea}
        aria-label="Milestone description"
        rows={rowsFor(description, 2)}
        placeholder="What this stage delivers."
        value={description}
        disabled={props.disabled}
        onChange={(event) => setDescription(event.target.value)}
        onBlur={() => save("description", description, m.description)}
      />
      {problem ? (
        <p className={styles.error} role="alert">
          {problem}
        </p>
      ) : null}
    </article>
  );
}
