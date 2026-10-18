// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import type { Row } from "./data";
import { JobHeader } from "./queue-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const job: Row = {
  id: "01a133b6-b93c-73c0-975f-cf6437992a85",
  ref: "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a85",
  title: "Example data service",
  stage_id: "review",
  stage_label: "Review",
  stage_kind: "open",
  revision: 3,
  fields: { posting_url: "https://jobs.example.com/postings/1" },
  transitions: [
    { stage_id: "qualified", label: "Qualified", kind: "open" },
    { stage_id: "passed", label: "Passed", kind: "terminal" },
  ],
  dismiss: { stage_id: "passed", label: "Passed" },
};

let host: HTMLDivElement;
let root: Root;
const assign = vi.fn();
const reload = vi.fn();
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  assign.mockReset();
  reload.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign, reload },
  });
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(submit: ReturnType<typeof vi.fn>) {
  const send = submit as unknown as NonNullable<ShellApi["submit"]>;
  act(() =>
    root.render(
      <JobHeader item={job} now={0} submit={send} back="/leads/?pipeline=jobs" />,
    ),
  );
}
function dismissButton(): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>('button[aria-label^="Dismiss"]');
  if (!found) throw new Error("no dismiss");
  return found;
}

describe("the job header's controls", () => {
  it("dismisses through the transition and returns to the queue", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 4 } });
    mount(submit);
    await act(async () => dismissButton().click());
    expect(submit).toHaveBeenCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 3,
      to_stage_id: "passed",
    });
    expect(assign).toHaveBeenCalledWith("/leads/?pipeline=jobs");
    expect(reload).not.toHaveBeenCalled();
  });
  it("reloads after a stage change from the select", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 4 } });
    mount(submit);
    const select = host.querySelector<HTMLSelectElement>("select");
    if (!select) throw new Error("no select");
    await act(async () => {
      select.value = "qualified";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(submit.mock.calls[0]?.[1]).toMatchObject({ to_stage_id: "qualified" });
    expect(reload).toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
  });
  it("says why a move failed and stays put", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "record_stale", text: "" });
    mount(submit);
    await act(async () => dismissButton().click());
    expect(host.textContent).toContain("Reload to change its stage.");
    expect(assign).not.toHaveBeenCalled();
    expect(dismissButton().disabled).toBe(false);
  });
});
