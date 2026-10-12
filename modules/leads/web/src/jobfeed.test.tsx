import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { ActionForm, FormResult, settle } from "./action-form";
import { JOBSEARCH_PRESET_ID } from "./data";
import {
  HEALTH_LABELS,
  REGISTRATION_REFUSALS,
  REVOKE_COPY,
  accountOptions,
  healthLabel,
} from "./jobfeed-panel";
import {
  FINISH_REFUSALS,
  FinishConnecting,
  finishShown,
} from "./finish-connecting";
import { JobFeedCollector, JobFeedConnect } from "./jobfeed-panel";
import { isValidElement, type ReactElement, type ReactNode } from "react";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-/i;
// Synthetic, UUID-shaped ids so the visible-text check has something to catch.
const SERVICES_ID = "0a1b2c3d-0000-7000-8000-00000000a001";
const JOBS_ID = "0a1b2c3d-0000-7000-8000-00000000a002";
const CONNECTION_ID = "0a1b2c3d-0000-7000-8000-00000000c001";
const ORGANIZATION_ID = "org-synthetic-7f3e9a";
const AUTHORIZATION_URL = "https://auth.example.com/authorize?state=synthetic";

function shell(answers: Record<string, OperationOutcome>) {
  const call = vi.fn(
    async (name: string) =>
      answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
  );
  const submit = vi.fn(async () => ok({}));
  return {
    role: "owner",
    call,
    submit,
    href: (id: string) => `/leads/${id}`,
  } satisfies ShellApi;
}
const services = {
  kind: "pipeline",
  id: SERVICES_ID,
  name: "Service inquiries",
  preset_id: "0a1b2c3d-0000-7000-8000-00000000b001",
  stages: [],
};
const jobs = {
  kind: "pipeline",
  id: JOBS_ID,
  name: "Freelance jobs",
  preset_id: JOBSEARCH_PRESET_ID,
  stages: [],
};
const ON = [
  { kind: "capability", name: "jobsearch" },
  { kind: "jobfeed" },
] as const;
const pollConnection = {
  kind: "connection",
  id: CONNECTION_ID,
  ref: `leads.intake_connection:${CONNECTION_ID}`,
  name: "Job feed",
  transport: "poll",
  state: "active",
  rules: [
    {
      id: "0a1b2c3d-0000-7000-8000-00000000d001",
      action: "attach_by_field",
      enabled: true,
      conditions: [],
    },
    {
      id: "0a1b2c3d-0000-7000-8000-00000000d002",
      action: "create_opportunity",
      pipeline_id: JOBS_ID,
      enabled: true,
      conditions: [],
    },
  ],
};
const collectorHealth = ok({
  last_accepted_at: "2026-01-02T00:00:00Z",
  last_processed_at: "2026-01-02T00:00:00Z",
  lag_seconds: 0,
  unresolved_failures: 0,
  failed_delivery_count: 0,
  collector: {
    authorization_state: "connected",
    organization_id: ORGANIZATION_ID,
    account_choices: [{ organization_id: ORGANIZATION_ID, label: "Studio" }],
  },
});
async function page(items: unknown[], health?: OperationOutcome) {
  const answers: Record<string, OperationOutcome> = {
    "leads.ui.catalog": ok({ items }),
  };
  if (health) answers["leads.connection.health"] = health;
  return renderToStaticMarkup(
    await screens.connections({ shell: shell(answers), query: {} }),
  );
}
function block(html: string): string {
  return (
    html.match(/<section[^>]*><h2>Job feed<\/h2>[\s\S]*?<\/section>/)?.[0] ?? ""
  );
}
const visible = (html: string) => html.replace(/<[^>]*>/g, " ");

