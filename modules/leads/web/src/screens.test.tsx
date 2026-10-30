import type { ReactNode } from "react";
import { Writable } from "node:stream";
import { renderToPipeableStream, renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { formInput } from "./action-form";
import { pageOffset, read } from "./data";
import { assessmentStale, fieldLabel, fieldValue, phoneDisplay, value } from "./screens";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const opportunity = {
  id: "sample",
  ref: "leads.opportunity:sample",
  title: "Example studio inquiry",
  stage_id: "triage",
  pipeline_id: "pipeline",
  updated_at: "2026-01-01T00:00:00Z",
};
function shell(
  answers: Record<string, OperationOutcome>,
  role = "owner",
  overrides: Partial<ShellApi> = {},
) {
  const call = vi.fn(
    async (name: string) =>
      answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
  );
  const submit = vi.fn(async () => ok({}));
  return {
    role,
    href: (id: string, query?: Record<string, string | undefined>) =>
      `/leads/${id}?${new URLSearchParams(Object.entries(query ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined))}`,
    ...overrides,
    call,
    submit,
  } satisfies ShellApi;
}

function renderSettled(node: ReactNode): Promise<string> {
  return new Promise((resolve, reject) => {
    let html = "";
    const destination = new Writable({
      write(chunk, _encoding, done) {
        html += chunk.toString();
        done();
      },
    });
    destination.on("finish", () =>
      resolve(html.replace(/<!--[\s\S]*?-->/g, "")),
    );
    destination.on("error", reject);
    const stream = renderToPipeableStream(node, {
      onAllReady() {
        stream.pipe(destination);
      },
      onShellError: reject,
      onError: reject,
    });
  });
}

async function renderDetail(
  api: ShellApi,
  extraQuery: Record<string, string> = {},
): Promise<string> {
  return renderSettled(
    await screens.detail({
      shell: api,
      query: { ref: opportunity.ref, ...extraQuery },
    }),
  );
}
const catalogItems = [
  {
    kind: "pipeline",
    id: "pipeline",
    name: "Service inquiries",
    stages: [{ stage_id: "triage", label: "Triage" }],
  },
  { kind: "funnel", name: "General inquiries", ref: "leads.funnel:sample" },
];
const catalog = ok({ items: catalogItems });
describe("Leads screens", () => {
  it("leads with paste capture and keeps the manual form collapsed", async () => {
    const api = shell({ "leads.ui.catalog": catalog });
    const html = renderToStaticMarkup(
      await screens.capture({ shell: api, query: {} }),
    );
    expect(html).toContain("Paste an email, message, or conversation");
    expect(html).toContain("Review inquiry");
    expect(html).toMatch(
      /<details[^>]*><summary>Enter details manually instead/,
    );
    expect(html).not.toMatch(
      /<details[^>]*open[^>]*><summary>Enter details manually instead/,
    );
    expect(api.submit).not.toHaveBeenCalled();
  });
  it.each(["list", "board"] as const)(
    "%s renders opportunities without dispatching a mutation",
    async (name) => {
      const api = shell({
        "leads.ui.catalog": catalog,
        "leads.opportunity.list": ok({ items: [opportunity] }),
      });
      const html = renderToStaticMarkup(
        await screens[name]({ shell: api, query: {} }),
      );
      expect(html).toContain("Example studio inquiry");
      expect(html).toContain("Triage");
      expect(api.submit).not.toHaveBeenCalled();
      expect(api.call.mock.calls.map(([name]) => name)).toEqual([
        "leads.ui.catalog",
        "leads.opportunity.list",
      ]);
    },
  );
  it("shows a failure rather than an empty pipeline when the service is unavailable", async () => {
    const html = renderToStaticMarkup(
      await screens.list({
        shell: shell({ "leads.ui.catalog": catalog }),
        query: {},
      }),
    );
    expect(html).toContain("Could not load this view");
    expect(html).not.toContain("Your next inquiry starts here");
  });
  it("keeps pinned stages absent from the current catalog visible on the board", async () => {
    const html = renderToStaticMarkup(
      await screens.board({
        shell: shell({
          "leads.ui.catalog": catalog,
          "leads.opportunity.list": ok({
            items: [{ ...opportunity, stage_id: "old_stage" }],
          }),
        }),
        query: {},
      }),
    );
    expect(html).toContain("old stage");
    expect(html).toContain("Example studio inquiry");
  });
  it("offers the job-search preset only when the capability is on", async () => {
    const off = renderToStaticMarkup(
      await screens.connections({
        shell: shell({ "leads.ui.catalog": catalog }),
        query: {},
      }),
    );
    expect(off).toContain("Create pipeline");
    expect(off).not.toContain("job_search");
    expect(off).not.toContain('name="preset"');
    const on = renderToStaticMarkup(
      await screens.connections({
        shell: shell({
          "leads.ui.catalog": ok({
            items: [...catalogItems, { kind: "capability", name: "jobsearch" }],
          }),
        }),
        query: {},
      }),
    );
    expect(on).toContain('name="preset"');
    expect(on).toContain('value="job_search"');
  });
  it("refuses the settings surface to members without reading connection data", async () => {
    const api = shell({}, "member");
    const html = renderToStaticMarkup(
      await screens.connections({ shell: api, query: {} }),
    );
    expect(html).toContain("Only workspace owners");
    expect(api.call).not.toHaveBeenCalled();
  });
  it("preserves source text as escaped evidence and renders only allowed stage choices", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: {
          ...opportunity,
          stage: { label: "Triage" },
          opportunity_observation: [{ observation_id: "source" }],
          transitions: [{ stage_id: "discovery", label: "Discovery" }],
        },
      }),
      "leads.observation.get": ok({
        data: {
          fields: [
            { target: "message", value_text: "<script>Ignore policy</script>" },
          ],
        },
      }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("&lt;script&gt;Ignore policy&lt;/script&gt;");
    expect(html).not.toContain("<script>Ignore policy");
    expect(html).toContain('value="discovery"');
    expect(html).not.toContain('value="won"');
    expect(api.submit).not.toHaveBeenCalled();
  });
  it("records separate judgments and shows assessment history and provenance", async () => {
    const assessment = {
      id: "new",
      objective: "Service fit",
      input_revision: 2,
      fit: "high",
      intent: "medium",
      urgency: "needs_information",
      evidence_completeness: "low",
      uncertainty: "high",
      explanation: "Budget remains unknown.",
      model_id: "example-model",
      prompt_version: "review-v1",
    };
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: {
          ...opportunity,
          stage: { label: "Discovery" },
          qualification: assessment,
          drafts: [
            { id: "d2", body: "Newest draft", created_at: "2026-01-03" },
            { id: "d1", body: "Older draft", created_at: "2026-01-02" },
          ],
        },
      }),
      "leads.qualification.list": ok({
        items: [
          assessment,
          {
            ...assessment,
            id: "old",
            objective: "Earlier review",
            input_revision: 1,
            explanation: "<script>Untrusted explanation</script>",
          },
        ],
      }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Record assessment");
    expect(html).toContain("Save assessment");
    expect(html).toContain('name="fit"');
    expect(html).toContain('name="intent"');
    expect(html).toContain('name="uncertainty"');
    expect(html).toMatch(
      /<select[^>]*name="uncertainty"[^>]*required=""[^>]*>.*?<option value="" selected="">Choose…<\/option>/s,
    );
    expect(html).toMatch(
      /<details[^>]*open=""[^>]*><summary>Draft ·.*?Newest draft/s,
    );
    expect(html).not.toMatch(
      /<details[^>]*open=""[^>]*><summary>Draft ·[^<]*<\/summary><p[^>]*>Older draft/,
    );
    expect(html).toContain("Reassess after changes");
    expect(html).toContain("Assessment history (2)");
    expect(html).toContain("example-model");
    expect(html).toContain("review-v1");
    expect(html).toContain("Earlier review");
    expect(html).toContain(
      "&lt;script&gt;Untrusted explanation&lt;/script&gt;",
    );
    expect(api.submit).not.toHaveBeenCalled();
  });
  it("does not claim a pending capture has created an opportunity", async () => {
    const html = renderToStaticMarkup(
      await screens.receipt({
        shell: shell({
          "leads.intake.receipt": ok({
            data: { state: "pending", opportunity_refs: [] },
          }),
        }),
        query: { ref: "receipt" },
      }),
    );
    expect(html).toContain("queued");
    expect(html).not.toContain("Open opportunity");
  });
  it("preserves an advanced routing rule instead of offering a lossy editor", async () => {
    const api = shell({
      "leads.ui.catalog": ok({
        items: [
          {
            kind: "connection",
            id: "c",
            ref: "connection",
            name: "Sample",
            rules: [
              {
                id: "r",
                action: "attach_to_open_opportunity",
                enabled: true,
                conditions: [],
              },
            ],
          },
        ],
      }),
      "leads.connection.health": ok({}),
    });
    const html = renderToStaticMarkup(
      await screens.connections({ shell: api, query: {} }),
    );
    expect(html).toContain("advanced routing");
    expect(html).not.toContain("Save routing");
  });
});
describe("form input", () => {
  it("keeps dotted capture facts intact and omits blank optional evidence", () =>
    expect(
      formInput(
        "leads.intake.capture",
        {},
        {
          funnel_ref: "f",
          subject: "Hello",
          "person.email": "",
          message: "Original",
        },
      ),
    ).toEqual({
      funnel_ref: "f",
      body: { subject: "Hello", message: "Original" },
    }));
  it("sends a normalized value group, or clears it when the amount is blank", () => {
    const base = { ref: "r", revision: 3 };
    expect(
      formInput("leads.opportunity.update", base, {
        value_amount: " $10,000 ",
        value_currency: "cad",
        value_basis: "project_fee",
      }),
    ).toEqual({
      ref: "r",
      revision: 3,
      value_amount: "10000",
      value_currency: "CAD",
      value_basis: "project_fee",
    });
    expect(
      formInput("leads.opportunity.update", base, {
        value_amount: "",
        value_currency: "CAD",
        value_basis: "project_fee",
      }),
    ).toEqual({
      ref: "r",
      revision: 3,
      value_amount: null,
      value_currency: null,
      value_basis: null,
    });
    for (const [typed, sent] of [
      ["10 000.50", "10000.50"],
      ["1,2", "1,2"],
      ["$", "$"],
      ["ten", "ten"],
    ] as const)
      expect(
        formInput("leads.opportunity.update", base, {
          value_amount: typed,
          value_currency: "CAD",
          value_basis: "project_fee",
        }).value_amount,
      ).toBe(sent);
    expect(
      formInput("leads.opportunity.update", base, { title: "Renamed" }),
    ).toEqual({ ref: "r", revision: 3, title: "Renamed" });
  });
  it("retains the rendered revision without coercing a form value", () =>
    expect(
      formInput(
        "leads.opportunity.add_note",
        { ref: "r", revision: 2 },
        { body: "Note" },
      ),
    ).toEqual({ ref: "r", revision: 2, body: "Note" }));
  it("retains the routing compare-and-swap snapshot", () =>
    expect(
      formInput(
        "leads.connection.set_routing",
        { connection_id: "c", expected_rule_ids: ["old"] },
        { pipeline_id: "p" },
      ),
    ).toEqual({
      connection_id: "c",
      expected_rule_ids: ["old"],
      rules: [{ action: "create_opportunity", pipeline_id: "p" }],
    }));
  it("submits a human assessment with separate ratings while retaining revision", () => {
    const values = {
      objective: "Service fit",
      fit: "high",
      intent: "medium",
      urgency: "needs_information",
      evidence_completeness: "low",
      uncertainty: "high",
      explanation: "Budget is unknown.",
    };
    const { objective, ...assessment } = values;
    expect(
      formInput(
        "leads.qualification.assess",
        { ref: "r", revision: 2 },
        values,
      ),
    ).toEqual({
      ref: "r",
      revision: 2,
      objective,
      assessment: { ...assessment, author: "human" },
    });
    expect(
      formInput(
        "leads.qualification.assess",
        { ref: "r", revision: 2 },
        { objective },
      ),
    ).toEqual({ ref: "r", revision: 2, objective });
  });
  it("bounds pagination input", () => {
    expect(pageOffset("NaN")).toBe(0);
    expect(pageOffset("-25")).toBe(0);
    expect(pageOffset("100001")).toBe(0);
    expect(pageOffset("25")).toBe(25);
  });
});

