import { Fragment } from "react";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ActionForm, type OkCopy } from "./action-form";
import {
  JOBSEARCH_PRESET_ID,
  label,
  moment,
  rows,
  str,
  type Row,
} from "./data";
import styles from "./leads.module.css";

/** Fixed copy for the registration refusals connect and reconnect can return. */
export const REGISTRATION_REFUSALS: Record<string, string> = {
  registration_failed:
    "The provider did not accept the registration. Nothing changed. Try again, or ask the host operator to check configuration.",
  registration_unsupported:
    "The provider asked for a sign-in method this app does not support. Nothing changed.",
  endpoint_misconfigured:
    "Job search is not set up on this deployment. Ask the host operator to check configuration.",
};

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

/** The form that finishes a sign-in in the loopback redirect mode: the provider
 * sends the browser to an address nothing serves, and the owner pastes it here. */
export function FinishConnecting({
  submit,
}: {
  submit: NonNullable<ShellApi["submit"]>;
}) {
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

/** Pipelines on the Job search preset, matched by preset id, never by name. */
export function jobSearchPipelines(pipelines: Row[]): Row[] {
  return pipelines.filter((p) => str(p.preset_id) === JOBSEARCH_PRESET_ID);
}

/** The job feed block before a connection exists: what it does, and connect. */
export function JobFeedConnect({
  shell,
  pipelines,
  finish = false,
}: {
  shell: ShellApi;
  pipelines: Row[];
  /** A sign-in this owner started waits for its pasted address. */
  finish?: boolean;
}) {
  const choices = jobSearchPipelines(pipelines);
  return (
    <section className={styles.section}>
      <h2>Job feed</h2>
      <p>
        Collects new job postings into a Job search pipeline on a regular
        schedule.
      </p>
      {!choices.length ? (
        <p className={styles.meta}>
          No Job search pipeline yet. Use Create pipeline with the Job search
          preset, then connect it here.
        </p>
      ) : shell.submit ? (
        <ActionForm
          submit={shell.submit}
          operation="leads.jobfeed.connect"
          button="Connect job feed"
          redirectField="authorization_url"
          refusalCopy={REGISTRATION_REFUSALS}
          fields={[
            {
              name: "pipeline_ref",
              label: "Collect into",
              type: "select",
              required: true,
              options: choices.map((p) => ({
                value: `leads.pipeline:${str(p.id)}`,
                label: str(p.name),
              })),
            },
          ]}
        />
      ) : (
        <p className={styles.meta}>Editing is unavailable in this view.</p>
      )}
      {finish && shell.submit ? <FinishConnecting submit={shell.submit} /> : null}
    </section>
  );
}

/** Short labels for the codes the collector writes to its connection's health. */
export const HEALTH_LABELS: Record<string, string> = {
  authorization_required: "Reconnect needed",
  token_refresh_failed: "Could not renew access",
  refresh_token_unsaved: "Renewed access not saved",
  account_selection_required: "Choose an account",
  upstream_unavailable: "Provider unavailable",
  upstream_rate_limited: "Provider rate limit",
  collection_partial: "Last check incomplete",
  endpoint_misconfigured: "Not set up on this deployment",
  registration_failed: "Registration failed",
};
/** The health code as fixed text; an unknown code reads as its own words. */
export function healthLabel(code: unknown): string {
  const value = str(code);
  if (!value) return "No problems";
  return Object.hasOwn(HEALTH_LABELS, value)
    ? (HEALTH_LABELS[value] ?? label(value))
    : label(value);
}

/** What the revoke result's ``upstream`` field says, in plain copy. */
export const REVOKE_COPY: OkCopy = {
  field: "upstream",
  copy: {
    revoked: "Access revoked. Collected opportunities are kept.",
    revocation_failed: "Ended here; the provider did not confirm revocation.",
    revocation_unavailable: "Ended here; the provider offers no revocation.",
  },
  otherwise: "Ended here; the provider did not confirm revocation.",
};

/** Account choices as select options. The label is the account's own; two
 * choices sharing one get the last 6 characters of the id, and only then. */
export function accountOptions(
  choices: Row[],
): { value: string; label: string }[] {
  const name = (c: Row) => str(c.label) || "Unnamed account";
  const counts = new Map<string, number>();
  for (const c of choices) counts.set(name(c), (counts.get(name(c)) ?? 0) + 1);
  return choices.map((c) => {
    const id = str(c.organization_id);
    return {
      value: id,
      label:
        (counts.get(name(c)) ?? 0) > 1
          ? `${name(c)} (…${id.slice(-6)})`
          : name(c),
    };
  });
}

const RUN_COUNTS = [
  ["fetched", "Fetched"],
  ["imported", "Imported"],
  ["skipped_dup", "Already collected"],
  ["refreshed", "Refreshed"],
  ["filtered", "Filtered out"],
  ["skipped_untracked", "Not tracked"],
  ["errors", "Errors"],
  ["detail_reads", "Detail reads"],
  ["pages", "Pages"],
] as const;

const STATUS: Record<string, string> = {
  connected: "Connected",
  authorization_required: "Authorization required",
  revoked: "Revoked: authorization required",
};

/** The collector's state, facts and controls, from the owner's health read. */
export function JobFeedCollector({
  shell,
  collector,
  code,
  finish = false,
}: {
  shell: ShellApi;
  collector: Row;
  code: unknown;
  /** A reconnect this owner started waits for its pasted address. */
  finish?: boolean;
}) {
  const state = str(collector.authorization_state);
  const connected = state === "connected";
  const choices = rows(collector.account_choices);
  const options = accountOptions(choices);
  const selected = options.find(
    (o) => o.value && o.value === str(collector.organization_id),
  );
  // On a connected feed this code means a reconnect was declined, not that
  // collection stopped; the terminal state comes from authorization_state.
  const declined = connected && str(code) === "authorization_required";
  const choosing = connected && str(code) === "account_selection_required";
  const reconnectPrimary = !connected || (choosing && !choices.length);
  return (
    <>
      <p>
        <span className={styles.status}>{STATUS[state] ?? label(state)}</span>
      </p>
      {declined ? (
        <p className={styles.meta}>
          The last reconnect did not finish. Collection continues with the
          existing authorization.
        </p>
      ) : null}
      {connected && str(code) === "registration_failed" ? (
        <p className={styles.meta}>
          The last reconnect could not register with the provider. Collection
          continues with the existing authorization.
        </p>
      ) : null}
      {state === "authorization_required" ? (
        <p>Collection is paused until you reconnect.</p>
      ) : null}
      {state === "revoked" ? (
        <p>
          Collection has stopped. Opportunities it already collected are kept.
        </p>
      ) : null}
      {choosing ? (
        <div
          role="group"
          aria-labelledby="jobfeed-account-heading"
          className={styles.evidence}
        >
          <h3 id="jobfeed-account-heading">Choose a freelancer account</h3>
          {choices.length ? (
            <>
              <p>Pick the account whose jobs this feed collects.</p>
              {shell.submit ? (
                <ActionForm
                  submit={shell.submit}
                  operation="leads.jobfeed.select_account"
                  button="Use this account"
                  okCopy="Saved. The next check is scheduled."
                  fields={[
                    {
                      name: "organization_id",
                      label: "Freelancer account",
                      type: "select",
                      required: true,
                      options,
                    },
                  ]}
                />
              ) : null}
            </>
          ) : (
            <p>
              No freelancer account was listed. Reconnect with the account you
              use for job search.
            </p>
          )}
        </div>
      ) : null}
      <dl className={styles.facts}>
        <dt>Last completed check</dt>
        <dd>{moment(collector.last_completed_at, "Not yet")}</dd>
        <dt>Next check not before</dt>
        <dd>{moment(collector.next_run_not_before, "Not scheduled")}</dd>
        <dt>Health</dt>
        <dd>
          {declined ? "Last reconnect did not finish" : healthLabel(code)}
        </dd>
        <dt>Account</dt>
        <dd>{selected ? selected.label : "Not selected"}</dd>
      </dl>
      <details className={styles.evidence}>
        <summary>Last run</summary>
        <dl className={styles.facts}>
          {RUN_COUNTS.map(([key, text]) => (
            <Fragment key={key}>
              <dt>{text}</dt>
              <dd>{str(collector[key]) || "0"}</dd>
            </Fragment>
          ))}
        </dl>
      </details>
      {shell.submit ? (
        <>
          <ActionForm
            submit={shell.submit}
            operation="leads.jobfeed.reconnect"
            button="Reconnect"
            redirectField="authorization_url"
            refusalCopy={REGISTRATION_REFUSALS}
            buttonClass={reconnectPrimary ? "primary" : "secondary"}
            fields={[]}
          />
          {finish ? <FinishConnecting submit={shell.submit} /> : null}
          {state === "revoked" ? null : (
            <details className={styles.manualCapture}>
              <summary>Revoke access</summary>
              <p>
                Revoking stops collection and ends this app&apos;s access to the
                job feed. Opportunities already collected are kept.
              </p>
              <ActionForm
                submit={shell.submit}
                operation="leads.jobfeed.revoke"
                button="Revoke access"
                okCopy={REVOKE_COPY}
                buttonClass="secondary"
                fields={[]}
              />
            </details>
          )}
        </>
      ) : (
        <p className={styles.meta}>Editing is unavailable in this view.</p>
      )}
    </>
  );
}
