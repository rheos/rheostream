import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { CurrentHandoffHistory } from "./handoff";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const handoffRef =
  "leads.handoff:019bf0b0-0000-7000-8000-000000000004";
const itemRef = "current.item:019bf0b0-0000-7000-8000-000000000005";
const projectRef =
  "current.project:019bf0b0-0000-7000-8000-000000000006";

const history = {
  ref: handoffRef,
  opportunity_ref:
    "leads.opportunity:019bf0b0-0000-7000-8000-000000000003",
  purpose: "internal_analysis",
  state: "succeeded",
  action_phase: null,
  destination_ref: "current",
  result_ref: itemRef,
  result_detail: null,
  approval_id: "019bf0b0-0000-7000-8000-000000000007",
  external_action_id: "019bf0b0-0000-7000-8000-000000000008",
  attempt_count: 1,
  qualification_ref:
    "leads.qualification:019bf0b0-0000-7000-8000-000000000009",
  qualification_stale: false,
  current_project_ref: projectRef,
  last_current_project_ref: null,
  current_project_state: "linked",
  current_link_revision: 2,
  requested_at: "2026-10-25T12:00:00Z",
  resolved_at: "2026-10-25T12:05:00Z",
};

function pending(actionPhase: string | null) {
  return {
    ...history,
    state: "pending",
    action_phase: actionPhase,
    result_ref: null,
    result_detail: null,
    current_project_ref: null,
    current_project_state: "never_assigned",
    current_link_revision: null,
    resolved_at: null,
  };
}
const item = {
  state: "live",
  ref: itemRef,
  content: {
    title: "Prepare Northwind onboarding workshop",
    body: "Confirm agenda, facilitators, and access requirements.",
    status: "open",
    priority: "normal",
    area: "client_delivery",
    due_on: null,
    updated_at: "2026-10-25T12:05:00Z",
  },
};
const project = {
  state: "live",
  ref: projectRef,
  content: {
    title: "Northwind launch",
    description: "Lasting delivery context.",
    status: "active",
    area: "client_delivery",
    due_on: null,
    updated_at: "2026-10-25T12:10:00Z",
  },
};

function shell(overrides: Partial<ShellApi> = {}): ShellApi {
  return {
    role: "owner",
    call: vi.fn(async () => ({ state: "unavailable" }) as OperationOutcome),
    submit: vi.fn(async () => ({ state: "unavailable" }) as OperationOutcome),
    href: () => "/leads/detail",
    recordHref: vi.fn((ref: string) => `/configured/${encodeURIComponent(ref)}`),
    approvalHref: vi.fn(
      (approvalId: string) => `/approval-review/${approvalId}`,
    ),
    ...overrides,
  };
}

function render(
  rows: Record<string, unknown>[],
  views: ReadonlyMap<string, OperationOutcome> = new Map(),
  api = shell(),
) {
  return renderToStaticMarkup(
    <CurrentHandoffHistory
      shell={api}
      result={ok({ items: rows })}
      views={views}
    />,
  );
}

describe("independent linked Current state", () => {
  it("handoff-succeeded-live-item-live-project shows both authorized siblings", () => {
    const html = render(
      [history],
      new Map([[handoffRef, ok({ history, item, project })]]),
    );
    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain("Northwind launch");
    expect(html).toContain("Open in Current");
    expect(html).toContain("Open project in Current");
  });

  it("handoff-deleted-item-live-project keeps the authorized project visible", () => {
    const html = render(
      [history],
      new Map([
        [
          handoffRef,
          ok({ history, item: { state: "deleted" }, project }),
        ],
      ]),
    );
    expect(html).toContain("Current item deleted");
    expect(html).toContain("Northwind launch");
  });

  it("handoff-denied-item-live-project keeps the authorized project visible", () => {
    const recordHref = vi.fn((ref: string) => `/configured/${ref}`);
    const api = shell({ recordHref });
    const html = render(
      [history],
      new Map([
        [
          handoffRef,
          ok({
            history,
            item: {
              state: "denied",
              message: `stale ${itemRef}`,
              title: "must not leak",
              ref: itemRef,
            },
            project,
          }),
        ],
      ]),
      api,
    );

    expect(html).toContain("You do not have access to this Current item.");
    expect(html).toContain("Northwind launch");
    expect(html).toContain("Open project in Current");
    expect(html).not.toContain(itemRef);
    expect(html).not.toContain("must not leak");
    expect(recordHref).toHaveBeenCalledTimes(1);
    expect(recordHref).toHaveBeenCalledWith(projectRef);
  });

  it("handoff-live-item-denied-project keeps the item and redacts the project", () => {
    const recordHref = vi.fn((ref: string) => `/configured/${ref}`);
    const html = render(
      [history],
      new Map([
        [
          handoffRef,
          ok({
            history,
            item,
            project: {
              state: "denied",
              message: `stale ${projectRef}`,
              title: "must not leak",
              ref: projectRef,
            },
          }),
        ],
      ]),
      shell({ recordHref }),
    );

    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain("You do not have access to this Current project.");
    expect(html).not.toContain(projectRef);
    expect(html).not.toContain("must not leak");
    expect(recordHref).toHaveBeenCalledTimes(1);
    expect(recordHref).toHaveBeenCalledWith(itemRef);
  });

  it("configured-only navigation keeps live content when no route is available", () => {
    const recordHref = vi.fn(() => null);
    const html = render(
      [history],
      new Map([[handoffRef, ok({ history, item, project })]]),
      shell({ recordHref }),
    );

    expect(html).toContain("Prepare Northwind onboarding workshop");
    expect(html).toContain("Northwind launch");
    expect(html.match(/Open in Current unavailable\./g)).toHaveLength(2);
    expect(html).not.toContain("href=\"/current");
    expect(recordHref).toHaveBeenCalledTimes(2);
  });
});