describe("job feed block", () => {
  it("is absent with every control when the catalog has no jobfeed row", async () => {
    // The capability row alone does not open the block: the jobfeed row does.
    const html = await page([
      services,
      jobs,
      { kind: "capability", name: "jobsearch" },
    ]);
    expect(block(html)).toBe("");
    expect(html).not.toContain("Job feed");
    expect(html).not.toContain("leads.jobfeed");
    expect(html).not.toContain('name="pipeline_ref"');
    const off = await page([services, jobs]);
    expect(off).not.toContain("Job feed");
    expect(off).not.toContain('name="pipeline_ref"');
    expect(off).not.toContain('value="job_search"');
  });
  it("keeps an existing collector connection's generic card while off", async () => {
    const html = await page([services, jobs, pollConnection], collectorHealth);
    expect(html.match(/<h2>Job feed<\/h2>/g)).toHaveLength(1);
    expect(html).toContain("advanced routing");
    expect(html).not.toContain('name="pipeline_ref"');
  });
  it("shows what the feed does and a connect form listing only Job search pipelines", async () => {
    const html = block(await page([services, jobs, ...ON]));
    expect(html).toContain("Collects new job postings");
    expect(html).toContain("Connect job feed");
    const options = [...html.matchAll(/<option value="([^"]*)"/g)].map(
      (m) => m[1],
    );
    expect(options).toEqual([`leads.pipeline:${JOBS_ID}`]);
    expect(html).toContain("Freelance jobs");
    expect(html).not.toContain("Service inquiries");
  });
  it("points at Create pipeline and offers no connect control without a Job search pipeline", async () => {
    const html = block(await page([services, ...ON]));
    expect(html).toContain("No Job search pipeline yet");
    expect(html).toContain("Create pipeline");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("Connect job feed");
  });
  it("renders the collector connection on one card", async () => {
    const html = await page(
      [services, jobs, ...ON, pollConnection],
      collectorHealth,
    );
    expect(html.match(/<h2>Job feed<\/h2>/g)).toHaveLength(1);
    expect(html.match(/Last accepted/g)).toHaveLength(1);
    expect(html).toContain("advanced routing");
    expect(html).not.toContain("Connect job feed");
  });
  it("shows the existing failure when the health read fails", async () => {
    const html = block(
      await page([services, jobs, ...ON, pollConnection], {
        state: "unavailable",
      }),
    );
    expect(html).toContain("Could not load this view");
  });
  it("shows no identifier or organization id as visible text", async () => {
    for (const html of [
      await page([services, jobs, ...ON]),
      await page([services, jobs, ...ON, pollConnection], collectorHealth),
    ]) {
      const text = visible(block(html));
      expect(text).toContain("Job feed");
      expect(text).not.toMatch(UUID);
      expect(visible(html)).not.toContain(ORGANIZATION_ID);
    }
  });
});

describe("redirecting form", () => {
  afterEach(() => vi.unstubAllGlobals());
  function page(protocol: string, hostname: string) {
    const assign = vi.fn();
    vi.stubGlobal("window", { location: { assign, protocol, hostname } });
    return assign;
  }
  const opened = (url: string) =>
    settle(
      ok({ authorization_url: url }),
      "authorization_url",
      window.location,
    );
  it("opens an https authorization URL", () => {
    const assign = page("https:", "leads.example.com");
    expect(opened(AUTHORIZATION_URL)).toEqual({ state: "redirecting" });
    expect(assign).toHaveBeenCalledExactlyOnceWith(AUTHORIZATION_URL);
  });
  it.each([
    ["a script URL", "javascript:alert(1)"],
    ["an unparseable value", "not a url"],
    ["a missing value", ""],
    ["plain http on a non-localhost page", "http://auth.example.com/authorize"],
  ])("refuses %s without navigating", (_, url) => {
    const assign = page("https:", "leads.example.com");
    const shown = opened(url);
    expect(shown.state).toBe("refused");
    expect(assign).not.toHaveBeenCalled();
    const html = renderToStaticMarkup(<FormResult result={shown} />);
    expect(html).toContain(
      "The change was refused. Check your access and try again.",
    );
    if (url) expect(html).not.toContain(url);
  });
  it("refuses plain http from an http page that is not localhost", () => {
    const assign = page("http:", "leads.example.com");
    expect(opened("http://localhost:8000/authorize").state).toBe("refused");
    expect(assign).not.toHaveBeenCalled();
  });
  it("allows plain http only on a localhost development page", () => {
    const assign = page("http:", "localhost");
    expect(opened("http://localhost:8000/authorize")).toEqual({
      state: "redirecting",
    });
    expect(assign).toHaveBeenCalledExactlyOnceWith(
      "http://localhost:8000/authorize",
    );
  });
  it("never renders the authorization URL", () => {
    page("https:", "leads.example.com");
    const html = renderToStaticMarkup(
      <FormResult result={opened(AUTHORIZATION_URL)} />,
    );
    expect(html).toContain("Opening authorization…");
    expect(html).not.toContain("example.com");
    expect(html).not.toContain("Saved.");
  });
  it("leaves forms without a redirect field unchanged", () => {
    const assign = page("https:", "leads.example.com");
    const result = ok({ authorization_url: AUTHORIZATION_URL });
    expect(settle(result, undefined, window.location)).toBe(result);
    expect(assign).not.toHaveBeenCalled();
    expect(renderToStaticMarkup(<FormResult result={result} />)).toContain(
      "Saved.",
    );
  });
  it("renders the connect form idle, with no URL anywhere", () => {
    const html = renderToStaticMarkup(
      <ActionForm
        submit={async () => ok({})}
        operation="leads.jobfeed.connect"
        button="Connect job feed"
        redirectField="authorization_url"
        fields={[]}
      />,
    );
    expect(html).toContain("Connect job feed");
    expect(html).not.toContain("example.com");
  });
});