describe("dated follow-ups", () => {
  it.each(["list", "board"] as const)(
    "%s sends an explicit cutoff and displays the next action",
    async (name) => {
      const api = shell({
        "leads.ui.catalog": catalog,
        "leads.opportunity.list": ok({
          items: [
            {
              ...opportunity,
              followup_due_on: "2026-10-15",
              followup_action: "Confirm scope",
            },
          ],
        }),
      });
      const html = renderToStaticMarkup(
        await screens[name]({ shell: api, query: { due: "2026-10-15" } }),
      );
      expect(api.call).toHaveBeenCalledWith(
        "leads.opportunity.list",
        expect.objectContaining({ followup_due_by: "2026-10-15" }),
      );
      expect(html).toContain("Confirm scope");
      expect(html).toContain('type="date"');
    },
  );
  it("offers rescheduling, completion and cancellation with escaped history", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 4,
        data: {
          ...opportunity,
          followup: {
            id: "reminder",
            action: "Confirm scope",
            due_on: "2026-10-15",
          },
          followup_history: [
            {
              id: "old",
              action: "<script>Earlier action</script>",
              state: "superseded",
              due_on: "2026-10-12",
            },
          ],
        },
      }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Reschedule follow-up");
    expect(html).toContain("Mark follow-up complete");
    expect(html).toContain("Cancel follow-up");
    expect(html).toContain('value="2026-10-15"');
    expect(html).toContain("&lt;script&gt;Earlier action&lt;/script&gt;");
    expect(api.submit).not.toHaveBeenCalled();
    expect(
      formInput(
        "leads.followup.resolve",
        {
          ref: opportunity.ref,
          revision: 4,
          reminder_id: "reminder",
          outcome: "completed",
        },
        {},
      ),
    ).toEqual({
      ref: opportunity.ref,
      revision: 4,
      reminder_id: "reminder",
      outcome: "completed",
    });
  });
  it("does not offer a new reminder for a closed opportunity", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 4,
        data: { ...opportunity, disposition_outcome: "lost" },
      }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Opportunity closed.");
    expect(html).not.toContain("Schedule follow-up");
  });
});
describe("phone display", () => {
  it("groups stored North American digits and leaves others as stored", () => {
    const digits = ["250", "555", "0142"];
    expect(phoneDisplay(digits.join(""))).toBe(digits.join("-"));
    expect(phoneDisplay(`+1${digits.join("")}`)).toBe(`+1 ${digits.join("-")}`);
    expect(phoneDisplay("+4420")).toBe("+4420");
    expect(phoneDisplay("")).toBe("");
  });
});
describe("assessment staleness", () => {
  it("follows the evidence digest, and the revision only for older assessments", () => {
    const assessed = { input_revision: 1, evidence_digest: "a" };
    expect(assessmentStale(assessed, { evidence_digest: "a" }, 4)).toBe(false);
    expect(assessmentStale(assessed, { evidence_digest: "b" }, 1)).toBe(true);
    const older = { input_revision: 1, evidence_digest: null };
    expect(assessmentStale(older, { evidence_digest: "a" }, 1)).toBe(false);
    expect(assessmentStale(older, { evidence_digest: "a" }, 2)).toBe(true);
  });
});
describe("opportunity value", () => {
  it("formats a recorded value and says when none is set", () => {
    expect(value({ value_amount: null })).toBe("Not set");
    expect(
      value({
        value_amount: "10000",
        value_currency: "CAD",
        value_basis: "project_fee",
      }),
    ).toMatch(/^CAD\s10,000 · project fee$/);
    expect(
      value({
        value_amount: "85.5",
        value_currency: "CAD",
        value_basis: "hourly_rate",
      }),
    ).toMatch(/^CAD\s85\.50 · hourly rate$/);
  });
});

