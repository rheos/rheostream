import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { formInput } from "./action-form";
import { pageOffset } from "./data";
import { assessmentStale, phoneDisplay, value } from "./screens";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const opportunity = {
  id: "sample",
  ref: "leads.opportunity:sample",
  title: "Example studio inquiry",
  stage_id: "triage",
  pipeline_id: "pipeline",
  updated_at: "2026-01-01T00:00:00Z",
};
function shell(answers: Record<string, OperationOutcome>, role = "owner") {
  const call = vi.fn(
    async (name: string) =>
      answers[name] ?? ({ state: "unavailable" } as OperationOutcome),
  );
  const submit = vi.fn(async () => ok({}));
  return {
    role,
    call,
    submit,
    href: (id: string, query?: Record<string, string | undefined>) =>
      `/leads/${id}?${new URLSearchParams(Object.entries(query ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined))}`,
  } satisfies ShellApi;
}
const catalog = ok({
  items: [
    {
      kind: "pipeline",
      id: "pipeline",
      name: "Service inquiries",
      stages: [{ stage_id: "triage", label: "Triage" }],
    },
    { kind: "funnel", name: "General inquiries", ref: "leads.funnel:sample" },
  ],
});
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
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: opportunity.ref } }),
    );
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
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: opportunity.ref } }),
    );
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
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: opportunity.ref } }),
    );
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
    const html = renderToStaticMarkup(
      await screens.detail({ shell: api, query: { ref: opportunity.ref } }),
    );
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