describe("refusal copy", () => {
  it.each(Object.entries(REGISTRATION_REFUSALS))(
    "shows fixed copy for %s",
    (code, copy) => {
      const html = renderToStaticMarkup(
        <FormResult
          result={{ state: "refused", code, text: "" }}
          refusalCopy={REGISTRATION_REFUSALS}
        />,
      );
      expect(html).toContain(copy);
    },
  );
  it("shows the generic text for any other code", () => {
    for (const code of ["forbidden", "toString", "constructor"]) {
      const html = renderToStaticMarkup(
        <FormResult
          result={{ state: "refused", code, text: "" }}
          refusalCopy={REGISTRATION_REFUSALS}
        />,
      );
      expect(html).toContain(
        "The change was refused. Check your access and try again.",
      );
      for (const copy of Object.values(REGISTRATION_REFUSALS))
        expect(html).not.toContain(copy);
    }
  });
});

const SECOND_ORGANIZATION_ID = "org-synthetic-41c0d2";
const THIRD_ORGANIZATION_ID = "org-synthetic-9b77e1";
const COUNTS = {
  fetched: 41,
  imported: 7,
  skipped_dup: 30,
  refreshed: 2,
  filtered: 1,
  skipped_untracked: 0,
  errors: 1,
  detail_reads: 6,
  pages: 3,
};
function collectorPage(collector: Record<string, unknown>, code?: string) {
  return page(
    [services, jobs, ...ON, pollConnection],
    ok({
      last_accepted_at: "2026-01-02T00:00:00Z",
      last_processed_at: "2026-01-02T00:00:00Z",
      lag_seconds: 0,
      unresolved_failures: 0,
      failed_delivery_count: 0,
      last_error: code ?? null,
      collector: {
        authorization_state: "connected",
        last_completed_at: "2026-01-05T14:30:00Z",
        next_run_not_before: "2026-01-05T15:00:00Z",
        ...COUNTS,
        organization_id: ORGANIZATION_ID,
        account_choices: [
          { organization_id: ORGANIZATION_ID, label: "Studio" },
          { organization_id: SECOND_ORGANIZATION_ID, label: "Agency" },
        ],
        ...collector,
      },
    }),
  );
}
const STATES: [string, Record<string, unknown>, string?][] = [
  ["connected", {}],
  ["connected, reconnect declined", {}, "authorization_required"],
  ["connected, registration declined", {}, "registration_failed"],
  ["authorization required", { authorization_state: "authorization_required" }],
  ["revoked", { authorization_state: "revoked" }],
  [
    "account selection",
    { organization_id: null },
    "account_selection_required",
  ],
  [
    "account selection, nothing listed",
    { organization_id: null, account_choices: [] },
    "account_selection_required",
  ],
];
const status = (text: string) =>
  new RegExp(`<span class="_status_\\w+">${text}</span>`);
