"use client";

import { useActionState } from "react";

import type { ApprovalDecisionState } from "@/shell/approval-actions";
import { ApprovalActionError } from "@/shell/approval-decision-focus";

import styles from "./chrome/approval-detail.module.css";

type DecisionAction = (
  state: ApprovalDecisionState,
  formData: FormData,
) => Promise<ApprovalDecisionState>;

const INITIAL: ApprovalDecisionState = { state: "idle", message: "" };

export function ApprovalDecisionForms({
  approve,
  refuse,
}: {
  approve?: DecisionAction;
  refuse: DecisionAction;
}) {
  return approve === undefined ? (
    <RefusalOnlyForm refuse={refuse} />
  ) : (
    <ActionableForms approve={approve} refuse={refuse} />
  );
}

function RefusalOnlyForm({ refuse }: { refuse: DecisionAction }) {
  const [refuseState, refuseAction, refusePending] = useActionState(refuse, INITIAL);
  const blocked = refusePending || refuseState.state === "error";
  return (
    <fieldset className={styles.actions} disabled={blocked}>
      <legend className={styles.sectionHeading}>Close request</legend>
      <form action={refuseAction} aria-busy={refusePending}>
        <button className={styles.refuse} type="submit">
          Refuse
        </button>
      </form>
      {refusePending ? (
        <p className={styles.progress} role="status">
          Submitting decision…
        </p>
      ) : null}
      {refuseState.message ? (
        <ApprovalActionError className={styles.error}>
          {refuseState.message}
        </ApprovalActionError>
      ) : null}
    </fieldset>
  );
}

function ActionableForms({ approve, refuse }: { approve: DecisionAction; refuse: DecisionAction }) {
  const [approveState, approveAction, approvePending] = useActionState(approve, INITIAL);
  const [refuseState, refuseAction, refusePending] = useActionState(refuse, INITIAL);
  const pending = approvePending || refusePending;
  const error = approveState.state === "error" ? approveState.message : refuseState.message;
  const blocked = pending || approveState.state === "error" || refuseState.state === "error";

  return (
    <fieldset className={styles.actions} disabled={blocked}>
      <legend className={styles.sectionHeading}>Decision</legend>
      <form action={approveAction} aria-busy={approvePending}>
        <button className={styles.approve} type="submit">
          Approve handoff
        </button>
      </form>
      <form action={refuseAction} aria-busy={refusePending}>
        <button className={styles.refuse} type="submit">
          Refuse
        </button>
      </form>
      {pending ? (
        <p className={styles.progress} role="status">
          Submitting decision…
        </p>
      ) : null}
      {error ? (
        <ApprovalActionError className={styles.error}>
          {error}
        </ApprovalActionError>
      ) : null}
    </fieldset>
  );
}
