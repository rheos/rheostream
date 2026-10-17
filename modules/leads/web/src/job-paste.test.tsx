import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { JOBSEARCH_PRESET_ID, pipelineRef } from "./data";
import {
  JobPaste,
  PAGE_LIMIT,
  POSTING_LIMIT,
  pasteInput,
  pasteSummary,
} from "./job-paste";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const JOBS_ID = "0a1b2c3d-0000-7000-8000-00000000a002";
const PIPELINE = pipelineRef(JOBS_ID);
const URL_ = "https://www.example.com/jobs/~02100";

function shell(answers: Record<string, OperationOutcome>) {
  const call = vi.fn(
    async (name: string) =>
      answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
  );
  const submit = vi.fn(async () => ok({}));
  return {
    role: "member",
    call,
    submit,
    href: (id: string) => `/leads/${id}`,
  } satisfies ShellApi;
}

describe("job paste input", () => {
  it("sends a search page without a link", () => {
    expect(pasteInput("search_page", PIPELINE, "<article/>", "")).toEqual({
      pipeline_ref: PIPELINE,
      kind: "search_page",
      content: "<article/>",
    });
  });
  it("sends a posting with its trimmed link", () => {
    expect(pasteInput("posting", PIPELINE, "Title", ` ${URL_} `)).toEqual({
      pipeline_ref: PIPELINE,
      kind: "posting",
      content: "Title",
      posting_url: URL_,
    });
  });
  it.each([
    ["search_page", "", "<article/>", "", "Choose a job search pipeline."],
    ["search_page", PIPELINE, "  ", "", "Paste the search results."],
    ["posting", PIPELINE, "", URL_, "Paste the posting text."],
    ["posting", PIPELINE, "Title", " ", "Add the posting's link."],
  ] as const)(
    "names what is missing (%s)",
    (kind, pipeline, content, url, reason) => {
      expect(pasteInput(kind, pipeline, content, url)).toBe(reason);
    },
  );
  it("refuses text over the operation's limits before sending", () => {
    expect(
      pasteInput("posting", PIPELINE, "x".repeat(POSTING_LIMIT + 1), URL_),
    ).toMatch(/16,000/);
    expect(
      pasteInput("search_page", PIPELINE, "x".repeat(PAGE_LIMIT + 1), ""),
    ).toMatch(/too large/);
  });
});

describe("job paste summary", () => {
  it("says what was added, matched and skipped", () => {
    expect(pasteSummary({ accepted: 3, duplicate: 1, skipped: 2 })).toBe(
      "3 jobs added, 1 already here, 2 skipped. Processing is queued; refresh to see the jobs.",
    );
    expect(pasteSummary({ accepted: 1 })).toMatch(/^1 job added\./);
    expect(pasteSummary({ duplicate: 2 })).toBe("2 already here.");
    expect(pasteSummary({})).toBe("No jobs were found in what you pasted.");
  });
});

describe("job paste form", () => {
  it("offers only a posting, with its link prefilled, on a job", () => {
    const html = renderToStaticMarkup(
      <JobPaste
        submit={vi.fn()}
        kinds={["posting"]}
        pipelines={[{ value: PIPELINE, label: "This pipeline" }]}
        postingUrl={URL_}
      />,
    );
    expect(html).toContain("Posting link");
    expect(html).toContain(`value="${URL_}"`);
    expect(html).toContain("Add posting");
    expect(html).not.toContain("What are you pasting?");
    expect(html).not.toContain("Pipeline</label>");
  });
  it("offers both kinds and a pipeline choice on the queue", () => {
    const html = renderToStaticMarkup(
      <JobPaste
        submit={vi.fn()}
        pipelines={[
          { value: PIPELINE, label: "Freelance jobs" },
          { value: pipelineRef("other"), label: "Contract jobs" },
        ]}
      />,
    );
    expect(html).toContain("What are you pasting?");
    expect(html).toContain("A search results page");
    expect(html).toContain("One full posting");
    expect(html).toContain("Freelance jobs");
    expect(html).toContain("Add jobs");
    expect(html).not.toContain("Posting link");
  });
});

describe("job paste on the queue", () => {
  const jobs = {
    kind: "pipeline",
    id: JOBS_ID,
    name: "Freelance jobs",
    preset_id: JOBSEARCH_PRESET_ID,
    stages: [{ stage_id: "review", label: "Review", kind: "open" }],
  };
  it("is offered when a job search pipeline exists", async () => {
    const api = shell({
      "leads.ui.catalog": ok({ items: [jobs] }),
      "leads.ui.queue": ok({ items: [] }),
    });
    const html = renderToStaticMarkup(
      await screens.queue({ shell: api, query: {} }),
    );
    expect(html).toContain("<summary>Paste jobs</summary>");
    expect(api.submit).not.toHaveBeenCalled();
  });
  it("is not offered without a job search pipeline", async () => {
    const api = shell({
      "leads.ui.catalog": ok({
        items: [{ ...jobs, preset_id: "0a1b2c3d-0000-7000-8000-00000000b001" }],
      }),
      "leads.ui.queue": ok({ items: [] }),
    });
    const html = renderToStaticMarkup(
      await screens.queue({ shell: api, query: {} }),
    );
    expect(html).not.toContain("Paste jobs");
  });
});