const forms = (html: string) =>
  [...html.matchAll(/<form[\s\S]*?<\/form>/g)].map((m) => m[0]);
const reconnect = (html: string) =>
  forms(html).find((f) => f.includes(">Reconnect<")) ?? "";

describe("job feed collector states", () => {
  it("shows a connected feed's facts by account label, with run counts collapsed", async () => {
    const html = block(await collectorPage({}));
    expect(html).toContain("Connected");
    expect(html).toContain("Jan 5, 2026, 2:30 PM UTC");
    expect(html).toContain("Jan 5, 2026, 3:00 PM UTC");
    expect(html).toContain("<dt>Account</dt><dd>Studio</dd>");
    expect(html).toContain("<dt>Health</dt><dd>No problems</dd>");
    const lastRun =
      html.match(
        /<details[^>]*><summary>Last run<\/summary>[\s\S]*?<\/details>/,
      )?.[0] ?? "";
    expect(lastRun).not.toMatch(/<details[^>]* open/);
    expect(lastRun).toContain("<dt>Fetched</dt><dd>41</dd>");
    expect(lastRun).toContain("<dt>Already collected</dt><dd>30</dd>");
    expect(lastRun).toContain("<dt>Pages</dt><dd>3</dd>");
    expect(lastRun.match(/<dt>/g)).toHaveLength(9);
    expect(reconnect(html)).toMatch(/class="_secondary_\w+"/);
    expect(html).toContain("<summary>Revoke access</summary>");
    expect(html).not.toContain("did not finish");
    expect(html).not.toContain("could not register");
  });
  it("falls back when nothing has run or is scheduled", async () => {
    const html = block(
      await collectorPage({
        last_completed_at: null,
        next_run_not_before: null,
      }),
    );
    expect(html).toContain("<dt>Last completed check</dt><dd>Not yet</dd>");
    expect(html).toContain(
      "<dt>Next check not before</dt><dd>Not scheduled</dd>",
    );
  });
  it("keeps a connected feed connected when a reconnect was declined", async () => {
    const html = block(await collectorPage({}, "authorization_required"));
    expect(html).toMatch(status("Connected"));
    expect(html).toContain(
      "The last reconnect did not finish. Collection continues with the existing authorization.",
    );
    expect(html).not.toContain("Authorization required");
    expect(html).toContain(
      "<dt>Health</dt><dd>Last reconnect did not finish</dd>",
    );
    expect(html).not.toContain("paused");
    expect(reconnect(html)).toMatch(/class="_secondary_\w+"/);
  });
  it("keeps a connected feed connected when a reconnect could not register", async () => {
    const html = block(await collectorPage({}, "registration_failed"));
    expect(html).toMatch(status("Connected"));
    expect(html).toContain(
      "The last reconnect could not register with the provider. Collection continues with the existing authorization.",
    );
    expect(html).not.toContain("did not finish");
    expect(reconnect(html)).toMatch(/class="_secondary_\w+"/);
  });
  it("pauses on lost authorization and makes Reconnect primary", async () => {
    const html = block(
      await collectorPage({ authorization_state: "authorization_required" }),
    );
    expect(html).toMatch(status("Authorization required"));
    expect(html).toContain("Collection is paused until you reconnect.");
    expect(reconnect(html)).toMatch(/class="_primary_\w+"/);
  });
  it("says a revoked feed keeps its opportunities and offers no revoke", async () => {
    const html = block(await collectorPage({ authorization_state: "revoked" }));
    expect(html).toMatch(status("Revoked: authorization required"));
    expect(html).toContain("Opportunities it already collected are kept.");
    expect(reconnect(html)).toMatch(/class="_primary_\w+"/);
    expect(html).not.toContain("Revoke access");
  });
  it("offers the listed accounts in the order received", async () => {
    const html = block(
      await collectorPage(
        {
          organization_id: null,
          account_choices: [
            { organization_id: SECOND_ORGANIZATION_ID, label: "Agency" },
            { organization_id: ORGANIZATION_ID, label: "Studio" },
            { organization_id: THIRD_ORGANIZATION_ID, label: "Consulting" },
          ],
        },
        "account_selection_required",
      ),
    );
    expect(html).toContain("Choose a freelancer account");
    expect(html).toContain("<dt>Account</dt><dd>Not selected</dd>");
    const select =
      forms(html).find((f) => f.includes('name="organization_id"')) ?? "";
    expect(
      [...select.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)</g)].map(
        (m) => [m[1], m[2]],
      ),
    ).toEqual([
      [SECOND_ORGANIZATION_ID, "Agency"],
      [ORGANIZATION_ID, "Studio"],
      [THIRD_ORGANIZATION_ID, "Consulting"],
    ]);
    expect(select).toContain("Use this account");
  });
  it("tells two accounts with one label apart by the id's last 6 characters only", () => {
    expect(
      accountOptions([
        { organization_id: ORGANIZATION_ID, label: "Studio" },
        { organization_id: SECOND_ORGANIZATION_ID, label: "Studio" },
        { organization_id: THIRD_ORGANIZATION_ID, label: "Agency" },
      ]),
    ).toEqual([
      { value: ORGANIZATION_ID, label: "Studio (…7f3e9a)" },
      { value: SECOND_ORGANIZATION_ID, label: "Studio (…41c0d2)" },
      { value: THIRD_ORGANIZATION_ID, label: "Agency" },
    ]);
  });
  it("offers Reconnect when no account was listed", async () => {
    const html = block(
      await collectorPage(
        { organization_id: null, account_choices: [] },
        "account_selection_required",
      ),
    );
    expect(html).toContain("No freelancer account was listed.");
    expect(html).not.toContain('name="organization_id"');
    expect(reconnect(html)).toMatch(/class="_primary_\w+"/);
  });
  it("confirms an account choice with fixed copy", () => {
    const html = renderToStaticMarkup(
      <FormResult
        result={ok({ state: "connected" })}
        okCopy="Saved. The next check is scheduled."
      />,
    );
    expect(html).toContain("Saved. The next check is scheduled.");
    expect(html).not.toContain("connected");
  });
  it("keeps revoke behind a closed disclosure with its consequence", async () => {
    const html = block(await collectorPage({}));
    const disclosure =
      html.match(
        /<details[^>]*><summary>Revoke access<\/summary>[\s\S]*?<\/details>/,
      )?.[0] ?? "";
    expect(disclosure).not.toMatch(/<details[^>]* open/);
    expect(disclosure).toContain("Revoking stops collection");
    expect(disclosure).toContain("Opportunities already collected are kept.");
    expect(disclosure).toContain(">Revoke access</button>");
    expect(html.match(/>Revoke access<\/button>/g)).toHaveLength(1);
  });
  it.each([
    ["revoked", "Access revoked. Collected opportunities are kept."],
    [
      "revocation_failed",
      "Ended here; the provider did not confirm revocation.",
    ],
    [
      "revocation_unavailable",
      "Ended here; the provider offers no revocation.",
    ],
  ])("says what revoking did upstream: %s", (upstream, copy) => {
    const html = renderToStaticMarkup(
      <FormResult
        result={ok({ state: "revoked", upstream })}
        okCopy={REVOKE_COPY}
      />,
    );
    expect(html).toContain(copy);
    expect(html).not.toContain("Saved.");
  });
  it.each(Object.entries(HEALTH_LABELS))(
    "labels the %s health code",
    async (code, text) => {
      expect(healthLabel(code)).toBe(text);
      expect(text).not.toContain("_");
      // A connected feed reads this code as a declined reconnect instead.
      const collector =
        code === "authorization_required"
          ? { authorization_state: "authorization_required" }
          : {};
      const html = block(await collectorPage(collector, code));
      expect(html).toContain(`<dt>Health</dt><dd>${text}</dd>`);
      expect(visible(html)).not.toContain(code);
    },
  );
  it("has exactly the nine collector health codes", () => {
    expect(Object.keys(HEALTH_LABELS).sort()).toEqual([
      "account_selection_required",
      "authorization_required",
      "collection_partial",
      "endpoint_misconfigured",
      "refresh_token_unsaved",
      "registration_failed",
      "token_refresh_failed",
      "upstream_rate_limited",
      "upstream_unavailable",
    ]);
  });
  it("falls back to the code's words for an unknown code", () => {
    expect(healthLabel("delivery_backlog")).toBe("delivery backlog");
    expect(healthLabel("toString")).toBe("toString");
    expect(healthLabel(null)).toBe("No problems");
  });
  it("shows the existing failure and no collector controls when the health read fails", async () => {
    const html = block(
      await page([services, jobs, ...ON, pollConnection], {
        state: "refused",
        code: "forbidden",
        text: "",
      }),
    );
    expect(html).toContain("Could not load this view");
    expect(html).not.toContain("Reconnect");
  });
  it("shows no collector controls with the capability off", async () => {
    const html = await page([services, jobs, pollConnection], collectorHealth);
    expect(html).toContain("<h2>Job feed</h2>");
    expect(html).toContain("Last accepted");
    for (const text of [
      "Reconnect",
      "Revoke access",
      "leads.jobfeed",
      "Last run",
      "organization_id",
    ])
      expect(html).not.toContain(text);
  });
  it.each(STATES)(
    "shows no identifier as visible text: %s",
    async (_, collector, code) => {
      const html = await collectorPage(collector, code);
      const text = visible(block(html));
      expect(text).toContain("Job feed");
      expect(text).not.toMatch(UUID);
      for (const id of [ORGANIZATION_ID, SECOND_ORGANIZATION_ID])
        expect(visible(html)).not.toContain(id);
    },
  );
  it("keeps organization ids to option values", async () => {
    const html = block(
      await collectorPage(
        {
          organization_id: null,
          account_choices: [
            { organization_id: ORGANIZATION_ID, label: "Studio" },
            { organization_id: SECOND_ORGANIZATION_ID, label: "Studio" },
          ],
        },
        "account_selection_required",
      ),
    );
    const outside = html.replace(/<option value="[^"]*"/g, "<option");
    expect(outside).not.toContain(ORGANIZATION_ID);
    expect(outside).not.toContain(SECOND_ORGANIZATION_ID);
    expect(visible(html)).toContain("Studio (…7f3e9a)");
  });
});

