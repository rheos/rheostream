import type { ReactNode } from "react";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { ActionForm } from "./action-form";
import { HandoffRequestForm } from "./handoff-request-form";
import { date, items, label, moment, row, str, type Row } from "./data";
import styles from "./leads.module.css";

export function QualificationHandoffEntry({
  shell,
  opportunityRef,
  revision,
  title,
  stale,
}: {
  shell: ShellApi;
  opportunityRef: string;
  revision: unknown;
  title: string;
  stale: boolean;
}) {
  if (!shell.submit) {
    return <p className={styles.meta}>Handoff is unavailable in this view.</p>;
  }
  return (
    <div className={styles.handoffEntry}>
      {stale ? (
        <div className={styles.handoffWarning}>
          <h3>Qualification may be out of date</h3>
          <p>
            Evidence changed after this qualification. Review or reassess it
            before handing off if the changes matter.
          </p>
        </div>
      ) : null}
      <p className={styles.handoffHelp}>
        This requests one standalone Current item and requires separate human
        approval. It creates no project and does not change this opportunity&apos;s
        stage or outcome.
      </p>
      <HandoffRequestForm
        submit={shell.submit}
        opportunityRef={opportunityRef}
        revision={revision}
        title={title}
      />
    </div>
  );
}

function stateSummary(record: Row): {
  heading: string;
  body?: string;
} {
  const state = str(record.state);
  const detail = str(record.result_detail);
  if (state === "pending") {
    const phase = str(record.action_phase);
    if (phase === "awaiting_approval")
      return {
        heading: "Awaiting approval",
        body: "No Current item has been created by this request yet.",
      };
    if (phase === "approved_queued")
      return {
        heading: "Approved and queued",
        body: "Creation is not yet confirmed.",
      };
    if (phase === "executing") return { heading: "Handoff in progress" };
    return {
      heading: "Handoff status unavailable",
      body:
        "Rheo cannot confirm whether approval or creation is in progress. Reload to check again.",
    };
  }
  if (state === "succeeded") return { heading: "Current item created" };
  if (state === "failed")
    return {
      heading: "Handoff failed",
      body: "No Current item was created by this attempt.",
    };
  if (state === "refused")
    return {
      heading: "Handoff refused",
      body:
        detail === "destination_unavailable_no_effect"
          ? "No Current item was created by this attempt."
          : undefined,
    };
  if (state === "cancelled")
    return { heading: "Handoff cancelled" };
  if (state === "unresolved")
    return {
      heading: "Outcome unknown",
      body:
        "Rheo cannot confirm whether the Current effect occurred. A person must reconcile this outcome before any retry.",
    };
  return {
    heading: "Current unavailable",
    body: "Current was unavailable. No Current effect is claimed.",
  };
}