describe("record fields as a person reads them", () => {
  it("names fields without their storage path", () => {
    expect(fieldLabel("ext.jobsearch.client_hire_rate")).toBe("Client hire rate");
    expect(fieldLabel("ext.jobsearch.posted_on")).toBe("Posted");
    expect(fieldLabel("ext.jobsearch.invited")).toBe("Invited");
    expect(fieldLabel("ext.other.some_new_field")).toBe("Some new field");
    expect(fieldLabel("message")).toBe("Original message");
    expect(fieldLabel("person.email")).toBe("Email");
  });
  it("shows flags, money, rates, dates and enums in words", () => {
    expect(fieldValue("ext.jobsearch.applied", "false")).toBe("No");
    expect(fieldValue("ext.jobsearch.client_payment_verified", "true")).toBe("Yes");
    expect(fieldValue("ext.jobsearch.client_spend", "245384.48")).toBe("$245,384.48");
    expect(fieldValue("ext.jobsearch.client_hire_rate", "0.68")).toBe("68%");
    expect(fieldValue("ext.jobsearch.proposal_observed_at", "2026-10-15T08:13:14.717540+00:00")).toBe(
      "Oct 15, 2026",
    );
    expect(fieldValue("ext.jobsearch.job_type", "hourly")).toBe("Hourly");
    // Date-shaped text outside a date field stays as written.
    expect(fieldValue("subject", "2026-10-15")).toBe("2026-10-15");
    expect(fieldValue("ext.jobsearch.client_rating", "4.84")).toBe("4.84");
    expect(
      fieldValue("message", "<untrusted_participant_content>\nBuild a shop.\n</untrusted_participant_content>"),
    ).toBe("Build a shop.");
  });
});

