import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { ActionForm, FormResult, settle } from "./action-form";
import { JOBSEARCH_PRESET_ID } from "./data";
import { REGISTRATION_REFUSALS } from "./jobfeed-panel";

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