describe("job feed with job search turned off", () => {
  // The catalog's marker while off and a connection is not revoked yet. The
  // loopback fields are never sent with it; here they prove they are ignored.
  const OFF = { kind: "jobfeed", revoke_only: true } as const;
  const OFF_LOOPBACK = { ...OFF, loopback: true, pending: true };
  const choosing = ok({
    last_accepted_at: "2026-01-02T00:00:00Z",
    last_error: "account_selection_required",
    collector: {
      authorization_state: "connected",
      organization_id: null,
      account_choices: [{ organization_id: ORGANIZATION_ID, label: "Studio" }],
    },
  });
  function revokeOnly(html: string) {
    expect(html.match(/<h2>Job feed<\/h2>/g)).toHaveLength(1);
    expect(html).toContain(
      "<p>Job search is turned off, so collection is paused.</p>",
    );
    const disclosure =
      html.match(
        /<details[^>]*><summary>Revoke access<\/summary>[\s\S]*?<\/details>/,
      )?.[0] ?? "";
    expect(disclosure).not.toMatch(/<details[^>]* open/);
    expect(disclosure).toContain("Revoking stops collection");
    expect(html.match(/>Revoke access<\/button>/g)).toHaveLength(1);
    // The one form on the card is revoke's (the collector's routing is advanced).
    expect(forms(block(html))).toHaveLength(1);
    for (const text of [
      "Connect job feed",
      'name="pipeline_ref"',
      "Reconnect",
      "Finish connecting",
      'name="address"',
      "Choose a freelancer account",
      'name="organization_id"',
      "Use this account",
      "Last run",
      "Last completed check",
      'value="job_search"',
    ])
      expect(html).not.toContain(text);
    expect(visible(html)).not.toContain(ORGANIZATION_ID);
  }
  it("offers only revoke on the collector's card", async () => {
    for (const marker of [OFF, OFF_LOOPBACK]) {
      const html = await page([services, jobs, marker, pollConnection], choosing);
      revokeOnly(html);
      // The intake facts and routing of the connection stay, as for any card.
      expect(block(html)).toContain("<h3>Intake</h3>");
      expect(block(html)).toContain("Last accepted");
      expect(block(html)).toContain("advanced routing");
    }
  });
  it("offers only revoke even before the collector's intake row is listed", async () => {
    revokeOnly(await page([services, jobs, OFF_LOOPBACK]));
  });
  it("renders nothing of the job feed without the marker", async () => {
    // Off with no connection, or one already revoked: the catalog sends no marker.
    const none = await page([services, jobs]);
    expect(none).not.toContain("Job feed");
    expect(none).not.toContain("leads.jobfeed");
    const revoked = await page([services, jobs, pollConnection], collectorHealth);
    expect(revoked).not.toContain("Revoke access");
    expect(revoked).not.toContain("collection is paused");
  });
  it("shows a member nothing of it", async () => {
    const member = { ...shell({ "leads.ui.catalog": ok({ items: [OFF] }) }), role: "member" };
    const html = renderToStaticMarkup(
      await screens.connections({ shell: member, query: {} }),
    );
    expect(html).not.toContain("Revoke access");
    expect(html).not.toContain("Job feed");
  });
});

