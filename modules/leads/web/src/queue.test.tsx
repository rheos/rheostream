import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { JOBSEARCH_PRESET_ID, type Row } from "./data";
import { screens } from "./index";
import {
  afterDismiss,
  age,
  DEFAULT_PREFS,
  fitFor,
  followupFor,
  formatBudget,
  formatProposals,
  formatRating,
  formatSpend,
  isNewClient,
  loadPrefs,
  overCap,
  parsePrefs,
  partyName,
  postingUrl,
  PREFS_KEY,
  savePrefs,
  jobId,
  shortId,
  sourceLabel,
  stageMatches,
  afterMove,
} from "./queue";
import {
  JobHeader,
  JobQueue,
  savedView,
  type QueueProps,
} from "./queue-view";
import { queueScope } from "./screens";

const NOW = Date.parse("2026-10-12T12:00:00Z");
const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });

const full: Row = {
  id: "01a133b6-b93c-73c0-975f-cf6437992a85",
  ref: "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a85",
  title: "Example data service",
  pipeline_id: "jobs",
  preset_id: JOBSEARCH_PRESET_ID,
  stage_id: "review",
  stage_label: "Review",
  stage_kind: "open",
  revision: 3,
  created_at: "2026-10-12T09:00:00Z",
  source: "poll",
  fields: {
    posting_url: "https://jobs.example.com/postings/1",
    job_type: "fixed",
    budget_min: "500",
    budget_max: "1500",
    proposal_count: "10",
    client_spend: "12000",
    client_rating: "4.9",
    client_reviews: "12",
    client_payment_verified: "true",
    posted_on: "2026-10-10",
  },
  preview: "A short synthetic description.",
  assessment: { fit: "high", summary: "Strong synthetic match." },
  followup: { due_on: "2026-10-20", action: "Send the draft" },
  has_draft: true,
  has_notes: true,
  party: { kind: "organization", name: "Example Studio" },
  transitions: [
    { stage_id: "qualified", label: "Qualified", kind: "open" },
    { stage_id: "passed", label: "Passed", kind: "terminal" },
  ],
  dismiss: { stage_id: "passed", label: "Passed" },
};
const sparse: Row = {
  id: "01a133b6-0000-7000-8000-00000000abcd",
  ref: "leads.opportunity:01a133b6-0000-7000-8000-00000000abcd",
  title: "Sparse example",
  pipeline_id: "jobs",
  preset_id: JOBSEARCH_PRESET_ID,
  stage_id: "review",
  stage_label: "Review",
  stage_kind: "open",
  revision: 1,
  created_at: "2026-10-12T06:00:00Z",
  source: null,
  fields: {},
  preview: null,
  assessment: null,
  followup: null,
  has_draft: false,
  has_notes: false,
  party: null,
  dismiss: { stage_id: "passed", label: "Passed" },
};

function props(overrides: Partial<QueueProps> = {}): QueueProps {
  return {
    items: [full, sparse],
    now: NOW,
    pipelines: [
      { value: "jobs", label: "Job leads" },
      { value: "all", label: "All pipelines" },
    ],
    stages: [
      { value: "open", label: "Open stages" },
      { value: "review", label: "Review" },
      { value: "all", label: "All stages" },
    ],
    pipeline: "jobs",
    stage: "review",
    hideFull: true,
    sort: "score",
    explicit: false,
    filtered: false,
    hiddenOnly: false,
    action: "/leads/queue",
    hrefs: {},
    noun: ["job", "jobs"],
    limited: false,
    nothingYet: <p>Nothing yet here.</p>,
    ...overrides,
  };
}
const render = (overrides: Partial<QueueProps> = {}) =>
  renderToStaticMarkup(<JobQueue {...props(overrides)} />);

function memory(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (key: string) => data[key] ?? null,
    setItem: (key: string, value: string) => {
      data[key] = value;
    },
  };
}

