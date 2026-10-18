// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import type { Row } from "./data";
import { holdsOf } from "./queue";
import { ReviewQueue } from "./review-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function held(id: string, title: string, hold: Row[]): Row {
  return {
    id,
    ref: `leads.opportunity:${id}`,
    title,
    preset_id: "019bf0b0-0000-7000-8000-000000000002",
    stage_id: "new",
    stage_label: "New",
    stage_kind: "open",
    revision: 2,
    created_at: "2026-10-18T08:00:00Z",
    fields: {
      posting_url: "https://jobs.example.com/postings/1",
      budget_max: "900",
      job_type: "fixed",
      proposal_count: "80",
    },
    transitions: [{ stage_id: "passed", label: "Passed", kind: "terminal" }],
    dismiss: { stage_id: "passed", label: "Passed" },
    hold,
  };
}
const CROWDED = held("01a133b6-b93c-73c0-975f-cf6437992a85", "Example crowded app", [
  { code: "high_proposals", text: "More than 50 proposals already." },
]);
const THIN = held("01a133b6-b93c-73c0-975f-cf6437992a86", "Example thin client", [
  { code: "low_spend", text: "The client has spent 120, below the 1,000 minimum." },
  { code: "low_rating", text: "The client's rating is 3.1, below 4.5." },
]);

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(submit?: ReturnType<typeof vi.fn>, items = [CROWDED, THIN]) {
  const send = submit as unknown as ShellApi["submit"];
  act(() =>
    root.render(
      <ReviewQueue items={items} now={0} hrefs={{}} limited={false} canPromote submit={send} />,
    ),
  );
}
function button(prefix: string, title: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(
    `button[aria-label^="${prefix} ${title}"]`,
  );
  if (!found) throw new Error(`no ${prefix} for ${title}`);
  return found;
}

describe("the review queue", () => {
  it("names each clause that held a job, as a badge and in words", () => {
    expect(holdsOf(THIN).map((h) => h.label)).toEqual([
      "Low client spend",
      "Low client rating",
    ]);
    expect(holdsOf({ hold: [{ code: "other", text: "x" }] })[0]?.label).toBe(
      "Held by the filters",
    );
    const html = renderToStaticMarkup(
      <ReviewQueue items={[THIN]} now={0} hrefs={{}} limited={false} canPromote />,
    );
    expect(html).toContain("Low client spend");
    expect(html).toContain("below 4.5.");
    expect(html).toContain("1 held job");
    // Without a submit there is nothing to press, but the facts still show.
    expect(html).not.toContain('aria-label="Promote');
    expect(html).toContain("Copy opportunity ID");
  });
  it("leaves Promote to owners; a member can still dismiss", () => {
    const submit = vi.fn() as unknown as ShellApi["submit"];
    const html = renderToStaticMarkup(
      <ReviewQueue items={[THIN]} now={0} hrefs={{}} limited={false} canPromote={false} submit={submit} />,
    );
    expect(html).not.toContain('aria-label="Promote');
    expect(html).toContain('aria-label="Dismiss Example thin client');
    expect(html).toContain("A workspace owner can promote one");
  });
  it("shows the empty state when nothing is held", () => {
    const html = renderToStaticMarkup(
      <ReviewQueue items={[]} now={0} hrefs={{}} limited={false} canPromote={false} />,
    );
    expect(html).toContain("Nothing is held back right now.");
  });
  it("promotes a job and takes its card off the list", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit);
    await act(async () => button("Promote", "Example crowded app").click());
    expect(submit).toHaveBeenCalledWith("leads.jobreview.promote", {
      ref: CROWDED.ref,
    });
    expect(host.querySelectorAll("article")).toHaveLength(1);
    expect(host.querySelector('[role="status"]')?.textContent).toContain(
      "Example crowded app is promoted",
    );
    expect(host.textContent).toContain("1 held job");
    expect(host.textContent).toContain("Example thin client");
  });
  it("dismisses through the transition, as the queue does", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 3 } });
    mount(submit);
    await act(async () => button("Dismiss", "Example thin client").click());
    expect(submit).toHaveBeenCalledWith("leads.opportunity.transition", {
      ref: THIN.ref,
      revision: 2,
      to_stage_id: "passed",
    });
    expect(host.textContent).toContain("1 held job");
  });
  it("keeps the card and says why when the job is no longer held", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "not_held", text: "" });
    mount(submit);
    await act(async () => button("Promote", "Example crowded app").click());
    expect(host.textContent).toContain("The filters no longer hold this job.");
    expect(host.textContent).toContain("2 held jobs");
    expect(button("Promote", "Example crowded app").disabled).toBe(false);
  });
  it("holds a card's controls while its action is in flight", async () => {
    let settle: (value: unknown) => void = () => {};
    const submit = vi.fn(() => new Promise((resolve) => (settle = resolve)));
    mount(submit);
    await act(async () => button("Promote", "Example crowded app").click());
    expect(button("Promote", "Example crowded app").disabled).toBe(true);
    expect(button("Dismiss", "Example crowded app").disabled).toBe(true);
    expect(button("Promote", "Example thin client").disabled).toBe(false);
    await act(async () => settle({ state: "unavailable" }));
    expect(host.textContent).toContain("Could not promote it. Try again.");
  });
});
