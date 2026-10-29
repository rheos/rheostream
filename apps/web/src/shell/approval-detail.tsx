import type { ReactNode } from "react";

import { callOperation, requestIdentity } from "@/lib/operations";
import { approvalId } from "@/lib/routing/links";
import { approveApproval, refuseApproval } from "@/shell/approval-actions";
import {
  type ApprovalPreview,
  type ExternalAction,
  isActionablePreview,
  isApprovalPreview,
  isExternalAction,
  isHistoryPreview,
  isRefusablePreview,
} from "@/shell/approval-contract";
import { ApprovalDecisionForms } from "@/shell/approval-decision-forms";

import styles from "./chrome/approval-detail.module.css";

const EXPECTED_PAYLOAD_FIELDS = [
  "destination_ref",
  "source_ref",
  "title",
  "body",
  "priority",
  "area",
  "due_on",
  "idempotency_key",
] as const;

const PAYLOAD_LABELS: Record<string, string> = {
  destination_ref: "Destination (destination_ref)",
  source_ref: "Source (source_ref)",
  title: "Title (title)",
  body: "Body (body)",
  priority: "Priority (priority)",
  area: "Area (area)",
  due_on: "Due date (due_on)",
  idempotency_key: "Idempotency key (idempotency_key)",
};

function PlainState({
  heading,
  children,
  alert = false,
}: {
  heading: string;
  children: ReactNode;
  alert?: boolean;
}) {
  return (
    <section className={styles.plain} role={alert ? "alert" : undefined}>
      <h1>{heading}</h1>
      {children}
    </section>
  );
}

function moment(value: string, timeZone: string): ReactNode {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return (
    <time dateTime={value}>
      {new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone,
      }).format(date)} {`(${timeZone})`}
    </time>
  );
}

function valueText(value: unknown): string {
  if (value === null) {
    return "Not set";
  }
  if (typeof value === "string") {
    return value.length === 0 ? "Empty" : value;
  }
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function payloadEntries(payload: Record<string, unknown>): Array<[string, unknown]> {
  const ordered: Array<[string, unknown]> = [];
  const included = new Set<string>();
  for (const key of EXPECTED_PAYLOAD_FIELDS) {
    if (Object.hasOwn(payload, key)) {
      ordered.push([key, payload[key]]);
      included.add(key);
    }
  }
  for (const key of Object.keys(payload).sort()) {
    if (!included.has(key)) {
      ordered.push([key, payload[key]]);
    }
  }
  return ordered;
}

function Fact({ term, children, mono = false }: { term: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className={styles.fact}>
      <dt>{term}</dt>
      <dd className={mono ? styles.mono : undefined}>{children}</dd>
    </div>
  );
}

function actionStatus(action: ExternalAction | null, preview: ApprovalPreview): ReactNode {
  if (action === null) {
    if (preview.durable_state === "approved") {
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff approved</h1>
          <p>The Current action status is temporarily unavailable. No repeat decision is available.</p>
        </header>
      );
    }
    if (preview.durable_state === "refused") {
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff refused</h1>
          <p>This approval is closed. No repeat decision is available.</p>
        </header>
      );
    }
    return (
      <header className={styles.result} tabIndex={-1}>
        <h1>Approval resolved</h1>
        <p>This approval is closed and has no repeat decision.</p>
      </header>
    );
  }

  switch (action.state) {
    case "pending":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff approved and queued</h1>
          <p>Creation is not yet confirmed.</p>
        </header>
      );
    case "executing":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff in progress</h1>
          <p>Current is processing the approved request.</p>
        </header>
      );
    case "succeeded":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Current item created</h1>
          <p>The external action reports a successful Current result.</p>
        </header>
      );
    case "failed":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff failed with no effect</h1>
          <p>No Current item was created by this attempt.</p>
        </header>
      );
    case "unresolved":
      return (
        <header className={styles.warning} tabIndex={-1} role="alert">
          <h1>Outcome unknown</h1>
          <p>Rheo cannot confirm whether the Current effect occurred. Reconcile it before taking another action.</p>
        </header>
      );
    case "refused":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff refused</h1>
          <p>No Current item was created by this attempt.</p>
        </header>
      );
    case "cancelled":
      return (
        <header className={styles.result} tabIndex={-1}>
          <h1>Handoff cancelled</h1>
          <p>The action is closed. Success is not claimed.</p>
        </header>
      );
  }
}