describe("Leads to Current detail integration", () => {
  const qualification = {
    id: "qualification-current",
    objective: "Ready for delivery",
    input_revision: 3,
    evidence_digest: "digest-current",
    fit: "low",
    intent: "needs_information",
    urgency: "medium",
    evidence_completeness: "high",
    uncertainty: "low",
    explanation: "The evidence is sufficient for a human handoff decision.",
  };
  const currentHandoff = {
    ref: "leads.handoff:019bf0b0-0000-7000-8000-000000000004",
    opportunity_ref: opportunity.ref,
    purpose: "internal_analysis",
    state: "succeeded",
    destination_ref: "current",
    result_ref: "current.item:019bf0b0-0000-7000-8000-000000000005",
    result_detail: null,
    approval_id: "019bf0b0-0000-7000-8000-000000000007",
    attempt_count: 1,
    requested_at: "2026-10-25T12:00:00Z",
    resolved_at: "2026-10-25T12:05:00Z",
  };
  const pendingHandoff = {
    ...currentHandoff,
    state: "pending",
    action_phase: "awaiting_approval",
    result_ref: null,
    result_detail: null,
    resolved_at: null,
  };

  it("qualified-current enables handoff regardless of qualification score", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: {
          ...opportunity,
          evidence_digest: "digest-current",
          stage: { label: "Triage" },
          qualification,
        },
      }),
      "leads.qualification.list": ok({ items: [qualification] }),
      "leads.handoff.list": ok({ items: [] }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Hand off to Current");
    expect(html).toContain("requires separate human approval");
    expect(html).toContain("No Current handoffs yet.");
  });

  it("requires a current qualification and renders no disabled facsimile", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: { ...opportunity, stage: { label: "Triage" } },
      }),
      "leads.qualification.list": ok({ items: [] }),
      "leads.handoff.list": ok({ items: [] }),
    });
    const html = await renderDetail(api);
    expect(html).not.toContain("Hand off to Current");
    expect(html).not.toContain("Qualification may be out of date");
  });

  it("qualified-stale warns conspicuously without disabling handoff", async () => {
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 4,
        data: {
          ...opportunity,
          evidence_digest: "digest-new",
          stage: { label: "Triage" },
          qualification,
        },
      }),
      "leads.qualification.list": ok({ items: [qualification] }),
      "leads.handoff.list": ok({ items: [] }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Qualification may be out of date");
    expect(html).toContain(
      "Evidence changed after this qualification. Review or reassess it before handing off if the changes matter.",
    );
    expect(html).toContain("Hand off to Current");
    expect(html).not.toMatch(/<fieldset[^>]*disabled/);
  });

  it("keeps the generic stale warning immediately before a staged durable summary", async () => {
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 4,
          data: {
            ...opportunity,
            evidence_digest: "digest-new",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
        "leads.handoff.list": ok({ items: [pendingHandoff] }),
      },
      "owner",
      { approvalHref: () => "/shell-owned/review" },
    );
    const html = await renderDetail(api);
    const warning = html.indexOf("Qualification may be out of date");
    const summary = html.indexOf('aria-label="Current handoff status"');
    expect(warning).toBeGreaterThanOrEqual(0);
    expect(summary).toBeGreaterThan(warning);
    expect(html.slice(warning, summary)).toContain(
      "Evidence changed after this qualification. Review or reassess it before handing off if the changes matter.",
    );
    expect(html).not.toContain("Hand off to Current");
  });

  it("places job handoff after the verdict and before a loaded proposal workflow", async () => {
    const job = {
      ...opportunity,
      preset_id: "019bf0b0-0000-7000-8000-000000000002",
      revision: 3,
      stage_label: "Triage",
      stage_kind: "open",
      assessment: { summary: "Qualified for review", mark: "B" },
      fields: {},
    };
    const proposal = {
      opportunity_ref: opportunity.ref,
      state: "ready",
      questions: "What is your delivery approach?",
      sections: [
        {
          id: "section-cover",
          position: 0,
          kind: "cover_letter",
          question: null,
          body: "A deliberately long proposal body follows the qualification verdict without displacing the handoff decision.",
          library_answer_id: null,
          library_match_id: null,
          edited: false,
        },
      ],
      draft:
        "A deliberately long proposal body follows the qualification verdict without displacing the handoff decision.\n\nIt continues with enough material to represent the real loaded workflow.",
      findings: {
        accuracy: [],
        accuracy_checked: true,
        flagged_pre_retry: [],
        retried: false,
        style: [],
        style_pre_repair: [],
        style_repaired: false,
        analysis: null,
      },
    };
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: {
          ...job,
          evidence_digest: "digest-current",
          stage: { label: "Triage" },
          qualification,
        },
      }),
      "leads.qualification.list": ok({ items: [qualification] }),
      "leads.ui.queue": ok({ items: [job] }),
      "leads.jobproposal.get": ok(proposal),
      "leads.handoff.list": ok({ items: [] }),
    });
    const html = await renderDetail(api);
    expect(html).toContain("Hand off to Current");
    expect(html).toContain("Current handoffs");
    const verdict = html.indexOf("Qualified for review");
    const handoff = html.indexOf("Qualification handoff");
    const workflow = html.indexOf('aria-label="Proposal workflow"');
    const notes = html.indexOf('aria-label="Notes and follow-up"');
    expect(verdict).toBeGreaterThanOrEqual(0);
    expect(handoff).toBeGreaterThan(verdict);
    expect(workflow).toBeGreaterThanOrEqual(0);
    expect(notes).toBeGreaterThan(workflow);
    expect(html.indexOf("Current handoffs")).toBeGreaterThan(
      notes,
    );
  });

  it("keeps the stale warning and durable job summary beside the verdict before a loaded proposal", async () => {
    const job = {
      ...opportunity,
      preset_id: "019bf0b0-0000-7000-8000-000000000002",
      revision: 3,
      stage_label: "Triage",
      stage_kind: "open",
      assessment: { summary: "Qualified for review", mark: "B" },
      fields: {},
    };
    const proposal = {
      state: "ready",
      questions: "",
      sections: [
        {
          id: "section-cover",
          position: 0,
          kind: "cover_letter",
          question: null,
          body: "Long proposal content after the durable handoff summary.",
          library_answer_id: null,
          library_match_id: null,
          edited: false,
        },
      ],
      draft: "Long proposal content after the durable handoff summary.",
      findings: {},
    };
    const approvalHref = vi.fn(() => "/shell-owned/review");
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 4,
          data: {
            ...job,
            evidence_digest: "digest-new",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
        "leads.ui.queue": ok({ items: [job] }),
        "leads.jobproposal.get": ok(proposal),
        "leads.handoff.list": ok({ items: [pendingHandoff] }),
      },
      "owner",
      { approvalHref },
    );
    const html = await renderDetail(api);
    const verdict = html.indexOf("Qualified for review");
    const warning = html.indexOf("Qualification may be out of date");
    const summary = html.indexOf('aria-label="Current handoff status"');
    const workflow = html.indexOf('aria-label="Proposal workflow"');
    expect(warning).toBeGreaterThan(verdict);
    expect(summary).toBeGreaterThan(warning);
    expect(workflow).toBeGreaterThan(summary);
    expect(html).toContain("Awaiting approval");
    expect(html).toContain('href="/shell-owned/review"');
    expect(api.call.mock.calls.filter(([name]) => name === "leads.handoff.list"))
      .toHaveLength(1);
  });

  const pagedHandoffs = Array.from({ length: 205 }, (_, index) => ({
    ...pendingHandoff,
    ref: `leads.handoff:019bf0b0-0000-7000-8000-${index.toString(16).padStart(12, "0")}`,
    action_phase: index === 0 ? "awaiting_approval" : index < 100 ? "approved_queued" : "executing",
    requested_at: new Date(Date.UTC(2026, 9, 30, 12, 0, 0) - index * 60_000).toISOString(),
  }));

  function pagedDetailShell() {
    const href = vi.fn(
      (id: string, query?: Record<string, string | undefined>) =>
        `/leads/${id}?${new URLSearchParams(
          Object.entries(query ?? {}).filter(
            (entry): entry is [string, string] => entry[1] !== undefined,
          ),
        )}`,
    );
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 3,
          data: {
            ...opportunity,
            evidence_digest: "digest-current",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
      },
      "owner",
      { href, approvalHref: () => "/shell-owned/review" },
    );
    api.call.mockImplementation(async (name: string, input?: unknown) => {
      if (name === "leads.handoff.list") {
        const request = input as { offset?: number; limit?: number };
        const offset = request.offset ?? 0;
        const limit = request.limit ?? 100;
        const page = pagedHandoffs.slice(offset, offset + limit);
        return ok({
          items: page,
          next_offset:
            offset + limit < pagedHandoffs.length ? offset + limit : null,
        });
      }
      return (
        {
          "leads.opportunity.get": ok({
            ref: opportunity.ref,
            revision: 3,
            data: {
              ...opportunity,
              evidence_digest: "digest-current",
              stage: { label: "Triage" },
              qualification,
            },
          }),
          "leads.qualification.list": ok({ items: [qualification] }),
        }[name] ?? { state: "unavailable" }
      ) as OperationOutcome;
    });
    return { api, href };
  }

  it("reuses the offset-zero page for newest summary and first 100 history rows", async () => {
    const { api } = pagedDetailShell();
    const html = await renderDetail(api);
    const listCalls = api.call.mock.calls.filter(
      ([name]) => name === "leads.handoff.list",
    );
    expect(listCalls).toEqual([
      ["leads.handoff.list", { ref: opportunity.ref, limit: 100, offset: 0 }],
    ]);
    expect(html).toContain("Awaiting approval");
    expect(html).toContain("Next");
    expect(html).not.toContain(">Previous<");
  });

  it("pages the middle while keeping the newest offset-zero summary", async () => {
    const { api, href } = pagedDetailShell();
    const html = await renderDetail(api, { handoff_offset: "100" });
    const listCalls = api.call.mock.calls.filter(
      ([name]) => name === "leads.handoff.list",
    );
    expect(listCalls).toHaveLength(2);
    expect(listCalls).toEqual(
      expect.arrayContaining([
        ["leads.handoff.list", { ref: opportunity.ref, limit: 1, offset: 0 }],
        ["leads.handoff.list", { ref: opportunity.ref, limit: 100, offset: 100 }],
      ]),
    );
    expect(html).toContain("Awaiting approval");
    expect(html).toContain("Handoff in progress");
    expect(href).toHaveBeenCalledWith("detail", {
      ref: opportunity.ref,
      handoff_offset: "0",
    });
    expect(href).toHaveBeenCalledWith("detail", {
      ref: opportunity.ref,
      handoff_offset: "200",
    });
    expect(html).toContain("Previous");
    expect(html).toContain("Next");
  });

  it.each([
    ["final", "200", true],
    ["empty", "300", true],
  ])("renders the %s older page without fabricating Next", async (_fixture, offset, previous) => {
    const { api } = pagedDetailShell();
    const html = await renderDetail(api, { handoff_offset: offset });
    expect(html).toContain("Awaiting approval");
    expect(html.includes(">Previous<")).toBe(previous);
    expect(html).not.toContain(">Next<");
    if (offset === "300") expect(html).toContain("No Current handoffs yet.");
  });

  it.each(["-1", "invalid", "1.5"])(
    "treats invalid handoff offset %s as offset zero",
    async (offset) => {
      const { api } = pagedDetailShell();
      const html = await renderDetail(api, { handoff_offset: offset });
      expect(api.call.mock.calls).toContainEqual([
        "leads.handoff.list",
        { ref: opportunity.ref, limit: 100, offset: 0 },
      ]);
      expect(html).not.toContain(">Previous<");
    },
  );

  it("uses only the Leads aggregate for linked Current display", async () => {
    const recordHref = vi.fn(() => null);
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 3,
          data: {
            ...opportunity,
            evidence_digest: "digest-current",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
        "leads.handoff.list": ok({ items: [currentHandoff] }),
        "leads.handoff.current_view": ok({
          history: currentHandoff,
          item: {
            state: "live",
            ref: currentHandoff.result_ref,
            content: {
              title: "Prepare Northwind onboarding workshop",
              body: "Confirm agenda and access requirements.",
              status: "open",
              priority: "normal",
              area: "client_delivery",
              due_on: null,
            },
          },
          project: { state: "never_assigned", message: "raw" },
        }),
      },
      "owner",
      { recordHref },
    );
    const html = await renderDetail(api);
    expect(api.call).toHaveBeenCalledWith("leads.handoff.current_view", {
      handoff_ref: currentHandoff.ref,
    });
    expect(api.call.mock.calls.some(([name]) => name.startsWith("current."))).toBe(
      false,
    );
    const qualificationHeading = html.indexOf(">Qualification</h2>");
    const compactSummary = html.indexOf('aria-label="Current handoff status"');
    expect(compactSummary).toBeGreaterThan(qualificationHeading);
    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain("Open in Current unavailable.");
    expect(api.call.mock.calls.filter(([name]) => name === "leads.handoff.list"))
      .toHaveLength(1);
  });

  it("keeps a live project beside a denied item through the real detail read seam", async () => {
    const projectRef =
      "current.project:019bf0b0-0000-7000-8000-000000000006";
    const recordHref = vi.fn((ref: string) => `/configured/${ref}`);
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 3,
          data: {
            ...opportunity,
            evidence_digest: "digest-current",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
        "leads.handoff.list": ok({ items: [currentHandoff] }),
        "leads.handoff.current_view": ok({
          history: currentHandoff,
          item: {
            state: "denied",
            ref: currentHandoff.result_ref,
            title: "must not leak",
          },
          project: {
            state: "live",
            ref: projectRef,
            content: {
              title: "Northwind launch",
              description: "Lasting delivery context.",
              status: "active",
              area: "client_delivery",
              due_on: null,
            },
          },
        }),
      },
      "owner",
      { recordHref },
    );
    const html = await renderDetail(api);
    expect(html).toContain("You do not have access to this Current item.");
    expect(html).toContain("Northwind launch");
    expect(html).not.toContain("must not leak");
    expect(html).not.toContain(currentHandoff.result_ref);
    expect(recordHref).toHaveBeenCalledTimes(1);
    expect(recordHref).toHaveBeenCalledWith(projectRef);
  });

  it.each([
    [
      "refused",
      {
        state: "refused",
        code: "role_not_permitted",
        text: `raw ${currentHandoff.result_ref} must not leak`,
      } as OperationOutcome,
      "Current details unavailable.",
    ],
    [
      "unavailable",
      { state: "unavailable" } as OperationOutcome,
      "Current details are temporarily unavailable.",
    ],
  ])("renders the real %s current_view outcome fail-closed", async (_state, view, copy) => {
    const recordHref = vi.fn(() => "/must-not-be-used");
    const api = shell(
      {
        "leads.opportunity.get": ok({
          ref: opportunity.ref,
          revision: 3,
          data: {
            ...opportunity,
            evidence_digest: "digest-current",
            stage: { label: "Triage" },
            qualification,
          },
        }),
        "leads.qualification.list": ok({ items: [qualification] }),
        "leads.handoff.list": ok({ items: [currentHandoff] }),
        "leads.handoff.current_view": view,
      },
      "owner",
      { recordHref },
    );
    const html = await renderDetail(api);
    expect(html).toContain(copy);
    expect(html).not.toContain("must not leak");
    expect(html).not.toContain(currentHandoff.result_ref);
    expect(html).not.toContain("Current details are still updating.");
    expect(recordHref).not.toHaveBeenCalled();
  });

  it("renders the real detail loading fallback before handoff history settles", async () => {
    let release!: (outcome: OperationOutcome) => void;
    const pending = new Promise<OperationOutcome>((resolve) => {
      release = resolve;
    });
    const api = shell({
      "leads.opportunity.get": ok({
        ref: opportunity.ref,
        revision: 3,
        data: {
          ...opportunity,
          evidence_digest: "digest-current",
          stage: { label: "Triage" },
          qualification,
        },
      }),
      "leads.qualification.list": ok({ items: [qualification] }),
    });
    api.call.mockImplementation(async (name: string) => {
      if (name === "leads.handoff.list") return pending;
      return (
        {
          "leads.opportunity.get": ok({
            ref: opportunity.ref,
            revision: 3,
            data: {
              ...opportunity,
              evidence_digest: "digest-current",
              stage: { label: "Triage" },
              qualification,
            },
          }),
          "leads.qualification.list": ok({ items: [qualification] }),
        }[name] ?? { state: "unavailable" }
      ) as OperationOutcome;
    });
    const detail = screens.detail({ shell: api, query: { ref: opportunity.ref } });
    const raced = await Promise.race([
      detail.then((node) => ({ state: "ready" as const, node })),
      new Promise<{ state: "blocked" }>((resolve) =>
        setTimeout(() => resolve({ state: "blocked" }), 50),
      ),
    ]);
    if (raced.state === "blocked") {
      release(ok({ items: [] }));
      await detail;
    }
    expect(raced.state).toBe("ready");
    if (raced.state === "ready") {
      const html = renderToStaticMarkup(raced.node);
      expect(html).toContain("Loading Current handoffs…");
      release(ok({ items: [] }));
    }
  });

  it("keeps the closed read parser bounded to Leads-owned handoff operations", async () => {
    const api = shell({
      "leads.handoff.list": ok({ items: [] }),
      "leads.handoff.current_view": ok({}),
    });
    await expect(
      read(api, "leads.handoff.list", { ref: opportunity.ref }),
    ).resolves.toEqual(ok({ items: [] }));
    await expect(
      read(api, "leads.handoff.current_view", { handoff_ref: currentHandoff.ref }),
    ).resolves.toEqual(ok({}));
    await expect(
      read(api, "current.item.get" as never, { ref: currentHandoff.result_ref }),
    ).resolves.toEqual({ state: "unavailable" });
  });
});