describe("finishing a pasted sign-in", () => {
  const LOOPBACK_PENDING = [
    { kind: "capability", name: "jobsearch" },
    { kind: "jobfeed", loopback: true, pending: true },
  ];
  const finishes = (html: string) => html.includes("Finish connecting");
  it("is offered only in loopback mode while this owner's sign-in is pending", async () => {
    expect(finishes(block(await page([services, jobs, ...ON])))).toBe(false);
    for (const marker of [
      { kind: "jobfeed", loopback: true, pending: false },
      { kind: "jobfeed", loopback: false, pending: true },
      { kind: "jobfeed", loopback: "true", pending: "true" },
    ]) {
      const html = block(
        await page([services, jobs, { kind: "capability", name: "jobsearch" }, marker]),
      );
      expect(finishes(html)).toBe(false);
    }
    const html = block(await page([services, jobs, ...LOOPBACK_PENDING]));
    expect(finishes(html)).toBe(true);
    expect(html).toContain("Connect job feed");
  });
  it("is offered on a connected feed's card for a pending reconnect", async () => {
    const plain = await page(
      [services, jobs, ...ON, pollConnection],
      collectorHealth,
    );
    expect(finishes(block(plain))).toBe(false);
    const html = block(
      await page(
        [services, jobs, ...LOOPBACK_PENDING, pollConnection],
        collectorHealth,
      ),
    );
    expect(finishes(html)).toBe(true);
    expect(html).toContain("Reconnect");
  });
  it("asks for the address with plain help and no URL", async () => {
    const html = block(await page([services, jobs, ...LOOPBACK_PENDING]));
    expect(html).toContain("Address from your browser");
    expect(html).toContain("a page that can&#x27;t be reached");
    expect(html).toContain("Copy the full address from the address bar");
    const input = html.match(/<input[^>]*name="address"[^>]*>/)?.[0] ?? "";
    expect(input).toContain('type="text"');
    expect(input).toContain("required");
    expect(input).toMatch(/maxlength="4096"/i);
    expect(html).toContain('aria-labelledby="jobfeed-finish-heading"');
    expect(visible(html)).not.toContain("127.0.0.1");
    expect(visible(html)).not.toMatch(UUID);
  });
  const refusedResult = { field: "state", value: "refused", codeField: "reason" };
  it("shows a refused completion result as its fixed refusal copy", () => {
    const location = { assign: vi.fn(), protocol: "https:", hostname: "leads.example.com" };
    const shown = settle(
      ok({ state: "refused", reason: "state_expired" }),
      undefined,
      location,
      refusedResult,
    );
    expect(shown).toEqual({ state: "refused", code: "state_expired", text: "" });
    const html = renderToStaticMarkup(
      <FormResult result={shown} refusalCopy={FINISH_REFUSALS} />,
    );
    expect(html).toContain(FINISH_REFUSALS.state_expired);
    expect(html).toContain('role="alert"');
    const connected = ok({ state: "connected", reason: null });
    expect(settle(connected, undefined, location, refusedResult)).toBe(connected);
    expect(location.assign).not.toHaveBeenCalled();
  });
  it.each(Object.entries(FINISH_REFUSALS))("shows fixed copy for %s", (code, copy) => {
    const html = renderToStaticMarkup(
      <FormResult
        result={{ state: "refused", code, text: "" }}
        refusalCopy={FINISH_REFUSALS}
      />,
    );
    expect(html).toContain(copy);
  });
  it("covers every completion refusal and the address check", () => {
    expect(Object.keys(FINISH_REFUSALS).sort()).toEqual(
      [
        "account_mismatch",
        "address_invalid",
        "capability_disabled",
        "client_rejected",
        "endpoint_misconfigured",
        "exchange_failed",
        "issuer_mismatch",
        "no_refresh_token",
        "not_found",
        "provider_error",
        "record_stale",
        "state_expired",
        "state_unknown",
      ].sort(),
    );
  });
});