function Snapshot({
  preview,
  timeZone,
  history,
}: {
  preview: ApprovalPreview;
  timeZone: string;
  history: boolean;
}) {
  const payload = preview.payload as Record<string, unknown>;
  const restored = preview.status === "restored";
  return (
    <>
      <section aria-labelledby="approval-destination">
        <h2 id="approval-destination" className={styles.sectionHeading}>Destination</h2>
        <dl className={styles.facts}>
          <Fact term="Destination">Current</Fact>
          <Fact term="Canonical destination" mono>{preview.destination_ref}</Fact>
          <Fact term="Operation" mono>{preview.operation_name}</Fact>
        </dl>
      </section>

      <section aria-labelledby="approval-context">
        <h2 id="approval-context" className={styles.sectionHeading}>Request context</h2>
        <dl className={styles.facts}>
          <Fact term="Purpose">{preview.purpose}</Fact>
          <Fact term="Original requester" mono>
            {preview.requester_kind}{preview.requester_id ? ` ${preview.requester_id}` : ""}
          </Fact>
          <Fact term="Held subject" mono>{preview.subject_ref}</Fact>
          <Fact term="Approval state" mono>{preview.durable_state}</Fact>
          <Fact term={restored ? "Historical source window start" : "Approval window start"}>
            {moment(preview.window_start!, timeZone)}
          </Fact>
          <Fact term={restored ? "Historical source window end" : "Approval window end"}>
            {moment(preview.window_end!, timeZone)}
          </Fact>
        </dl>
        {restored ? (
          <p className={styles.notice}>
            These dates are the historical source-workspace window. Approving reopens the execution window now.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="approval-payload">
        <h2 id="approval-payload" className={styles.sectionHeading}>Verified stored payload</h2>
        <dl className={styles.payload}>
          {payloadEntries(payload).map(([key, value]) => (
            <div className={styles.payloadField} key={key}>
              <dt>{PAYLOAD_LABELS[key] ?? key}</dt>
              <dd className={styles.payloadValue}>{valueText(value)}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section aria-labelledby="approval-binding">
        <h2 id="approval-binding" className={styles.sectionHeading}>Review binding</h2>
        <p>This digest binds this review to the exact stored request.</p>
        <p className={styles.digest}>{preview.payload_digest}</p>
        {history ? null : (
          <p className={styles.safety}>
            Approving queues this request. It does not by itself prove that a Current item was created.
          </p>
        )}
      </section>
    </>
  );
}

async function loadAction(preview: ApprovalPreview, workspaceId: string): Promise<ExternalAction | null> {
  if (preview.external_action_id === null) {
    return null;
  }
  const outcome = await callOperation(
    "core.external_action.get",
    { external_action_id: preview.external_action_id },
    await requestIdentity(),
    workspaceId,
  );
  if (outcome.state !== "ok" || !isExternalAction(outcome.result)) {
    return null;
  }
  const action = outcome.result;
  return action.approval_id === preview.approval_id &&
    action.operation_id === preview.operation_id &&
    action.external_action_id === preview.external_action_id
    ? action
    : null;
}

export async function ApprovalDetail({
  approvalId: rawApprovalId,
  workspaceId,
  timeZone = "UTC",
}: {
  approvalId: string | undefined;
  workspaceId: string;
  timeZone?: string;
}) {
  const id = approvalId(rawApprovalId);
  if (id === null) {
    return (
      <PlainState heading="Approval unavailable">
        <p>This approval cannot be reviewed.</p>
      </PlainState>
    );
  }

  const outcome = await callOperation(
    "core.approval.preview",
    { approval_id: id },
    await requestIdentity(),
    workspaceId,
  );
  if (outcome.state === "unavailable") {
    return (
      <PlainState heading="Approval temporarily unavailable">
        <p>Reload this page to try again. No decision has been submitted.</p>
      </PlainState>
    );
  }
  if (outcome.state !== "ok" || !isApprovalPreview(outcome.result)) {
    return (
      <PlainState heading="Approval unavailable">
        <p>This approval cannot be reviewed.</p>
      </PlainState>
    );
  }

  const preview = outcome.result;
  if (isRefusablePreview(preview)) {
    const refuse = refuseApproval.bind(null, {
      approvalId: id,
      workspaceId,
      payloadDigest: null,
    });
    return (
      <article className={styles.chrome}>
        <header>
          <h1>Close restored approval</h1>
          <p>This restored approval cannot be executed. Refusing closes the held request.</p>
        </header>
        <ApprovalDecisionForms refuse={refuse} />
      </article>
    );
  }

  if (isActionablePreview(preview)) {
    const binding = {
      approvalId: id,
      workspaceId,
      payloadDigest: preview.payload_digest,
    };
    return (
      <article className={styles.chrome}>
        <header>
          <h1>Review Current handoff</h1>
          <p>No action happens until you choose.</p>
          {preview.status === "restored" ? (
            <p className={styles.notice}>This restored approval needs a fresh decision.</p>
          ) : null}
        </header>
        <Snapshot preview={preview} timeZone={timeZone} history={false} />
        <ApprovalDecisionForms
          approve={approveApproval.bind(null, binding)}
          refuse={refuseApproval.bind(null, binding)}
        />
      </article>
    );
  }

  if (isHistoryPreview(preview)) {
    const action = preview.status === "resolved" ? await loadAction(preview, workspaceId) : null;
    return (
      <article className={styles.chrome}>
        {preview.status === "expired" ? (
          <header className={styles.result} tabIndex={-1}>
            <h1>Approval expired</h1>
            <p>This verified snapshot is read-only history. It cannot be approved or rebuilt.</p>
          </header>
        ) : actionStatus(action, preview)}
        <p className={styles.historyLabel}>Read-only approval history</p>
        <Snapshot preview={preview} timeZone={timeZone} history />
      </article>
    );
  }

  return (
    <PlainState heading="Approval unavailable">
      <p>This approval cannot be reviewed.</p>
    </PlainState>
  );
}
