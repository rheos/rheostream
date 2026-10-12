"use client";
import { useState } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ActionForm } from "./action-form";
import styles from "./leads.module.css";

/** Fixed copy for every refusal finishing a pasted sign-in can return. */
export const FINISH_REFUSALS: Record<string, string> = {
  address_invalid:
    "That is not the address the provider sent you to. Copy the whole address from the address bar, starting with http, and paste it again.",
  state_unknown:
    "This sign-in was already used or replaced by a newer one. Connect again.",
  state_expired:
    "This sign-in expired. Connect again and paste the new address within 10 minutes.",
  provider_error:
    "The provider did not grant access. Nothing changed. Connect again to retry.",
  account_mismatch:
    "Another owner started this sign-in. Connect again from your own session.",
  capability_disabled: "Job search is off for this workspace.",
  issuer_mismatch:
    "The address came from a different provider. Nothing changed. Connect again.",
  exchange_failed:
    "The provider did not finish the sign-in. Nothing changed. Connect again.",
  no_refresh_token:
    "The provider did not grant lasting access. Nothing changed. Connect again.",
  client_rejected:
    "The provider no longer accepts the registration for this app. Connect again.",
  endpoint_misconfigured:
    "Job search is not set up on this deployment. Ask the host operator to check configuration.",
  record_stale: "The job feed changed meanwhile. Connect again.",
  not_found: "The job feed changed meanwhile. Connect again.",
};

/** What the Job feed card knows in the loopback redirect mode: whether a sign-in
 * this owner started is waiting for its pasted address. Absent in identity mode. */
export interface FinishState {
  pending: boolean;
}

/** Whether the form shows: while a sign-in waits, and from its first answer on.
 * A completion's refusal is an ok result, so the shell refreshes the screen and the
 * consumed sign-in is no longer pending; the answer must stay readable after that. */
export function finishShown(pending: boolean, answered: boolean): boolean {
  return pending || answered;
}

/** The form that finishes a sign-in in the loopback redirect mode: the provider
 * sends the browser to an address nothing serves, and the owner pastes it here.
 * The card renders this element whenever loopback mode is on, at a fixed place,
 * so a refresh after an answer keeps its state (and its result) mounted. */
export function FinishConnecting({
  submit,
  pending,
}: {
  submit: NonNullable<ShellApi["submit"]>;
  pending: boolean;
}) {
  const [answered, setAnswered] = useState(false);
  if (!finishShown(pending, answered)) return null;
  return (
    <div
      role="group"
      aria-labelledby="jobfeed-finish-heading"
      className={styles.evidence}
    >
      <h3 id="jobfeed-finish-heading">Finish connecting</h3>
      <p>
        After you approve, your browser shows a page that can&apos;t be reached.
        Copy the full address from the address bar, come back to this page and
        paste it here.
      </p>
      <ActionForm
        submit={submit}
        operation="leads.jobfeed.complete_pasted"
        button="Finish connecting"
        okCopy="Job feed connected. Reload the page to see its status."
        refusalCopy={FINISH_REFUSALS}
        refusedResult={{ field: "state", value: "refused", codeField: "reason" }}
        onResult={() => setAnswered(true)}
        fields={[
          {
            name: "address",
            label: "Address from your browser",
            required: true,
            maxLength: 4096,
          },
        ]}
      />
    </div>
  );
}