/** The child-index path to the first element of ``type`` in an element tree,
 * without rendering function components (a server refresh re-renders the tree,
 * and React keeps a client component's state while its type and path hold). */
function pathTo(node: ReactNode, type: unknown, at: number[] = []): number[] | null {
  if (Array.isArray(node)) {
    for (const [index, child] of node.entries()) {
      const found = pathTo(child, type, [...at, index]);
      if (found) return found;
    }
    return null;
  }
  if (!isValidElement(node)) return null;
  if (node.type === type) return at;
  const children = (node as ReactElement<{ children?: ReactNode }>).props.children;
  return children === undefined ? null : pathTo(children, type, [...at, -1]);
}
function finishProps(node: ReactNode): { pending: boolean } | null {
  const path = pathTo(node, FinishConnecting);
  if (!path) return null;
  let current: ReactNode = node;
  for (const index of path) {
    current =
      index === -1
        ? (current as ReactElement<{ children?: ReactNode }>).props.children
        : (current as ReactNode[])[index];
  }
  return (current as ReactElement<{ pending: boolean }>).props;
}

describe("a finished sign-in's answer survives the screen refresh", () => {
  const submitting = shell({});
  it("keeps the form once it has answered, after the sign-in stops pending", () => {
    expect(finishShown(true, false)).toBe(true);
    expect(finishShown(false, true)).toBe(true);
    expect(finishShown(false, false)).toBe(false);
  });
  it("keeps the form's element at one place on the connect block either way", () => {
    const before = JobFeedConnect({
      shell: submitting,
      pipelines: [jobs],
      finish: { pending: true },
    });
    const after = JobFeedConnect({
      shell: submitting,
      pipelines: [jobs],
      finish: { pending: false },
    });
    expect(pathTo(before, FinishConnecting)).not.toBeNull();
    expect(pathTo(after, FinishConnecting)).toEqual(pathTo(before, FinishConnecting));
    expect(finishProps(before)?.pending).toBe(true);
    expect(finishProps(after)?.pending).toBe(false);
    const identity = JobFeedConnect({ shell: submitting, pipelines: [jobs] });
    expect(pathTo(identity, FinishConnecting)).toBeNull();
  });
  it("keeps the form's element at one place on the collector card either way", () => {
    const collector = { authorization_state: "connected" };
    const before = JobFeedCollector({
      shell: submitting,
      collector,
      code: null,
      finish: { pending: true },
    });
    // A declined reconnect changes the health code as the sign-in is consumed.
    const after = JobFeedCollector({
      shell: submitting,
      collector,
      code: "authorization_required",
      finish: { pending: false },
    });
    expect(pathTo(before, FinishConnecting)).not.toBeNull();
    expect(pathTo(after, FinishConnecting)).toEqual(pathTo(before, FinishConnecting));
    expect(finishProps(after)?.pending).toBe(false);
  });
  it("renders nothing before any answer while no sign-in is pending", () => {
    const html = renderToStaticMarkup(
      <FinishConnecting submit={submitting.submit} pending={false} />,
    );
    expect(html).toBe("");
  });
});
