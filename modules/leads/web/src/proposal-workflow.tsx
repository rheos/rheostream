"use client";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { row, str, type Row } from "./data";
import {
  busy,
  draftState,
  failureCopy,
  findings as readFindings,
  progressLine,
  REFRESH_MS,
  refusalCopy,
  rowsFor,
  sections as readSections,
  STAGE_IDS,
  STAGE_LABELS,
  stageFromHash,
  stageWords,
  type Findings,
  type SectionView,
  type StageId,
} from "./proposal";
import { CopyButton } from "./queue-view";
import styles from "./proposal.module.css";

type Submit = NonNullable<ShellApi["submit"]>;

export interface WorkflowProps {
  submit?: Submit;
  opportunityRef: string;
  /** The ``leads.jobproposal.get`` result. */
  proposal: Row;
  /** The posting text the job holds now. */
  posting: string;
  /** The paste-the-full-posting form, rendered by the screen. */
  paste?: ReactNode;
}

const UNREACHABLE = "Could not reach the server. Check the connection and try again.";

function isView(result: unknown): boolean {
  const r = row(result);
  return typeof r.state === "string" && Array.isArray(r.sections);
}

/** The job detail's proposal workflow: Posting, Questions and Proposal. */
export function ProposalWorkflow(props: WorkflowProps) {
  const [view, setView] = useState<Row>(props.proposal);
  const [questions, setQuestions] = useState(str(props.proposal.questions));
  const [stage, setStage] = useState<StageId>(
    draftState(props.proposal) === "idle" ? "posting" : "proposal",
  );
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});

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
  // then the draft itself appear without anyone pressing anything.
  const running = busy(view);
  useEffect(() => {
    if (!running) return;
    const timer = setTimeout(() => window.location.reload(), REFRESH_MS);
    return () => clearTimeout(timer);
  }, [running, view]);

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
              <span className={styles.tabIndex} aria-hidden="true">
                {index + 1}
              </span>
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
        aria-labelledby="stage-tab-posting"
        hidden={stage !== "posting"}
      >
        <PostingPanel posting={props.posting} paste={props.paste} />
      </div>
      <div
        className={styles.panel}
        id="stage-panel-questions"
        role="tabpanel"
        aria-labelledby="stage-tab-questions"
        hidden={stage !== "questions"}
      >
        <QuestionsPanel
          questions={questions}
          saved={str(view.questions)}
          locked={running}
          onChange={setQuestions}
          onDraft={() => select("proposal")}
        />
      </div>
      <div
        className={styles.panel}
        id="stage-panel-proposal"
        role="tabpanel"
        aria-labelledby="stage-tab-proposal"
        hidden={stage !== "proposal"}
      >
        <ProposalPanel
          submit={props.submit}
          opportunityRef={props.opportunityRef}
          view={view}
          questions={questions}
          setView={setView}
        />
      </div>
    </section>
  );
}