describe("handoff history states and actions", () => {
  it("handoff-awaiting-approval uses only the shell approval URL", () => {
    const approvalHref = vi.fn(() => "/shell-owned/review");
    const html = render(
      [pending("awaiting_approval")],
      new Map(),
      shell({ approvalHref }),
    );
    expect(html).toContain("Awaiting approval");
    expect(html).toContain('href="/shell-owned/review"');
    expect(html).toContain("Review approval");
    expect(approvalHref).toHaveBeenCalledWith(history.approval_id);
    expect(html).not.toContain("approvals/detail");
  });

  it.each([
    ["handoff-approved-queued", "approved_queued", "Approved and queued", "Creation is not yet confirmed."],
    ["handoff-executing", "executing", "Handoff in progress", ""],
  ])("%s renders its accepted action phase", (_fixture, phase, heading, body) => {
    const html = render([pending(phase)]);
    expect(html).toContain(heading);
    if (body) expect(html).toContain(body);
    expect(html).not.toContain("Review approval");
    expect(html).not.toContain("Current item created");
  });

  it.each(["unknown", null])(
    "pending %s fails closed without inferring approval or exposing an action",
    (phase) => {
      const approvalHref = vi.fn(() => "/must-not-be-used");
      const html = render(
        [pending(phase)],
        new Map(),
        shell({ approvalHref }),
      );
      expect(html).toContain("Handoff status unavailable");
      expect(html).toContain(
        "Rheo cannot confirm whether approval or creation is in progress. Reload to check again.",
      );
      expect(html).not.toContain("Awaiting approval");
      expect(html).not.toContain("Review approval");
      expect(html).not.toContain(history.approval_id);
      expect(html).not.toContain(history.external_action_id);
      expect(approvalHref).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["handoff-succeeded-live-item-no-project", "never_assigned", "No Current project assigned yet."],
    ["handoff-cleared-project", "cleared", "Project connection cleared in Current."],
    ["handoff-deleted-project", "deleted", "Current project deleted."],
  ])("%s renders the exact project state", (_fixture, state, copy) => {
    const html = render(
      [history],
      new Map([
        [
          handoffRef,
          ok({
            history,
            item,
            project: { state, message: `raw ${projectRef}` },
          }),
        ],
      ]),
    );
    expect(html).toContain(copy);
    expect(html).not.toContain(projectRef);
  });

  it("handoff-unavailable-item-unavailable-project redacts both branches", () => {
    const html = render(
      [history],
      new Map([
        [
          handoffRef,
          ok({
            history,
            item: { state: "unavailable", message: `raw ${itemRef}` },
            project: { state: "unavailable", message: `raw ${projectRef}` },
          }),
        ],
      ]),
    );
    expect(html).toContain("Current item is temporarily unavailable.");
    expect(html).toContain("Current project is temporarily unavailable.");
    expect(html).not.toContain(itemRef);
    expect(html).not.toContain(projectRef);
  });

  it("handoff-unresolved forbids blind retry", () => {
    const html = render([
      { ...history, state: "unresolved", result_detail: "outcome_unknown" },
    ]);
    expect(html).toContain("Outcome unknown");
    expect(html).toContain("Rheo cannot confirm whether the Current effect occurred");
    expect(html).not.toContain("Retry handoff");
  });

  it("handoff-retryable-no-effect offers one explicit new-approval retry", () => {
    const html = render([
      { ...history, state: "failed", result_detail: "retryable_no_effect" },
    ]);
    expect(html).toContain("No Current item was created by this attempt.");
    expect(html).toContain("Retry handoff");
    expect(html).toContain("requires new approval");
  });

  it.each([
    ["failed", "retryable_no_effect"],
    ["refused", "destination_unavailable_no_effect"],
    ["cancelled", null],
    ["unresolved", "outcome_unknown"],
  ])("renders historical %s state without an assertive live region", (state, detail) => {
    const html = render([
      { ...history, state, action_phase: null, result_detail: detail },
    ]);
    expect(html).not.toContain('role="alert"');
    expect(html).not.toContain('role="status"');
  });

  it("renders loading, empty, denied and temporary history states", () => {
    const api = shell();
    const loading = renderToStaticMarkup(
      <CurrentHandoffHistory shell={api} result={null} views={new Map()} />,
    );
    expect(loading).toContain("Loading Current handoffs…");
    expect(render([])).toContain("No Current handoffs yet.");
    const denied = renderToStaticMarkup(
      <CurrentHandoffHistory
        shell={api}
        result={{ state: "refused", code: "role_not_permitted", text: "private" }}
        views={new Map()}
      />,
    );
    expect(denied).toContain("Current handoff history unavailable.");
    expect(denied).not.toContain("private");
    const unavailable = renderToStaticMarkup(
      <CurrentHandoffHistory
        shell={api}
        result={{ state: "unavailable" }}
        views={new Map()}
      />,
    );
    expect(unavailable).toContain(
      "Current handoff history is temporarily unavailable.",
    );
  });
});
