import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { JOBSEARCH_PRESET_ID, type Row } from "./data";
import {
  failureCopy,
  findings,
  progressLine,
  refusalCopy,
  rowsFor,
  sections,
  stageFromHash,
  stageWords,
  where,
} from "./proposal";
import { ProposalWorkflow } from "./proposal-workflow";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const REF = "leads.opportunity:0a1b2c3d-0000-7000-8000-00000000b001";
const POSTING = "Build a small example storefront.\n\nPlease include your rate.";

const ready: Row = {
  opportunity_ref: REF,
  state: "ready",
  questions: "What is your rate?",
  sections: [
    {
      id: "s0",
      position: 0,
      kind: "cover_letter",
      question: null,
      body: "Checkout is where carts are lost. <b>Example Shop</b> takes payments.",
      library_answer_id: null,
      library_match_id: null,
      edited: false,
    },
    {
      id: "s1",
      position: 1,
      kind: "answer",
      question: "What is your rate?",
      body: "Fifty an hour.",
      library_answer_id: null,
      library_match_id: "lib1",
      edited: true,
    },
  ],
  draft: "Checkout is where carts are lost.\n\n---\n\n**What is your rate?**\nFifty an hour.",
  findings: {
    accuracy: [
      {
        claim: "Kubernetes",
        kind: "entity",
        verdict: "flagged",
        context: "runs on Kubernetes",
        flagged_pre_retry: true,
      },
      { claim: "Next.js", kind: "entity", verdict: "whitelisted", context: "" },
    ],
    accuracy_checked: true,
    flagged_pre_retry: ["Kubernetes"],
    retried: true,
    style: [
      {
        rule: "long_paragraph",
        location: "answer:1/para:2",
        excerpt: "A long one",
      },
    ],
    style_pre_repair: [{ rule: "em_dash" }, { rule: "long_paragraph" }],
    style_repaired: true,
    analysis: {
      angle: "The storefront proves checkout work.",
      hiring_for: "A checkout fix.",
      must_address: ["Fixed price only"],
      gaps: ["Rust"],
    },
  },
};

function render(proposal: Row, posting = POSTING) {
  return renderToStaticMarkup(
    <ProposalWorkflow
      submit={vi.fn(async () => ok({}))}
      opportunityRef={REF}
      proposal={proposal}
      posting={posting}
      paste={<p>paste-form-marker</p>}
    />,
  );
}

describe("proposal helpers", () => {
  it("reads the tab from the hash and says each tab's state", () => {
    expect(stageFromHash("#stage-proposal")).toBe("proposal");
    expect(stageFromHash("#stage-pricing")).toBe("posting");
    expect(stageFromHash("")).toBe("posting");
    expect(stageWords({ state: "idle" }, "", "")).toEqual({
      posting: "Needed",
      questions: "Optional",
      proposal: "Draft next",
    });
    expect(stageWords(ready, POSTING, "What is your rate?")).toEqual({
      posting: "Ready",
      questions: "Added",
      proposal: "Ready",
    });
    // Typed but not yet saved with a draft: the tab says so.
    expect(stageWords(ready, POSTING, "Another?").questions).toBe("Unsaved");
    expect(stageWords({ state: "drafting" }, POSTING, "").proposal).toBe(
      "Drafting",
    );
    expect(stageWords({ state: "failed" }, POSTING, "").proposal).toBe("Failed");
  });
  it("words the progress, failures and refusals plainly", () => {
    expect(
      progressLine({ state: "drafting", stage: "Drafting", step: 2, total: 4 }),
    ).toBe("Drafting (2 of 4)");
    expect(progressLine({ state: "queued" })).toMatch(/^Queued/);
    expect(
      progressLine({ state: "queued", failure_kind: "usage_limited" }),
    ).toMatch(/usage limit/);
    expect(failureCopy("output_invalid")).toBe(
      "The model's answer could not be used.",
    );
    expect(failureCopy("something_new")).toBe(
      "Drafting failed (something_new).",
    );
    expect(refusalCopy("profile_required")).toMatch(/proposal profile/);
    expect(refusalCopy("whatever")).toBe("That did not go through. Try again.");
  });
  it("labels sections and sizes a textarea to its text", () => {
    expect(sections(ready).map((s) => s.label)).toEqual([
      "Cover letter",
      "What is your rate?",
    ]);
    expect(sections(ready)[1]?.libraryMatch).toBe("lib1");
    expect(rowsFor("", 4)).toBe(4);
    expect(rowsFor("a\nb\nc\nd\ne", 4)).toBe(6);
    expect(rowsFor("x".repeat(880), 2)).toBe(11);
  });
  it("summarises the findings, honestly when the check did not run", () => {
    const f = findings(ready.findings);
    expect(f?.flagged.map((c) => c.claim)).toEqual(["Kubernetes"]);
    expect(f?.style[0]).toEqual({
      rule: "Long paragraph",
      where: "Answer 1, paragraph 2",
      excerpt: "A long one",
    });
    expect(f?.repairedCount).toBe(2);
    expect(f?.clean).toBe(false);
    const unchecked = findings({ accuracy: [], accuracy_checked: false, style: [] });
    expect(unchecked?.checked).toBe(false);
    expect(unchecked?.clean).toBe(false);
    expect(findings({ accuracy: [], style: [] })?.clean).toBe(true);
    expect(findings(null)).toBeNull();
    expect(where("document")).toBe("Whole draft");
  });
});