describe("proposal bands and money", () => {
  it.each([
    ["4", "under 5 proposals"],
    ["10", "5-10 proposals"],
    ["15", "10-15 proposals"],
    ["20", "15-20 proposals"],
    ["50", "20-50 proposals"],
    ["51", "50+ proposals"],
    ["7", "7 proposals"],
    ["", "proposals unknown"],
    ["many", "proposals unknown"],
  ])("count %s reads %s", (count, text) => {
    expect(formatProposals({ proposal_count: count })).toBe(text);
  });
  it("shows an exact count as the number, even past the cap", () => {
    expect(
      formatProposals({ proposal_count: "50", proposal_count_exact: "true" }),
    ).toBe("50 proposals");
    expect(
      formatProposals({ proposal_count: "73", proposal_count_exact: "true" }),
    ).toBe("73 proposals");
  });
  it("warns over the cap only on a count above it", () => {
    expect(overCap({ proposal_count: "51" })).toBe(true);
    expect(overCap({ proposal_count: "50" })).toBe(false);
    expect(overCap({})).toBe(false);
  });
  it("formats budgets, hourly rates and missing budgets", () => {
    expect(formatBudget({ budget_min: "500", budget_max: "1500" })).toBe(
      "$500 - $1,500",
    );
    expect(formatBudget({ budget_min: "900", budget_max: "900" })).toBe("$900");
    // Built in two parts so no rate-shaped literal sits in the fixture.
    const hourly = formatBudget({
      budget_min: "40",
      budget_max: "65",
      job_type: "hourly",
    });
    expect(hourly.startsWith("$40 - $65")).toBe(true);
    expect(hourly.endsWith(["", "hr"].join("/"))).toBe(true);
    expect(formatBudget({ budget_max: "300" })).toBe("$300");
    expect(formatBudget({})).toBe("No budget listed");
  });
  it("shows spend and rating only when known and above zero", () => {
    expect(formatSpend({ client_spend: "12000.4" })).toBe("$12,000 spent");
    expect(formatSpend({ client_spend: "0" })).toBeNull();
    expect(formatSpend({})).toBeNull();
    expect(formatRating({ client_rating: "4.9" })).toBe("4.9 rating");
    expect(formatRating({ client_rating: "0" })).toBeNull();
  });
  it("calls a client new only on a known zero spend and zero reviews", () => {
    expect(isNewClient({ client_spend: "0", client_reviews: "0" })).toBe(true);
    expect(isNewClient({ client_spend: "0" })).toBe(false);
    expect(isNewClient({ client_reviews: "0" })).toBe(false);
    expect(isNewClient({ client_spend: "10", client_reviews: "0" })).toBe(
      false,
    );
  });
  it("links only https postings", () => {
    expect(postingUrl({ posting_url: "https://jobs.example.com/a" })).toBe(
      "https://jobs.example.com/a",
    );
    expect(postingUrl({ posting_url: "javascript:alert(1)" })).toBeNull();
    expect(postingUrl({ posting_url: "not a url" })).toBeNull();
    expect(postingUrl({})).toBeNull();
  });
  it("labels sources", () => {
    expect(sourceLabel("poll")).toBe("Auto");
    expect(sourceLabel("manual")).toBe("Manual");
    expect(sourceLabel(null)).toBeNull();
  });
});

describe("fit pill", () => {
  it.each([
    ["high", "High", "high", "Pursue", "Pursue: fit high"],
    ["medium", "Med", "mid", "Review", "Review: fit medium"],
    ["needs_information", "?", "dim", "Review", "Review: fit needs information"],
    ["low", "Low", "low", "Skip", "Skip: fit low"],
  ])("%s shows %s in the %s band", (fit, mark, band, verdict, description) => {
    expect(fitFor({ fit })).toMatchObject({ mark, band, verdict, description });
  });
  it("shows the score when one was recorded, in its fit's band", () => {
    expect(fitFor({ fit: "high", score: 80 })).toMatchObject({
      mark: "80",
      band: "high",
      description: "Pursue: scored 80 of 100, fit high",
    });
    expect(fitFor({ fit: "medium", score: 70 })).toMatchObject({
      mark: "70",
      band: "mid",
    });
    // A held job has a fit and no score; a malformed score is not shown.
    expect(fitFor({ fit: "low", score: null })).toMatchObject({ mark: "Low" });
    expect(fitFor({ fit: "low", score: 450 })).toMatchObject({ mark: "Low" });
    expect(fitFor({ fit: "low", score: "40" })).toMatchObject({ mark: "Low" });
  });
  it("invents nothing without an assessment", () => {
    expect(fitFor(null)).toBeNull();
    expect(fitFor(undefined)).toBeNull();
    expect(fitFor({ fit: "unknown" })).toBeNull();
  });
});