function canonicalCurrentRef(value: unknown, recordType: "item" | "project") {
  const reference = str(value);
  return new RegExp(
    `^current\\.${recordType}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
    "i",
  ).test(reference)
    ? reference
    : null;
}

function liveLink(
  shell: ShellApi,
  reference: string | null,
  text: string,
): ReactNode {
  const href = reference && shell.recordHref ? shell.recordHref(reference) : null;
  return href ? (
    <a href={href}>{text}</a>
  ) : (
    <span className={styles.meta}>Open in Current unavailable.</span>
  );
}

function ItemStatus({ shell, value }: { shell: ShellApi; value: Row }) {
  const state = str(value.state);
  if (state === "live") {
    const content = row(value.content);
    const reference = canonicalCurrentRef(value.ref, "item");
    return (
      <>
        <h4>{str(content.title) || "Current item"}</h4>
        {str(content.body) ? (
          <p className={styles.note}>{str(content.body)}</p>
        ) : null}
        <dl className={styles.handoffFacts}>
          <div>
            <dt>Status</dt>
            <dd>{label(content.status)}</dd>
          </div>
          <div>
            <dt>Priority</dt>
            <dd>{label(content.priority)}</dd>
          </div>
          <div>
            <dt>Area</dt>
            <dd>{str(content.area) || "Not set"}</dd>
          </div>
          <div>
            <dt>Due date</dt>
            <dd>{content.due_on ? date(content.due_on) : "Not set"}</dd>
          </div>
        </dl>
        <p>{liveLink(shell, reference, "Open in Current")}</p>
      </>
    );
  }
  if (state === "deleted") return <p>Current item deleted</p>;
  if (state === "denied")
    return <p>You do not have access to this Current item.</p>;
  return <p>Current item is temporarily unavailable.</p>;
}

function ProjectStatus({ shell, value }: { shell: ShellApi; value: Row }) {
  const state = str(value.state);
  if (state === "live") {
    const content = row(value.content);
    const reference = canonicalCurrentRef(value.ref, "project");
    return (
      <>
        <h4>{str(content.title) || "Current project"}</h4>
        {str(content.description) ? (
          <p className={styles.note}>{str(content.description)}</p>
        ) : null}
        <dl className={styles.handoffFacts}>
          <div>
            <dt>Status</dt>
            <dd>{label(content.status)}</dd>
          </div>
          <div>
            <dt>Area</dt>
            <dd>{str(content.area) || "Not set"}</dd>
          </div>
          <div>
            <dt>Due date</dt>
            <dd>{content.due_on ? date(content.due_on) : "Not set"}</dd>
          </div>
        </dl>
        <p>{liveLink(shell, reference, "Open project in Current")}</p>
      </>
    );
  }
  if (state === "never_assigned")
    return (
      <>
        <p>No Current project assigned yet.</p>
        <p className={styles.meta}>Assign a project from the Current item.</p>
      </>
    );
  if (state === "cleared") return <p>Project connection cleared in Current.</p>;
  if (state === "deleted") return <p>Current project deleted.</p>;
  if (state === "denied")
    return <p>You do not have access to this Current project.</p>;
  return <p>Current project is temporarily unavailable.</p>;
}

export function IndependentRecordStatus({
  shell,
  item,
  project,
}: {
  shell: ShellApi;
  item: Row;
  project: Row;
}) {
  return (
    <div className={styles.linkedRecords}>
      <section className={styles.linkedRecord} aria-label="Current item">
        <h3>Current item</h3>
        <ItemStatus shell={shell} value={item} />
      </section>
      <section
        className={`${styles.linkedRecord} ${styles.projectRecord}`}
        aria-label="Current project"
      >
        <h3>Current project</h3>
        <ProjectStatus shell={shell} value={project} />
      </section>
    </div>
  );
}

function handoffId(reference: unknown): string | null {
  const found = /^leads\.handoff:([0-9a-f-]{36})$/i.exec(str(reference));
  return found?.[1] ?? null;
}

function retryable(record: Row): boolean {
  if (record.destination_ref !== "current") return false;
  const state = str(record.state);
  const detail = str(record.result_detail);
  return (
    state === "failed" ||
    (state === "unavailable" && detail === "destination_unavailable") ||
    (state === "refused" && detail === "destination_unavailable_no_effect")
  );
}

function approvalReviewHref(shell: ShellApi, record: Row): string | null {
  if (record.state !== "pending" || record.action_phase !== "awaiting_approval")
    return null;
  const approval = str(record.approval_id);
  return approval && shell.approvalHref ? shell.approvalHref(approval) : null;
}

export function HandoffStatusSummary({
  shell,
  record,
}: {
  shell: ShellApi;
  record: Row;
}) {
  const summary = stateSummary(record);
  const reviewHref = approvalReviewHref(shell, record);
  return (
    <aside className={styles.handoffSummary} aria-label="Current handoff status">
      <p className={styles.meta}>Latest Current handoff</p>
      <div className={styles.handoffStatus}>
        <h3>{summary.heading}</h3>
        {summary.body ? <p>{summary.body}</p> : null}
      </div>
      {reviewHref ? (
        <p className={styles.handoffAction}>
          <a className={styles.primary} href={reviewHref}>
            Review approval
          </a>
        </p>
      ) : null}
    </aside>
  );
}

function CurrentProjection({
  shell,
  view,
}: {
  shell: ShellApi;
  view: OperationOutcome | undefined;
}) {
  if (view === undefined)
    return (
      <p className={styles.meta} role="status">
        Current details are still updating.
      </p>
    );
  if (view.state === "refused")
    return <p className={styles.meta}>Current handoff details unavailable.</p>;
  if (view.state === "unavailable")
    return (
      <p className={styles.meta}>
        Current handoff details are temporarily unavailable.
      </p>
    );
  const current = row(view.result);
  return (
    <IndependentRecordStatus
      shell={shell}
      item={row(current.item)}
      project={row(current.project)}
    />
  );
}

function CurrentHandoffRow({
  shell,
  record,
  view,
}: {
  shell: ShellApi;
  record: Row;
  view: OperationOutcome | undefined;
}) {
  const summary = stateSummary(record);
  const reviewHref = approvalReviewHref(shell, record);
  const reference = handoffId(record.ref);
  const allowRetry = retryable(record) && reference && shell.submit;
  return (
    <article className={styles.handoffRow}>
      <div className={styles.handoffStatus}>
        <h3>{summary.heading}</h3>
        {summary.body ? <p>{summary.body}</p> : null}
      </div>
      <dl className={styles.handoffMeta}>
        <div>
          <dt>Requested</dt>
          <dd>{moment(record.requested_at, "Not yet", shell.timeZone)}</dd>
        </div>
        <div>
          <dt>Last updated</dt>
          <dd>
            {moment(
              record.resolved_at ?? record.requested_at,
              "Not yet",
              shell.timeZone,
            )}
          </dd>
        </div>
        <div>
          <dt>Purpose</dt>
          <dd>{label(record.purpose) || "Handoff to Current"}</dd>
        </div>
      </dl>
      {record.state === "succeeded" ? (
        <CurrentProjection shell={shell} view={view} />
      ) : null}
      {reviewHref ? (
        <p className={styles.handoffAction}>
          <a className={styles.primary} href={reviewHref}>
            Review approval
          </a>
        </p>
      ) : null}
      {allowRetry ? (
        <div className={styles.handoffRetry}>
          <p>
            This retry is safe because no effect was proven. It requires new
            approval.
          </p>
          <ActionForm
            submit={shell.submit!}
            operation="leads.handoff.restage"
            base={{
              handoff_id: reference,
              attempt_count: Number(record.attempt_count) || 0,
            }}
            fields={[]}
            button="Retry handoff"
            okCopy="Retry staged for approval."
            refusalCopy={{
              handoff_not_restageable:
                "This handoff can no longer be retried. Reload to see its current state.",
              record_stale:
                "This handoff changed after the page loaded. Reload before trying again.",
            }}
          />
        </div>
      ) : null}
    </article>
  );
}

export function CurrentHandoffHistory({
  shell,
  result,
  views,
}: {
  shell: ShellApi;
  result: OperationOutcome | null;
  views: ReadonlyMap<string, OperationOutcome>;
}) {
  let content: ReactNode;
  if (result === null) {
    content = <p role="status">Loading Current handoffs…</p>;
  } else if (result.state === "refused") {
    content = <p>Current handoff history unavailable.</p>;
  } else if (result.state === "unavailable") {
    content = <p>Current handoff history is temporarily unavailable.</p>;
  } else {
    const handoffs = items(result);
    content = handoffs.length ? (
      <ol className={styles.handoffList}>
        {handoffs.map((record) => (
          <li key={str(record.ref)}>
            <CurrentHandoffRow
              shell={shell}
              record={record}
              view={views.get(str(record.ref))}
            />
          </li>
        ))}
      </ol>
    ) : (
      <p>No Current handoffs yet.</p>
    );
  }
  return (
    <section className={`${styles.section} ${styles.handoffHistory}`}>
      <h2>Current handoffs</h2>
      {content}
    </section>
  );
}