describe("the proposal workflow", () => {
  it("opens on the posting with no draft, and moves the paste form there", () => {
    const html = render({ opportunity_ref: REF, state: "idle", sections: [] });
    expect(html).toContain('role="tablist"');
    expect(html.match(/role="tab"/g)).toHaveLength(3);
    expect(html).toMatch(/id="stage-tab-posting"[^>]*aria-selected="true"/);
    expect(html).toMatch(/id="stage-panel-proposal"[^>]*hidden=""/);
    expect(html).toContain("Build a small example storefront.");
    expect(html).toContain("paste-form-marker");
    expect(html).toContain("Draft proposal");
    expect(html).toContain("nothing is sent");
  });
  it("asks for the posting when the job has none", () => {
    expect(render({ state: "idle", sections: [] }, "")).toContain(
      "No posting text yet",
    );
  });
  it("shows a running draft's stage and no draft controls", () => {
    const html = render({
      opportunity_ref: REF,
      state: "drafting",
      stage: "Checking",
      step: 3,
      total: 4,
      sections: [],
    });
    expect(html).toMatch(/id="stage-tab-proposal"[^>]*aria-selected="true"/);
    expect(html).toContain("Checking (3 of 4)");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Drafting…<\/button>/);
    expect(html).not.toContain("Delete draft");
  });
  it("says why a draft failed and offers to draft again", () => {
    const html = render({
      opportunity_ref: REF,
      state: "failed",
      failure_kind: "deadline_exceeded",
      sections: [],
    });
    expect(html).toContain("The model took too long to answer. Draft again to retry.");
    expect(html).toContain("Regenerate proposal");
  });
  it("shows a ready draft as escaped cards with their tools and findings", () => {
    const html = render(ready);
    expect(html).toContain("Cover letter");
    expect(html).toContain("&lt;b&gt;Example Shop&lt;/b&gt;");
    expect(html).not.toContain("<b>Example Shop</b>");
    // The answer has a library match; the cover letter never gets library tools.
    expect(html.match(/Use the saved library answer/g)).toHaveLength(2);
    expect(html).toContain("Save this answer to the library");
    expect(html).toContain(">Edited<");
    expect(html).toContain("Copy the whole draft");
    expect(html).toContain("Delete draft");
    expect(html).toContain("Still not backed by your profile");
    expect(html).toContain("Kubernetes");
    expect(html).toContain("2 style issues were found and repaired.");
    expect(html).toContain("What the draft argues");
    expect(html).toContain("Fixed price only");
    expect(html).not.toContain("No issues found.");
  });
  it("says plainly when a ready draft is clean", () => {
    const html = render({
      ...ready,
      findings: { accuracy: [], accuracy_checked: true, style: [], analysis: {} },
    });
    expect(html).toContain("No issues found.");
  });
});

describe("the job detail", () => {
  it("reads the proposal and renders the workflow for a job", async () => {
    const answers: Record<string, OperationOutcome> = {
      "leads.opportunity.get": ok({
        ref: REF,
        revision: 3,
        data: {
          title: "Example storefront",
          preset_id: JOBSEARCH_PRESET_ID,
          stage: { label: "New" },
        },
      }),
      "leads.ui.queue": ok({
        items: [
          {
            ref: REF,
            id: "0a1b2c3d-0000-7000-8000-00000000b001",
            title: "Example storefront",
            pipeline_id: "0a1b2c3d-0000-7000-8000-00000000a002",
            fields: { posting_url: "https://www.example.com/jobs/~02100" },
            description: POSTING,
          },
        ],
      }),
      "leads.jobproposal.get": ok(ready),
    };
    const call = vi.fn(
      async (name: string) => answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
    );
    const api = {
      role: "owner",
      call,
      submit: vi.fn(async () => ok({})),
      href: (id: string) => `/leads/${id}`,
    } satisfies ShellApi;
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: REF } }),
    );
    expect(call).toHaveBeenCalledWith("leads.jobproposal.get", { ref: REF });
    expect(html).toContain('aria-label="Proposal workflow"');
    expect(html).toContain("Fifty an hour.");
    // The paste form lives in the Posting tab now, not again under the evidence.
    expect(html).not.toContain("<summary>Paste the full posting</summary>");
    expect(api.submit).not.toHaveBeenCalled();
  });
});