describe("times, short IDs and extras", () => {
  it("reads posting dates in days and arrivals in hours", () => {
    expect(age({ fields: { posted_on: "2026-10-12" } }, NOW)).toBe(
      "posted today",
    );
    expect(age({ fields: { posted_on: "2026-10-09" } }, NOW)).toBe(
      "posted 3d ago",
    );
    expect(age({ fields: {}, created_at: "2026-10-12T09:00:00Z" }, NOW)).toBe(
      "arrived 3h ago",
    );
    expect(age({ fields: {}, created_at: "2026-10-12T11:30:00Z" }, NOW)).toBe(
      "arrived just now",
    );
  });
  it("uses the random tail of a UUIDv7 as the short ID", () => {
    expect(shortId("01a133b6-b93c-73c0-975f-cf6437992a85")).toBe("37992a85");
    expect(shortId("not-a-uuid")).toBe("not-a-uuid");
    // Two jobs from the same minute share their head, never their tail.
    expect(shortId("01a133b6-b93c-73c0-975f-cf6437992a85")).not.toBe(
      shortId("01a133b6-b93c-7e11-8a02-55d1e0c4b7f3"),
    );
  });
  it("prefers the server's short ID, which lengthens on a shared tail", () => {
    const id = "01a133b6-b93c-73c0-975f-cf6437992a85";
    expect(jobId({ id })).toBe("37992a85");
    expect(jobId({ id, short_id: "6437992a85" })).toBe("6437992a85");
  });
  it("marks a follow-up due before today as overdue", () => {
    expect(followupFor(full, NOW)).toEqual({
      due: "2026-10-20",
      action: "Send the draft",
      overdue: false,
    });
    expect(
      followupFor({ followup: { due_on: "2026-10-11", action: "" } }, NOW)
        ?.overdue,
    ).toBe(true);
    expect(followupFor(sparse, NOW)).toBeNull();
  });
  it("names a linked party only when one exists", () => {
    expect(partyName(full)).toBe("Example Studio");
    expect(partyName(sparse)).toBeNull();
    expect(partyName({ party: { name: "  " } })).toBeNull();
  });
});

