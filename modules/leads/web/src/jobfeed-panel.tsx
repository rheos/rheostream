import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ActionForm } from "./action-form";
import { JOBSEARCH_PRESET_ID, str, type Row } from "./data";
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

/** Pipelines on the Job search preset, matched by preset id, never by name. */
export function jobSearchPipelines(pipelines: Row[]): Row[] {
  return pipelines.filter((p) => str(p.preset_id) === JOBSEARCH_PRESET_ID);
}

/** The job feed block before a connection exists: what it does, and connect. */
export function JobFeedConnect({
  shell,
  pipelines,
}: {
  shell: ShellApi;
  pipelines: Row[];
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
    </section>
  );
}