function PostingPanel({ posting, paste }: { posting: string; paste?: ReactNode }) {
  return (
    <>
      <div className={styles.head}>
        <h2 className={styles.label}>Verify the posting</h2>
        <p className={styles.hint}>
          Search results and feed entries often carry only a summary, so the score
          and any draft may rest on thin text. Paste the full posting (with its
          activity section, which updates the proposal count) and the job is scored
          again on the real text.
        </p>
      </div>
      {posting.trim() ? (
        <div className={styles.posting}>
          <div className={styles.postingText}>{posting}</div>
          <div className={styles.postingCopy}>
            <CopyButton text={posting} label="Copy the posting" />
          </div>
        </div>
      ) : (
        <p className={styles.empty}>No posting text yet. Paste the full posting below.</p>
      )}
      {paste ? <div className={styles.paste}>{paste}</div> : null}
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
  return (
    <>
      <div className={styles.head}>
        <h2 className={styles.label}>Screening questions</h2>
        <p className={styles.hint}>
          One per line, as the application form asks them. The draft answers each
          one after the cover letter, from your profile only. Leave this empty for a
          cover letter alone; questions the posting itself asks you to answer are
          picked up when you draft.
        </p>
      </div>
      <textarea
        className={styles.textarea}
        aria-label="Screening questions, one per line"
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
          <button type="button" className={styles.linkButton} onClick={props.onDraft}>
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
  setView: (view: Row) => void;
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
  const found = readFindings(view.findings);

  async function call(operation: string, input: Record<string, unknown>) {
    if (!submit) return null;
    setPending(operation);
    setError("");
    let outcome: OperationOutcome;
    try {
      outcome = await submit(operation, input);
    } catch {
      outcome = { state: "unavailable" };
    }
    setPending("");
    if (outcome.state === "ok") {
      if (isView(outcome.result)) setView(row(outcome.result));
      return outcome;
    }
    if (outcome.state === "refused") {
      if (outcome.code === "draft_edited") setConfirmForce(true);
      else setError(refusalCopy(outcome.code));
    } else setError(UNREACHABLE);
    return null;
  }

  async function draft(force: boolean) {
    setConfirmForce(false);
    const done = await call("leads.jobproposal.request", {
      ref: opportunityRef,
      questions: props.questions,
      force,
    });
    if (done) setEditingAll(false);
  }

  async function remove() {
    setConfirmDelete(false);
    const done = await call("leads.jobproposal.delete", { ref: opportunityRef });
    if (done) setEditingAll(false);
  }

  const working = pending !== "";
  const running = state === "queued" || state === "drafting";
  const ready = state === "ready" && parts.length > 0;
  return (
    <>
      <div className={styles.head}>
        <h2 className={styles.label}>Proposal</h2>
        <p className={styles.hint}>
          Drafts are prepared here from your proposal profile and nothing is sent.
          You submit the application yourself, on the platform.
        </p>
      </div>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          onClick={() => draft(false)}
          disabled={!submit || working || running}
        >
          {running
            ? "Drafting…"
            : pending === "leads.jobproposal.request"
              ? "Asking…"
              : state === "idle"
                ? "Draft proposal"
                : "Regenerate proposal"}
        </button>
        {ready ? (
          <>
            <button
              type="button"
              className={styles.ghost}
              onClick={() => setEditingAll((value) => !value)}
              disabled={working}
            >
              {editingAll ? "Back to sections" : "Edit the whole draft"}
            </button>
            <CopyButton text={whole} label="Copy the whole draft" />
            <button
              type="button"
              className={styles.ghostDanger}
              onClick={() => setConfirmDelete(true)}
              disabled={working}
            >
              Delete draft
            </button>
          </>
        ) : null}
      </div>
      {confirmForce ? (
        <div className={styles.confirm} role="alertdialog" aria-label="Replace edited sections">
          <p>{refusalCopy("draft_edited")} Draft again anyway?</p>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={() => draft(true)} autoFocus>
              Replace my edits
            </button>
            <button type="button" className={styles.ghost} onClick={() => setConfirmForce(false)}>
              Keep my edits
            </button>
          </div>
        </div>
      ) : null}
      {confirmDelete ? (
        <div className={styles.confirm} role="alertdialog" aria-label="Delete the draft">
          <p>Delete this draft? The screening questions stay.</p>
          <div className={styles.actions}>
            {/* Focus lands on the safe choice. */}
            <button type="button" className={styles.danger} onClick={remove}>
              Delete the draft
            </button>
            <button type="button" className={styles.ghost} onClick={() => setConfirmDelete(false)} autoFocus>
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
        <p className={styles.progress} role="status" aria-live="polite">
          {progressLine(view)}
        </p>
      ) : null}
      {state === "failed" ? (
        <p className={styles.error} role="alert">
          {failureCopy(str(view.failure_kind))} Draft again to retry.
        </p>
      ) : null}
      {state === "idle" && !running ? (
        <p className={styles.empty}>
          No draft yet. Drafting takes a few minutes; the page follows it.
        </p>
      ) : null}
      {ready && editingAll ? (
        <WholeDraft
          key={whole}
          text={whole}
          saving={pending === "leads.jobproposal.update"}
          onSave={(text) => call("leads.jobproposal.update", { ref: opportunityRef, draft: text })}
        />
      ) : null}
      {ready && !editingAll ? (
        <div className={styles.cards}>
          {parts.map((part) => (
            <AnswerCard
              key={`${part.id}:${part.body}`}
              part={part}
              disabled={!submit || working}
              onSave={(body) =>
                call("leads.proposalsection.set", {
                  ref: opportunityRef,
                  section_id: part.id,
                  body,
                })
              }
              onUseLibrary={() =>
                call("leads.proposalsection.use_library", {
                  ref: opportunityRef,
                  section_id: part.id,
                })
              }
              onSaveToLibrary={async () => {
                const done = await call("leads.answerlibrary.save", {
                  question: part.question,
                  answer: part.body,
                });
                if (!done) return false;
                // The library now matches this question.
                setView({
                  ...view,
                  sections: (Array.isArray(view.sections) ? view.sections : []).map((s) =>
                    str(row(s).id) === part.id
                      ? { ...row(s), library_match_id: str(row(done.result).id) }
                      : s,
                  ),
                });
                return true;
              }}
            />
          ))}
        </div>
      ) : null}
      {found && (state === "ready" || state === "failed") ? (
        <FindingsPanel found={found} />
      ) : null}
    </>
  );
}

function WholeDraft(props: {
  text: string;
  saving: boolean;
  onSave: (text: string) => Promise<OperationOutcome | null>;
}) {
  const [text, setText] = useState(props.text);
  const [saved, setSaved] = useState(false);
  const changed = text !== props.text;
  return (
    <div className={styles.whole}>
      <p className={styles.hint}>
        The letter, then a line with three dashes, then each question in double
        asterisks with its answer under it. Text with no dashes line replaces only the
        cover letter.
      </p>
      <textarea
        className={styles.textarea}
        aria-label="The whole draft"
        rows={rowsFor(text, 12)}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setSaved(false);
        }}
      />
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          disabled={!changed || props.saving || !text.trim()}
          onClick={async () => setSaved((await props.onSave(text)) !== null)}
        >
          {props.saving ? "Saving…" : "Save the draft"}
        </button>
        {saved && !changed ? (
          <span className={styles.note} role="status">
            Saved.
          </span>
        ) : null}
      </div>
    </div>
  );
}

function BookIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  );
}

function EditIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}

function SaveIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12h14" />
      <path d="M12 5v14" />
    </svg>
  );
}

/** One section of the draft: read, click or Enter to edit, saved on blur. */
function AnswerCard(props: {
  part: SectionView;
  disabled: boolean;
  onSave: (body: string) => Promise<OperationOutcome | null>;
  onUseLibrary: () => Promise<OperationOutcome | null>;
  onSaveToLibrary: () => Promise<boolean>;
}) {
  const { part } = props;
  const [text, setText] = useState(part.body);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");
  const answer = part.kind === "answer";

  async function commit() {
    setEditing(false);
    if (text === part.body) return;
    if (!text.trim()) {
      setNote("An answer cannot be empty, so the saved text stays.");
      setText(part.body);
      return;
    }
    const done = await props.onSave(text);
    if (!done) setText(part.body);
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
              onClick={props.onUseLibrary}
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
                if (await props.onSaveToLibrary())
                  setNote("Saved to the library for this question.");
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
          rows={rowsFor(text, 5)}
          value={text}
          disabled={props.disabled}
          autoFocus
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setText(part.body);
              setEditing(false);
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
          {text || <span className={styles.empty}>No answer yet. Click to add one.</span>}
        </div>
      )}
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
      {!found.checked ? (
        <p className={styles.warn}>
          The accuracy check did not run on this draft. Read every claim against your
          profile before you send it.
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