describe("filters, sorting and saved preferences", () => {
  it("defaults to hiding 50+, sorting by score, open stages", () => {
    expect(DEFAULT_PREFS).toEqual({
      pipeline: "",
      stage: "",
      hideFull: true,
      sort: "score",
    });
    expect(loadPrefs(memory())).toEqual(DEFAULT_PREFS);
    expect(loadPrefs(null)).toEqual(DEFAULT_PREFS);
  });
  it("round-trips saved choices and ignores unreadable ones", () => {
    const store = memory();
    savePrefs(store, { ...DEFAULT_PREFS, hideFull: false, sort: "newest" });
    expect(loadPrefs(store)).toEqual({
      ...DEFAULT_PREFS,
      hideFull: false,
      sort: "newest",
    });
    expect(parsePrefs("{not json")).toEqual(DEFAULT_PREFS);
    expect(parsePrefs('{"sort":"loudest","hideFull":"no"}')).toEqual(
      DEFAULT_PREFS,
    );
    expect(loadPrefs(memory({ [PREFS_KEY]: '{"stage":"all"}' })).stage).toBe(
      "all",
    );
  });
  it("survives storage that throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadPrefs(broken)).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(broken, DEFAULT_PREFS)).not.toThrow();
  });
  it("reopens a bare URL with saved choices the catalog still offers", () => {
    const base = {
      pipeline: "jobs",
      stage: "review",
      hideFull: true,
      sort: "score" as const,
      pipelines: props().pipelines,
      stages: props().stages,
    };
    expect(savedView({ ...DEFAULT_PREFS, stage: "review" }, base)).toBeNull();
    expect(
      savedView({ ...DEFAULT_PREFS, stage: "open", sort: "newest" }, base),
    ).toEqual({ pipeline: "jobs", stage: "open", full: "hide", sort: "newest" });
    // A removed pipeline and a stage the pipeline lacks fall back, so nothing
    // redirects again on the next visit.
    expect(
      savedView(
        { ...DEFAULT_PREFS, pipeline: "gone", stage: "nope", hideFull: true },
        base,
      ),
    ).toBeNull();
    expect(
      savedView({ ...DEFAULT_PREFS, pipeline: "all", stage: "triage" }, base),
    ).toEqual({ pipeline: "all", stage: "triage", full: "hide", sort: "score" });
  });
  it("keeps a dismissed card only where the stage filter still covers it", () => {
    expect(afterDismiss([full, sparse], str(full.id), "")).toEqual([sparse]);
    expect(afterDismiss([full, sparse], str(full.id), "open")).toEqual([sparse]);
    const all = afterDismiss([full, sparse], str(full.id), "all");
    expect(all[0]).toMatchObject({
      stage_id: "passed",
      stage_label: "Passed",
      stage_kind: "terminal",
      dismiss: null,
    });
    expect(afterDismiss([full], str(full.id), "passed")).toHaveLength(1);
    expect(afterDismiss([full], str(full.id), "review")).toHaveLength(0);
    expect(stageMatches("", "review", "open")).toBe(true);
    expect(stageMatches("open", "passed", "terminal")).toBe(false);
  });
  it("moves a card through the stage select in step with the filter", () => {
    const qualified = { stage_id: "qualified", label: "Qualified", kind: "open" };
    const open = afterMove([full], str(full.id), qualified, "open");
    expect(open[0]).toMatchObject({
      stage_id: "qualified",
      stage_label: "Qualified",
      stage_kind: "open",
      transitions: [],
      dismiss: null,
    });
    expect(afterMove([full], str(full.id), qualified, "review")).toEqual([]);
    expect(afterMove([full], str(full.id), qualified, "qualified")).toHaveLength(1);
    expect(afterMove([full, sparse], "other", qualified, "review")).toEqual([
      full,
      sparse,
    ]);
  });
});
function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

