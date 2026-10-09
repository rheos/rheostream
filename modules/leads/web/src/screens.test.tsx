import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";
import { formInput } from "./action-form";
import { pageOffset } from "./data";

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
    const html = renderToStaticMarkup(await screens.capture({ shell: api, query: {} }));
    expect(html).toContain("Paste an email, message, or conversation");
    expect(html).toContain("Review inquiry");
    expect(html).toMatch(/<details[^>]*><summary>Enter details manually instead/);
    expect(html).not.toMatch(/<details[^>]*open[^>]*><summary>Enter details manually instead/);
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
  it("bounds pagination input", () => {
    expect(pageOffset("NaN")).toBe(0);
    expect(pageOffset("-25")).toBe(0);
    expect(pageOffset("100001")).toBe(0);
    expect(pageOffset("25")).toBe(25);
  });
});
