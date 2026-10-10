import type { ReactNode } from "react";
import type {
  OperationOutcome,
  ScreenProps,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { ActionForm, type FormProps } from "./action-form";
import { PasteCapture } from "./paste-capture";
import {
  data,
  date,
  items,
  label,
  pageOffset,
  read,
  row,
  rows,
  str,
  type Row,
} from "./data";
import styles from "./leads.module.css";

const NAV = [
  ["list", "Opportunities"],
  ["board", "Pipeline"],
  ["capture", "Capture inquiry"],
  ["connections", "Connections"],
] as const;
function Frame({
  shell,
  active,
  title,
  description,
  children,
}: {
  shell: ShellApi;
  active: string;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.surface}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.heading}>{title}</h1>
          {description ? <p className={styles.intro}>{description}</p> : null}
        </div>
        {active === "list" || active === "board" ? (
          <a className={styles.primary} href={shell.href("capture")}>
            Capture inquiry
          </a>
        ) : null}
      </header>
      <nav className={styles.nav} aria-label="Leads">
        {NAV.filter(
          ([id]) => id !== "connections" || shell.role === "owner",
        ).map(([id, text]) => (
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
function Failure({ outcome }: { outcome: OperationOutcome }) {
  return (
    <div className={styles.empty} role="alert">
      <h2>
        {outcome.state === "refused" && outcome.code === "not_found"
          ? "Record unavailable"
          : "Could not load this view"}
      </h2>
      <p>
        {outcome.state === "refused"
          ? outcome.text || "Your access may have changed."
          : "The service did not respond. Reload to try again."}
      </p>
    </div>
  );
}
function Form({
  shell,
  ...props
}: Omit<FormProps, "submit"> & { shell: ShellApi }) {
  return shell.submit ? (
    <ActionForm {...props} submit={shell.submit} />
  ) : (
    <p className={styles.meta}>Editing is unavailable in this view.</p>
  );
}
function Options({ catalog }: { catalog: Row[] }) {
  return (
    <>
      {catalog
        .filter((r) => r.kind === "pipeline")
        .map((r) => (
          <option key={str(r.id)} value={str(r.id)}>
            {str(r.name)}
          </option>
        ))}
    </>
  );
}
/** Stored phones are digits with an optional "+"; show North American ones grouped. */
export function phoneDisplay(stored: string): string {
  const north = stored.match(/^(\+1)?(\d{3})(\d{3})(\d{4})$/);
  return north
    ? `${north[1] ? "+1 " : ""}${north[2]}-${north[3]}-${north[4]}`
    : stored;
}
/** Stale only when what the assessment judged has changed. Assessments recorded
 * before evidence digests existed fall back to the revision comparison. */
export function assessmentStale(
  assessment: Row,
  record: Row,
  revision: unknown,
): boolean {
  return assessment.evidence_digest
    ? assessment.evidence_digest !== record.evidence_digest
    : assessment.input_revision !== revision;
}
function fieldLabel(target: string): string {
  const labels: Record<string, string> = {
    "person.name": "Contact",
    "person.email": "Email",
    "person.phone": "Phone",
    "organization.name": "Organization",
    "organization.domain": "Organization website",
    subject: "Subject",
    message: "Original message",
    interest: "Interest",
    source_url: "Source URL",
    locale: "Language",
  };
  return labels[target] ?? label(target);
}
function stageLabel(record: Row, catalog: Row[]): string {
  const pipeline = catalog.find((p) => p.id === record.pipeline_id);
  return (
    str(
      rows(pipeline?.stages).find((s) => s.stage_id === record.stage_id)?.label,
    ) || label(record.stage_id)
  );
}
export function value(record: Row): string {
  if (record.value_amount == null) return "Not set";
  const currency = str(record.value_currency);
  const amount = Number(str(record.value_amount));
  let shown = `${currency} ${str(record.value_amount)}`;
  try {
    if (Number.isFinite(amount))
      shown = new Intl.NumberFormat("en-CA", {
        style: "currency",
        currency,
        currencyDisplay: "code",
        minimumFractionDigits: Number.isInteger(amount) ? 0 : 2,
        maximumFractionDigits: 2,
      }).format(amount);
  } catch {
    // An unrecognized currency code keeps the stored text.
  }
  return `${shown} · ${label(record.value_basis)}`;
}
const VALUE_BASES = [
  { value: "project_fee", label: "Project fee" },
  { value: "annual_contract", label: "Annual contract" },
  { value: "hourly_rate", label: "Hourly rate" },
  { value: "referral_fee", label: "Referral fee" },
];
export async function overview({ shell, query }: ScreenProps, board: boolean) {
  const offset = pageOffset(query.offset);
  const [catalogResult, result] = await Promise.all([
    read(shell, "leads.ui.catalog"),
    read(shell, "leads.opportunity.list", {
      query: (query.q ?? "").slice(0, 512),
      pipeline_id: query.pipeline || null,
      followup_due_by: query.due || null,
      limit: 25,
      offset,
    }),
  ]);
  const catalog = items(catalogResult),
    records = items(result);
  const active = board ? "board" : "list";
  const stages = new Map<string, string>();
  for (const pipeline of catalog.filter(
    (r) =>
      r.kind === "pipeline" && (!query.pipeline || r.id === query.pipeline),
  ))
    for (const stage of rows(pipeline.stages))
      stages.set(str(stage.stage_id), str(stage.label));
  // Older pinned versions may contain stages absent from the current preset.
  for (const record of records)
    if (!stages.has(str(record.stage_id)))
      stages.set(str(record.stage_id), label(record.stage_id));
  return (
    <Frame
      shell={shell}
      active={active}
      title={board ? "Pipeline" : "Opportunities"}
      description="Work each inquiry from its original evidence to a clear outcome."
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : catalogResult.state !== "ok" ? (
        <Failure outcome={catalogResult} />
      ) : (
        <>
          <form
            action={shell.href(active)}
            method="get"
            className={styles.toolbar}
          >
            <div className={styles.field}>
              <label htmlFor="lead-query">Filter opportunities</label>
              <input
                id="lead-query"
                name="q"
                defaultValue={query.q}
                maxLength={512}
                placeholder="Title contains…"
              />
            </div>
            <div className={styles.field}>
              <label htmlFor="lead-pipeline">Pipeline</label>
              <select
                id="lead-pipeline"
                name="pipeline"
                defaultValue={query.pipeline ?? ""}
              >
                <option value="">All pipelines</option>
                <Options catalog={catalog} />
              </select>
            </div>
            <div className={styles.field}>
              <label htmlFor="lead-due">Follow-ups due on or before</label>
              <input
                id="lead-due"
                name="due"
                type="date"
                defaultValue={query.due}
              />
            </div>
            <button className={styles.secondary}>Apply filters</button>
          </form>
          {!records.length ? (
            <div className={styles.empty}>
              <h2>
                {query.q || query.pipeline || query.due
                  ? "No matching opportunities"
                  : "Your next inquiry starts here"}
              </h2>
              <p>
                {query.q || query.pipeline || query.due
                  ? "Try another title, pipeline or follow-up date."
                  : "Capture an inquiry, then open its opportunity to review the evidence and choose the next step."}
              </p>
              {!catalog.some((r) => r.kind === "pipeline") ? (
                <p>
                  {shell.role === "owner" ? (
                    <a href={shell.href("connections")}>
                      Create your first pipeline and choose capture routing.
                    </a>
                  ) : (
                    "Ask a workspace owner to create a pipeline and choose routing."
                  )}
                </p>
              ) : null}
            </div>
          ) : board ? (
            <>
              <p className={styles.meta}>
                Showing {records.length} opportunities on this page. Open one to
                change its stage.
              </p>
              <div
                className={styles.board}
                tabIndex={0}
                role="region"
                aria-label="Pipeline stages, scroll horizontally"
              >
                {[...stages].map(([id, name]) => {
                  const group = records.filter((r) => r.stage_id === id);
                  return (
                    <section className={styles.lane} key={id}>
                      <h2>
                        {name}
                        <span className={styles.meta}>{group.length}</span>
                      </h2>
                      <ul>
                        {group.map((record) => (
                          <li key={str(record.id)}>
                            <a
                              className={styles.card}
                              href={shell.href("detail", {
                                ref: str(record.ref),
                              })}
                            >
                              <strong>{str(record.title)}</strong>
                              <span className={styles.meta}>
                                {value(record)}
                              </span>
                              <p className={styles.meta}>
                                {record.followup_due_on
                                  ? `Follow up ${date(record.followup_due_on)} · ${str(record.followup_action)}`
                                  : `Updated ${date(record.updated_at)}`}
                              </p>
                            </a>
                          </li>
                        ))}
                      </ul>
                      {!group.length ? (
                        <p className={styles.meta}>
                          No opportunities on this page.
                        </p>
                      ) : null}
                    </section>
                  );
                })}
              </div>
            </>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">Opportunity</th>
                    <th scope="col">Stage</th>
                    <th scope="col">Value</th>
                    <th scope="col">Next follow-up</th>
                    <th scope="col">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => (
                    <tr key={str(record.id)}>
                      <td>
                        <a
                          href={shell.href("detail", { ref: str(record.ref) })}
                        >
                          {str(record.title)}
                        </a>
                        <div className={styles.meta}>
                          {str(
                            catalog.find((p) => p.id === record.pipeline_id)
                              ?.name,
                          )}
                        </div>
                      </td>
                      <td>
                        <span className={styles.status}>
                          {stageLabel(record, catalog)}
                        </span>
                      </td>
                      <td>{value(record)}</td>
                      <td>
                        {record.followup_due_on ? (
                          <>
                            <div>{date(record.followup_due_on)}</div>
                            <div className={styles.meta}>
                              {str(record.followup_action)}
                            </div>
                          </>
                        ) : (
                          "Not scheduled"
                        )}
                      </td>
                      <td>{date(record.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className={styles.pagination}>
            {offset ? (
              <a
                className={styles.secondary}
                href={shell.href(active, {
                  ...query,
                  offset: String(Math.max(0, offset - 25)),
                })}
              >
                Previous page
              </a>
            ) : (
              <span />
            )}
            {records.length > 0 ? (
              <span className={styles.meta}>
                Rows {offset + 1}–{offset + records.length}
              </span>
            ) : null}
            {records.length === 25 && offset < 100000 ? (
              <a
                className={styles.secondary}
                href={shell.href(active, {
                  ...query,
                  offset: String(Math.min(100000, offset + 25)),
                })}
              >
                Next page
              </a>
            ) : (
              <span />
            )}
          </div>
        </>
      )}
    </Frame>
  );
}
export async function capture({ shell }: ScreenProps) {
  const result = await read(shell, "leads.ui.catalog"),
    catalog = items(result);
  const funnels = catalog.filter((r) => r.kind === "funnel");
  return (
    <Frame
      shell={shell}
      active="capture"
      title="Capture an inquiry"
      description="Bring an inquiry from wherever it arrived. Paste the original, review the details, then capture it."
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : !funnels.length ? (
        <p>
          No active funnel is available. Ask a workspace owner to configure
          intake.
        </p>
      ) : (
        <>
          {shell.submit ? (
            <PasteCapture
              submit={shell.submit}
              funnels={funnels.map((f) => ({
                value: str(f.ref),
                label: str(f.name),
              }))}
              receiptHref={shell.href("receipt")}
            />
          ) : (
            <p>Editing is unavailable in this view.</p>
          )}
          <details className={styles.manualCapture}>
            <summary>Enter details manually instead</summary>
            <Form
              shell={shell}
              operation="leads.intake.capture"
              button="Capture inquiry"
              receiptHref={shell.href("receipt")}
              fields={[
                {
                  name: "funnel_ref",
                  label: "Funnel",
                  type: "select",
                  required: true,
                  options: funnels.map((f) => ({
                    value: str(f.ref),
                    label: str(f.name),
                  })),
                },
                { name: "subject", label: "Inquiry title", required: true },
                { name: "person.name", label: "Contact name" },
                { name: "person.email", label: "Email", type: "email" },
                { name: "organization.name", label: "Organization" },
                {
                  name: "message",
                  label: "Original message",
                  type: "textarea",
                  required: true,
                },
                { name: "interest", label: "Interest or service" },
              ]}
            />
          </details>
          <p className={styles.meta}>
            Just adding a contact? Ask your connected agent to add the person
            through Relationships. An inquiry is only needed when there is
            something to follow up.
          </p>
        </>
      )}
    </Frame>
  );
}
export async function receipt({ shell, query }: ScreenProps) {
  const result = await read(shell, "leads.intake.receipt", {
    receipt_ref: query.ref ?? "",
  });
  const record = data(result);
  const catalogResult =
    record.state === "processed" ? await read(shell, "leads.ui.catalog") : null;
  const links = Array.isArray(record.opportunity_refs)
    ? record.opportunity_refs.map(str)
    : [];
  return (
    <Frame shell={shell} active="capture" title="Inquiry processing">
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : (
        <>
          <p role="status">
            {record.state === "pending"
              ? "Your inquiry is queued. Refresh to check processing."
              : record.state === "erased"
                ? "The source inquiry has been erased."
                : record.state === "refused"
                  ? "Processing was refused. Ask a workspace owner to check connection health."
                  : "Your inquiry has been processed and its source evidence preserved."}
          </p>
          <a
            className={styles.secondary}
            href={shell.href("receipt", { ref: query.ref })}
          >
            Refresh status
          </a>
          {links.map((ref) => (
            <p key={ref}>
              <a href={shell.href("detail", { ref })}>Open opportunity</a>
            </p>
          ))}
          {record.state === "processed" &&
          !links.length &&
          catalogResult?.state === "ok" ? (
            <section className={styles.section}>
              <h2>Create an opportunity</h2>
              <p>
                This inquiry was recorded without an opportunity. Choose a
                pipeline to work it.
              </p>
              {items(catalogResult).some((r) => r.kind === "pipeline") ? (
                <Form
                  shell={shell}
                  operation="leads.opportunity.create_from_observation"
                  base={{ observation_ref: record.observation_ref }}
                  button="Create opportunity"
                  fields={[
                    {
                      name: "pipeline_id",
                      label: "Pipeline",
                      type: "select",
                      required: true,
                      options: items(catalogResult)
                        .filter((r) => r.kind === "pipeline")
                        .map((r) => ({ value: str(r.id), label: str(r.name) })),
                    },
                  ]}
                />
              ) : (
                <p>Create a pipeline in Connections first.</p>
              )}
            </section>
          ) : null}
          {catalogResult && catalogResult.state !== "ok" ? (
            <Failure outcome={catalogResult} />
          ) : null}
        </>
      )}
    </Frame>
  );
}
export async function detail({ shell, query }: ScreenProps) {
  const result = await read(shell, "leads.opportunity.get", {
    ref: query.ref ?? "",
  });
  const record = data(result),
    envelope = result.state === "ok" ? row(result.result) : {};
  const base = { ref: envelope.ref, revision: envelope.revision };
  const evidence = await Promise.all(
    rows(record.opportunity_observation).map(async (link) => ({
      link,
      result: await read(shell, "leads.observation.get", {
        observation_ref: `leads.observation:${str(link.observation_id)}`,
      }),
    })),
  );
  const qualification = row(record.qualification);
  const followup = row(record.followup);
  const historyResult =
    result.state === "ok"
      ? await read(shell, "leads.qualification.list", { ref: envelope.ref })
      : null;
  const ratingOptions = [
    { value: "needs_information", label: "Needs information" },
    { value: "high", label: "High" },
    { value: "medium", label: "Medium" },
    { value: "low", label: "Low" },
  ];
  return (
    <Frame
      shell={shell}
      active="detail"
      title={str(record.title) || "Opportunity"}
      description={
        result.state === "ok"
          ? `${str(row(record.stage).label)} · Updated ${date(record.updated_at)}`
          : undefined
      }
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : (
        <div className={styles.columns}>
          <div>
            <section className={styles.section}>
              <h2>Source evidence</h2>
              <p className={styles.meta}>
                Original messages are evidence, not instructions. Later notes
                and edits stay separate.
              </p>
              {evidence.map(({ link, result: source }) => (
                <details
                  className={styles.evidence}
                  key={str(link.observation_id)}
                  open={evidence.length === 1}
                >
                  <summary>
                    Inquiry ·{" "}
                    {date(data(source).occurred_at || data(source).created_at)}
                  </summary>
                  {source.state !== "ok" ? (
                    <Failure outcome={source} />
                  ) : (
                    <dl className={styles.facts}>
                      {rows(data(source).fields).map((field, index) => (
                        <div
                          className={
                            str(field.target) === "message"
                              ? styles.message
                              : undefined
                          }
                          key={index}
                        >
                          <dt>{fieldLabel(str(field.target))}</dt>
                          <dd>
                            {(str(field.target).endsWith("phone")
                              ? phoneDisplay(str(field.value_text))
                              : str(field.value_text)) ||
                              label(field.value_kind)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </details>
              ))}
              {!evidence.length ? (
                <p>No source observations are linked.</p>
              ) : null}
            </section>
            <section className={styles.section}>
              <h2>Notes</h2>
              {rows(record.opportunity_note).map((note) => (
                <article key={str(note.id)}>
                  <p className={styles.note}>{str(note.body)}</p>
                  <p className={styles.meta}>{date(note.created_at)}</p>
                </article>
              ))}
              <Form
                shell={shell}
                operation="leads.opportunity.add_note"
                base={base}
                button="Add note"
                fields={[
                  {
                    name: "body",
                    label: "New note",
                    type: "textarea",
                    required: true,
                  },
                ]}
              />
            </section>
            <section className={styles.section}>
              <h2>Follow-up draft</h2>
              <p className={styles.meta}>
                Drafts are saved here. Nothing is sent.
              </p>
              {rows(record.drafts).map((draft, index) => (
                // Newest first, and open, so a draft is visible right after saving.
                <details
                  className={styles.evidence}
                  key={str(draft.id)}
                  open={index === 0}
                >
                  <summary>Draft · {date(draft.created_at)}</summary>
                  <p className={styles.note}>{str(draft.body)}</p>
                </details>
              ))}
              <Form
                shell={shell}
                operation="leads.followup.draft"
                base={base}
                button="Save draft"
                fields={[
                  {
                    name: "body",
                    label: "Draft text",
                    type: "textarea",
                    required: true,
                  },
                ]}
              />
            </section>
          </div>
          <aside>
            <section className={styles.section}>
              <h2>Next follow-up</h2>
              <p className={styles.meta}>
                A dated action for you or your agent to check here. No
                notification is sent.
              </p>
              {record.followup ? (
                <>
                  <p className={styles.note}>{str(followup.action)}</p>
                  <p>Due {date(followup.due_on)}</p>
                  <div className={styles.toolbar}>
                    <Form
                      shell={shell}
                      operation="leads.followup.resolve"
                      base={{
                        ...base,
                        reminder_id: followup.id,
                        outcome: "completed",
                      }}
                      fields={[]}
                      button="Mark follow-up complete"
                    />
                    <Form
                      shell={shell}
                      operation="leads.followup.resolve"
                      base={{
                        ...base,
                        reminder_id: followup.id,
                        outcome: "cancelled",
                      }}
                      fields={[]}
                      button="Cancel follow-up"
                    />
                  </div>
                </>
              ) : (
                <p>
                  {record.disposition_outcome
                    ? "Opportunity closed."
                    : "No follow-up scheduled."}
                </p>
              )}
              {!record.disposition_outcome ? (
                <Form
                  key={str(followup.id) || "new-followup"}
                  shell={shell}
                  operation="leads.followup.schedule"
                  base={base}
                  button={
                    record.followup
                      ? "Reschedule follow-up"
                      : "Schedule follow-up"
                  }
                  fields={[
                    {
                      name: "action",
                      label: "Follow-up action",
                      required: true,
                      maxLength: 2000,
                      value: str(followup.action),
                    },
                    {
                      name: "due_on",
                      label: "Due date",
                      type: "date",
                      required: true,
                      value: str(followup.due_on),
                    },
                  ]}
                />
              ) : null}
              {rows(record.followup_history).length ? (
                <details className={styles.evidence}>
                  <summary>Recent follow-up history</summary>
                  <p className={styles.meta}>
                    Up to 20 previous reminders. Closing an opportunity cancels
                    its pending follow-up.
                  </p>
                  {rows(record.followup_history).map((reminder) => (
                    <article key={str(reminder.id)}>
                      <p className={styles.note}>{str(reminder.action)}</p>
                      <p className={styles.meta}>
                        {label(reminder.state)} · Due {date(reminder.due_on)} ·
                        Resolved {date(reminder.resolved_at)}
                      </p>
                    </article>
                  ))}
                </details>
              ) : null}
            </section>
            <section className={styles.section}>
              <h2>Next step</h2>
              {rows(record.transitions).length ? (
                <Form
                  shell={shell}
                  operation="leads.opportunity.transition"
                  base={base}
                  button="Change stage"
                  fields={[
                    {
                      name: "to_stage_id",
                      label: "Move to",
                      type: "select",
                      required: true,
                      options: rows(record.transitions).map((stage) => ({
                        value: str(stage.stage_id),
                        label: str(stage.label),
                      })),
                    },
                    {
                      name: "note",
                      label: "Reason or next action",
                      type: "textarea",
                    },
                  ]}
                />
              ) : (
                <p>
                  This opportunity has reached a final outcome:{" "}
                  {label(record.disposition_outcome) ||
                    str(row(record.stage).label)}
                  .
                </p>
              )}
            </section>
            <section className={styles.section}>
              <h2>Qualification</h2>
              {record.qualification ? (
                <>
                  <p className={styles.note}>{str(qualification.objective)}</p>
                  <dl className={styles.facts}>
                    {[
                      "fit",
                      "intent",
                      "urgency",
                      "evidence_completeness",
                      "uncertainty",
                    ].map((key) => (
                      <div key={key}>
                        <dt>{label(key)}</dt>
                        <dd>{label(qualification[key])}</dd>
                      </div>
                    ))}
                  </dl>
                  <p className={styles.note}>
                    {str(qualification.explanation)}
                  </p>
                  {qualification.model_id ? (
                    <p className={styles.meta}>
                      Model: {str(qualification.model_id)} · Prompt:{" "}
                      {str(qualification.prompt_version)}
                    </p>
                  ) : null}
                  <p className={styles.meta}>
                    Assessed at revision {str(qualification.input_revision)} ·{" "}
                    {assessmentStale(qualification, record, envelope.revision)
                      ? "Evidence changed since · Reassess after changes"
                      : "Evidence unchanged"}
                  </p>
                </>
              ) : (
                <p>
                  No assessment yet. Review the evidence, then record how well
                  this inquiry fits your objective.
                </p>
              )}
              <details className={styles.evidence}>
                <summary>Record assessment</summary>
                <p className={styles.meta}>
                  Record your judgment against the source evidence. Choose Needs
                  information wherever the evidence is missing. Saving keeps
                  prior assessments and leaves the stage unchanged.
                </p>
                <Form
                  shell={shell}
                  operation="leads.qualification.assess"
                  base={base}
                  button="Save assessment"
                  fields={[
                    {
                      name: "objective",
                      label: "Qualification objective",
                      required: true,
                      value: str(qualification.objective),
                    },
                    ...(
                      [
                        ["fit", "Fit for this objective"],
                        ["intent", "Buying intent"],
                        ["urgency", "Urgency"],
                        ["evidence_completeness", "Evidence completeness"],
                      ] as const
                    ).map(([name, label]) => ({
                      name,
                      label,
                      type: "select" as const,
                      required: true,
                      options: ratingOptions,
                    })),
                    {
                      name: "uncertainty",
                      label: "Uncertainty",
                      type: "select",
                      required: true,
                      // No silent default: the assessor chooses how sure they are.
                      value: "",
                      options: [
                        { value: "", label: "Choose…" },
                        { value: "high", label: "High" },
                        { value: "medium", label: "Medium" },
                        { value: "low", label: "Low" },
                      ],
                    },
                    {
                      name: "explanation",
                      label: "Reasoning and missing information",
                      type: "textarea",
                      required: true,
                    },
                  ]}
                />
              </details>
              <details className={styles.evidence}>
                <summary>Check evidence completeness</summary>
                <p className={styles.meta}>
                  Counts populated fields and records an assessment with fit,
                  intent and urgency left as Needs information. This does not
                  judge suitability.
                </p>
                <Form
                  shell={shell}
                  operation="leads.qualification.assess"
                  base={base}
                  button="Check completeness"
                  fields={[
                    {
                      name: "objective",
                      label: "Completeness objective",
                      required: true,
                      value: str(qualification.objective),
                    },
                  ]}
                />
              </details>
              {historyResult && historyResult.state !== "ok" ? (
                <Failure outcome={historyResult} />
              ) : historyResult && items(historyResult).length > 1 ? (
                <details className={styles.evidence}>
                  <summary>
                    Assessment history ({items(historyResult).length})
                  </summary>
                  {items(historyResult)
                    .slice(0, 20)
                    .map((assessment) => (
                      <article key={str(assessment.id)}>
                        <h3 className={styles.note}>
                          {str(assessment.objective)}
                        </h3>
                        <p className={styles.meta}>
                          {date(assessment.created_at)} · Evidence revision{" "}
                          {str(assessment.input_revision)}
                          {assessment.id === qualification.id
                            ? " · Latest assessment"
                            : ""}
                        </p>
                        <dl className={styles.facts}>
                          {[
                            "fit",
                            "intent",
                            "urgency",
                            "evidence_completeness",
                            "uncertainty",
                          ].map((key) => (
                            <div key={key}>
                              <dt>{label(key)}</dt>
                              <dd>{label(assessment[key])}</dd>
                            </div>
                          ))}
                        </dl>
                        <p className={styles.note}>
                          {str(assessment.explanation)}
                        </p>
                        {assessment.model_id ? (
                          <p className={styles.meta}>
                            Model: {str(assessment.model_id)} · Prompt:{" "}
                            {str(assessment.prompt_version)}
                          </p>
                        ) : null}
                      </article>
                    ))}
                  {items(historyResult).length > 20 ? (
                    <p>Showing the latest 20 assessments.</p>
                  ) : null}
                </details>
              ) : null}
            </section>
            <details className={styles.section}>
              <summary>Edit opportunity title</summary>
              <Form
                shell={shell}
                operation="leads.opportunity.update"
                base={base}
                button="Save title"
                fields={[
                  {
                    name: "title",
                    label: "Title",
                    required: true,
                    value: str(record.title),
                  },
                ]}
              />
            </details>
            <section className={styles.section}>
              <h2>Record details</h2>
              <dl className={styles.facts}>
                <dt>Value</dt>
                <dd>{value(record)}</dd>
                <dt>Preset version</dt>
                <dd>{str(record.preset_version)}</dd>
                <dt>Revision</dt>
                <dd>{str(envelope.revision)}</dd>
              </dl>
              <details>
                <summary>
                  {record.value_amount == null ? "Set value" : "Change value"}
                </summary>
                <Form
                  shell={shell}
                  operation="leads.opportunity.update"
                  base={base}
                  button="Save value"
                  fields={[
                    {
                      name: "value_amount",
                      label: "Estimated amount; leave blank to clear",
                      value: str(record.value_amount),
                      maxLength: 32,
                    },
                    {
                      name: "value_currency",
                      label: "Currency code",
                      value: str(record.value_currency) || "CAD",
                      maxLength: 3,
                    },
                    {
                      name: "value_basis",
                      label: "Basis",
                      type: "select",
                      value: str(record.value_basis) || "project_fee",
                      options: VALUE_BASES,
                    },
                  ]}
                />
              </details>
            </section>
          </aside>
        </div>
      )}
    </Frame>
  );
}
export async function connections({ shell }: ScreenProps) {
  if (shell.role !== "owner")
    return (
      <Frame shell={shell} active="connections" title="Connections">
        <p>Only workspace owners can configure intake.</p>
      </Frame>
    );
  const result = await read(shell, "leads.ui.catalog"),
    catalog = items(result);
  const pipelines = catalog.filter((r) => r.kind === "pipeline");
  const jobsearch = catalog.some(
    (r) => r.kind === "capability" && r.name === "jobsearch",
  );
  const connections = catalog.filter((r) => r.kind === "connection");
  const health = await Promise.all(
    connections.map((c) =>
      read(shell, "leads.connection.health", { connection_ref: c.ref }),
    ),
  );
  return (
    <Frame
      shell={shell}
      active="connections"
      title="Connections"
      description="Choose where inquiries go and check whether intake is processing."
    >
      {result.state !== "ok" ? (
        <Failure outcome={result} />
      ) : (
        <div className={styles.columns}>
          <div>
            {connections.map((connection, index) => {
              const outcome = health[index],
                status = outcome?.state === "ok" ? row(outcome.result) : {};
              const rules = rows(connection.rules);
              const simple =
                rules.length <= 1 &&
                rules.every(
                  (r) =>
                    !rows(r.conditions).length &&
                    r.enabled === true &&
                    ["record_only", "create_opportunity"].includes(
                      str(r.action),
                    ),
                );
              return (
                <section className={styles.section} key={str(connection.id)}>
                  <h2>{str(connection.name)}</h2>
                  <p>
                    <span className={styles.status}>
                      {label(connection.state)}
                    </span>{" "}
                    · {label(connection.transport)}
                  </p>
                  {outcome?.state !== "ok" ? (
                    outcome ? (
                      <Failure outcome={outcome} />
                    ) : null
                  ) : (
                    <dl className={styles.facts}>
                      <dt>Last accepted</dt>
                      <dd>{date(status.last_accepted_at)}</dd>
                      <dt>Last processed</dt>
                      <dd>{date(status.last_processed_at)}</dd>
                      <dt>Processing lag</dt>
                      <dd>{str(status.lag_seconds)} seconds</dd>
                      <dt>Unresolved failures</dt>
                      <dd>{str(status.unresolved_failures)}</dd>
                      <dt>Failed deliveries</dt>
                      <dd>{str(status.failed_delivery_count)}</dd>
                    </dl>
                  )}
                  <h3>Routing</h3>
                  {rules.length ? (
                    rules.map((rule) => (
                      <p key={str(rule.id)}>
                        {label(rule.action)}
                        {rule.pipeline_id
                          ? ` → ${str(pipelines.find((p) => p.id === rule.pipeline_id)?.name) || "Unavailable pipeline"}`
                          : ""}
                        {rows(rule.conditions).length
                          ? ` · ${rows(rule.conditions).length} conditions`
                          : ""}
                      </p>
                    ))
                  ) : (
                    <p>Record inquiries without creating opportunities.</p>
                  )}
                  {simple ? (
                    <Form
                      shell={shell}
                      operation="leads.connection.set_routing"
                      base={{
                        connection_id: connection.id,
                        expected_rule_ids: rules.map((r) => str(r.id)),
                      }}
                      button="Save routing"
                      fields={[
                        {
                          name: "pipeline_id",
                          label: "Create opportunities in",
                          type: "select",
                          value: str(rules[0]?.pipeline_id),
                          options: [
                            { value: "", label: "Record only" },
                            ...pipelines.map((p) => ({
                              value: str(p.id),
                              label: str(p.name),
                            })),
                          ],
                        },
                      ]}
                    />
                  ) : (
                    <p className={styles.meta}>
                      This connection uses advanced routing. Manage its ordered
                      rules through the configured client.
                    </p>
                  )}
                </section>
              );
            })}
          </div>
          <section className={styles.section}>
            <h2>Pipelines</h2>
            {pipelines.map((pipeline) => (
              <p key={str(pipeline.id)}>{str(pipeline.name)}</p>
            ))}
            <p className={styles.meta}>
              {jobsearch
                ? "Create a pipeline from a preset, then select it in connection routing."
                : "Create a pipeline using the inbound-services preset, then select it in connection routing."}
            </p>
            <Form
              shell={shell}
              operation="leads.pipeline.create"
              button="Create pipeline"
              fields={[
                {
                  name: "name",
                  label: "Pipeline name",
                  required: true,
                  maxLength: 512,
                },
                // The job-search preset is absent, not hidden, unless the workspace
                // turned the capability on.
                ...(jobsearch
                  ? [
                      {
                        name: "preset",
                        label: "Preset",
                        type: "select" as const,
                        required: true,
                        options: [
                          {
                            value: "inbound_services",
                            label: "Inbound services",
                          },
                          { value: "job_search", label: "Job search" },
                        ],
                      },
                    ]
                  : []),
              ]}
            />
          </section>
        </div>
      )}
    </Frame>
  );
}