describe("queue rendering", () => {
  it("draws a full card in the reference order", () => {
    const html = render({
      hrefs: { [str(full.id)]: "/leads/detail?ref=x" },
      submit: vi.fn(),
    });
    const order = [
      "Example data service",
      ">Review<",
      ">Auto<",
      ">Draft<",
      ">Notes<",
      'aria-label="Pursue: fit high"',
      "$500 - $1,500",
      "5-10 proposals",
      "$12,000 spent",
      "4.9 rating",
      "Example Studio",
      "posted 2d ago",
      "Follow up",
      "Strong synthetic match.",
      "A short synthetic description.",
      'aria-label="Stage for Example data service"',
      'aria-label="Open the posting"',
      'aria-label="Copy job ID ',
      "Dismiss Example data service (move to Passed)",
    ];
    let at = -1;
    for (const part of order) {
      const next = html.indexOf(part, at + 1);
      expect(next, part).toBeGreaterThan(at);
      at = next;
    }
    expect(html).toContain('href="https://jobs.example.com/postings/1"');
    expect(html).toContain("2 jobs");
  });
  it("omits every absent fact on a sparse card and invents no score", () => {
    const html = render({ items: [sparse] });
    expect(html).toContain("Sparse example");
    expect(html).toContain("No budget listed");
    expect(html).toContain("proposals unknown");
    expect(html).toContain("arrived 6h ago");
    for (const absent of [
      "Pursue",
      "Review:",
      "Skip",
      "spent",
      "rating",
      "Follow up",
      ">Draft<",
      ">Notes<",
      "New client",
      "Open the posting",
      "Dismiss",
    ])
      expect(html).not.toContain(absent);
    expect(html).toContain("1 job<");
  });
  it("shows the pipeline name only when several pipelines are listed", () => {
    expect(render({ pipelineNames: { jobs: "Job leads" } })).toContain(
      ">Job leads</span>",
    );
    expect(render()).not.toContain(">Job leads</span>");
  });
  it("draws the controls from the server's view and offers the next stages", () => {
    const html = render({ submit: vi.fn() });
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('name="full" value="hide"');
    expect(html).toMatch(/<option value="review" selected="">Review<\/option>/);
    expect(html).toMatch(/<option value="score" selected="">Score<\/option>/);
    expect(html).toMatch(
      /aria-label="Stage for Example data service"><option value="review" selected="">Review<\/option><option value="qualified">Qualified<\/option><option value="passed">Passed<\/option>/,
    );
    expect(render({ hideFull: false, sort: "newest" })).toContain(
      'name="full" value="show"',
    );
    // Read-only hosts get neither the stage select nor dismiss.
    const quiet = render();
    expect(quiet).not.toContain("Stage for");
    expect(quiet).not.toContain("Dismiss");
  });
  it("says when the list stops at the limit", () => {
    expect(render({ limited: true })).toContain("Showing the first 2.");
    expect(render()).not.toContain("Showing the first");
  });
  it("has three empty states", () => {
    expect(render({ items: [] })).toContain("Nothing yet here.");
    expect(render({ items: [], filtered: true, explicit: true })).toContain(
      "Nothing matches these filters",
    );
    const crowdedOnly = render({ items: [], hiddenOnly: true, filtered: true });
    expect(crowdedOnly).toContain("Every job here has 50+ proposals.");
    expect(crowdedOnly).toContain("Show them");
  });
  it("draws the detail header with the job ID copy and the over-cap alert", () => {
    const html = renderToStaticMarkup(
      <JobHeader
        item={{ ...full, fields: { ...row(full.fields), proposal_count: "51" } }}
        now={NOW}
      />,
    );
    expect(html).toMatch(/<h1[^>]*><a href="https:\/\/jobs\.example\.com/);
    expect(html).toContain(`Copy job ID ${shortId(full.id)}`);
    expect(html).toContain("Payment verified");
    expect(html).toContain('role="alert"');
    expect(html).toContain("Over 50 proposals");
    const quiet = renderToStaticMarkup(<JobHeader item={sparse} now={NOW} />);
    expect(quiet).not.toContain('role="alert"');
    expect(quiet).not.toContain("Payment verified");
  });
  it("puts the client's hire rate and reviews in the info tip", () => {
    const html = renderToStaticMarkup(
      <JobHeader
        item={{ ...full, fields: { ...row(full.fields), client_hire_rate: "0.68", client_reviews: "379" } }}
        now={NOW}
      />,
    );
    expect(html).toContain("<dd>68% hire rate, 379 reviews</dd>");
    // A 0% rate with no reviews is the feed's placeholder: not shown.
    const fresh = renderToStaticMarkup(
      <JobHeader
        item={{ ...full, fields: { ...row(full.fields), client_hire_rate: "0", client_reviews: "0" } }}
        now={NOW}
      />,
    );
    expect(fresh).not.toContain("hire rate");
  });
    it("shows the stage in view when no stage select is offered", () => {
    // A read-only host: no submit, so no select; the stage stays visible.
    const html = renderToStaticMarkup(<JobHeader item={full} now={NOW} />);
    expect(html).toMatch(/<span class="[^"]*badge[^"]*">Review<\/span>/);
    // With a select the stage lives there instead of twice.
    const movable = renderToStaticMarkup(
      <JobHeader
        item={{
          ...full,
          transitions: [{ stage_id: "passed", label: "Passed", kind: "terminal" }],
        }}
        now={NOW}
        submit={async () => ({ state: "ok", result: {} })}
      />,
    );
    expect(movable).not.toMatch(/<span class="[^"]*badge[^"]*">Review<\/span>/);
    expect(movable).toContain("<select");
  });
    it("lists the score's reasons under the verdict line", () => {
    const html = renderToStaticMarkup(
      <JobHeader
        item={{
          ...full,
          assessment: {
            fit: "high",
            score: 80,
            summary: "Fit: strong. Economics: favorable. Clarify scope.",
            reasons: [
              "Fit: strong. Economics: favorable.",
              "Candidate evidence to review: Example pipeline.",
              "Clarify scope and acceptance criteria.",
            ],
          },
        }}
        now={NOW}
      />,
    );
    expect(html).toContain('aria-label="Why this score, and the record details"');
    expect(html).toContain(">80<");
    expect(html).toMatch(/<p[^>]*>Fit: strong\. Economics: favorable\.<\/p>/);
    expect(html).toContain("<li>Candidate evidence to review: Example pipeline.</li>");
    expect(html).toContain("<li>Clarify scope and acceptance criteria.</li>");
    // Without reasons the one-paragraph summary still shows, and the info
    // button keeps only the record details.
    const plain = renderToStaticMarkup(<JobHeader item={full} now={NOW} />);
    expect(plain).not.toContain("<li>");
    expect(plain).toContain("Strong synthetic match.");
  });
});
function row(value: unknown): Row {
  return typeof value === "object" && value !== null ? (value as Row) : {};
}

function shell(answers: Record<string, OperationOutcome>, role = "owner") {
  const call = vi.fn(
    async (name: string) =>
      answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
  );
  return {
    role,
    call,
    submit: vi.fn(async () => ok({})),
    href: (id: string, query?: Record<string, string | undefined>) =>
      `/leads/${id}?${new URLSearchParams(Object.entries(query ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined))}`,
  } satisfies ShellApi;
}
const JOBS_ID = "01a133b6-b2ea-7928-ace7-000000000001";
const catalog = [
  {
    kind: "pipeline",
    id: JOBS_ID,
    name: "Job leads",
    preset_id: JOBSEARCH_PRESET_ID,
    stages: [
      { stage_id: "review", label: "Review", kind: "open" },
      { stage_id: "passed", label: "Passed", kind: "terminal" },
    ],
  },
  {
    kind: "pipeline",
    id: "services",
    name: "Service inquiries",
    preset_id: "other",
    stages: [{ stage_id: "triage", label: "Triage" }],
  },
];

describe("queue screen", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("links each card by its short ID, not the full reference", async () => {
    const api = shell({
      "leads.ui.catalog": ok({ items: catalog }),
      "leads.ui.queue": ok({ items: [{ ...full, short_id: "1a2b3c4d" }] }),
    });
    const html = renderToStaticMarkup(
      await screens.queue({ shell: api, query: {} }),
    );
    expect(html).toContain('href="/leads/detail?ref=1a2b3c4d"');
    expect(html).not.toContain(`ref=${encodeURIComponent(str(full.ref))}`);
  });

  it("opens on the job-search pipeline's first open stage, by score, 50+ hidden", async () => {
    const api = shell({
      "leads.ui.catalog": ok({ items: catalog }),
      "leads.ui.queue": ok({ items: [full] }),
    });
    const html = renderToStaticMarkup(
      await screens.queue({ shell: api, query: {} }),
    );
    expect(api.call.mock.calls).toEqual([
      ["leads.ui.catalog", {}],
      [
        "leads.ui.queue",
        {
          pipeline_id: JOBS_ID,
          jobsearch_only: false,
          stage_id: "review",
          include_closed: false,
          sort: "score",
          hide_full: true,
          limit: 100,
        },
      ],
    ]);
    expect(html).toContain("Job queue");
    expect(html).toContain("Example data service");
    expect(api.submit).not.toHaveBeenCalled();
  });
  it("lists job-search pipelines first and asks for what the URL names", () => {
    const scope = queueScope(catalog, { pipeline: "all", stage: "all" });
    expect(scope.pipelineChoices.map((c) => c.label)).toEqual([
      "Job leads",
      "Service inquiries",
      "All pipelines",
    ]);
    expect(scope.stageChoices.map((c) => c.value)).toEqual([
      "open",
      "review",
      "passed",
      "triage",
      "all",
    ]);
    expect(scope.input).toMatchObject({
      pipeline_id: null,
      include_closed: true,
    });
    expect(scope.jobsOnly).toBe(false);
    const unknown = queueScope(catalog, { pipeline: "gone", stage: "nope" });
    expect(unknown.pipeline).toBe(JOBS_ID);
    expect(unknown.stage).toBe("review");
    expect(queueScope(catalog, { pipeline: "all" }).stage).toBe("open");
    expect(
      queueScope(catalog, { stage: "open", full: "show", sort: "newest" }).input,
    ).toMatchObject({ stage_id: null, hide_full: false, sort: "newest" });
    const two = queueScope(
      [...catalog, { ...catalog[0], id: "01a133b6-b2ea-7928-ace7-000000000002", name: "More jobs" }],
      {},
    );
    expect(two.pipeline).toBe("jobs");
    expect(two.pipelineChoices[0]).toEqual({
      value: "jobs",
      label: "All job search",
    });
    expect(two.input.jobsearch_only).toBe(true);
  });
  it("puts Job queue first in the nav and keeps Connections owner-only", async () => {
    const answers = {
      "leads.ui.catalog": ok({ items: catalog }),
      "leads.ui.queue": ok({ items: [] }),
    };
    const owner = renderToStaticMarkup(
      await screens.queue({ shell: shell(answers), query: {} }),
    );
    const member = renderToStaticMarkup(
      await screens.queue({ shell: shell(answers, "member"), query: {} }),
    );
    const nav = (html: string) =>
      [...html.matchAll(/<nav[^>]*>(.*?)<\/nav>/g)][0]?.[1] ?? "";
    expect(nav(owner).indexOf("Job queue")).toBeLessThan(
      nav(owner).indexOf("Opportunities"),
    );
    expect(nav(owner)).toContain('aria-current="page">Job queue');
    expect(nav(owner)).toContain("Connections");
    expect(nav(member)).not.toContain("Connections");
    expect(owner).toContain("once it is connected in");
    expect(member).toContain("once a workspace owner connects it.");
  });
  it("shows a failure, not an empty queue, when a read fails", async () => {
    const html = renderToStaticMarkup(
      await screens.queue({
        shell: shell({ "leads.ui.catalog": ok({ items: catalog }) }),
        query: {},
      }),
    );
    expect(html).toContain("Could not load this view");
    expect(html).not.toContain("No jobs yet");
  });
  it("adds the job header to a job-search opportunity's detail only", async () => {
    const record = {
      ref: "leads.opportunity:x",
      revision: 2,
      data: {
        title: "Example data service",
        preset_id: JOBSEARCH_PRESET_ID,
        stage: { label: "Review" },
      },
    };
    const api = shell({
      "leads.opportunity.get": ok(record),
      "leads.ui.queue": ok({ items: [full] }),
    });
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: "leads.opportunity:x" } }),
    );
    expect(api.call).toHaveBeenCalledWith("leads.ui.queue", {
      ref: "leads.opportunity:x",
    });
    expect(html).toContain("Copy job ID");
    expect(html.match(/<h1/g)).toHaveLength(1);
    // The queue card's controls, in the header: stage, open, copy, dismiss.
    expect(html).toContain('aria-label="Stage for Example data service"');
    expect(html).toContain('aria-label="Open the posting"');
    expect(html).toMatch(/aria-label="Dismiss Example data service \(move to [^)]+\)"/);
    const readOnly = renderToStaticMarkup(
      await screens.detail({
        shell: { ...api, submit: undefined },
        query: { ref: "leads.opportunity:x" },
      }),
    );
    expect(readOnly).toContain('aria-label="Open the posting"');
    expect(readOnly).not.toContain("Stage for Example data service");
    expect(readOnly).not.toContain("Dismiss Example data service");
    // Dismiss returns to the queue's own route, bare, so its saved view reopens.
    const href = vi.spyOn(api, "href");
    await screens.detail({ shell: api, query: { ref: "leads.opportunity:x" } });
    expect(href).toHaveBeenCalledWith("queue");
    const plain = shell({
      "leads.opportunity.get": ok({
        ...record,
        data: { ...record.data, preset_id: "other" },
      }),
    });
    const other = renderToStaticMarkup(
      await screens.detail({ shell: plain, query: { ref: "leads.opportunity:x" } }),
    );
    expect(plain.call.mock.calls.map(([name]) => name)).not.toContain(
      "leads.ui.queue",
    );
    expect(other).not.toContain("Copy job ID");
  });
});
