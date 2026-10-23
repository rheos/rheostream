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

const DISMISSED =
  "/leads/?pipeline=jobs&dismissed=leads.opportunity%3A01a133b6-b93c-73c0-975f-cf6437992a85&from=review";

/** The same job after the dismiss: closed, with only the way back. */
const passed: Row = {
  ...job,
  stage_id: "passed",
  stage_label: "Passed",
  stage_kind: "terminal",
  revision: 4,
  transitions: [{ stage_id: "review", label: "Review", kind: "open" }],
  dismiss: null,
};

let host: HTMLDivElement;
let root: Root;
const assign = vi.fn();
const replace = vi.fn();
const reload = vi.fn();
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  assign.mockReset();
  replace.mockReset();
  reload.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign, replace, reload },
  });
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(submit: ReturnType<typeof vi.fn>, item: Row = job) {
  const send = submit as unknown as NonNullable<ShellApi["submit"]>;
  act(() =>
    root.render(
      <JobHeader item={item} now={0} submit={send} back="/leads/?pipeline=jobs" />,
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
    // The queue gets the job and the stage it left, so it can offer Undo.
    expect(replace).toHaveBeenCalledWith(DISMISSED);
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
    expect(replace).not.toHaveBeenCalled();
  });
  it("treats the dismiss stage chosen in the select as a dismiss", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit);
    const select = host.querySelector<HTMLSelectElement>("select") as HTMLSelectElement;
    await act(async () => {
      select.value = "passed";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(replace).toHaveBeenCalledWith(DISMISSED);
  });
  it("holds the controls while a move is in flight", async () => {
    let settle: (value: unknown) => void = () => {};
    const submit = vi.fn(() => new Promise((resolve) => (settle = resolve)));
    mount(submit);
    await act(async () => dismissButton().click());
    expect(dismissButton().disabled).toBe(true);
    expect(host.querySelector("select")?.disabled).toBe(true);
    await act(async () => settle({ state: "unavailable" }));
    expect(dismissButton().disabled).toBe(false);
  });
  it("says why a move failed and stays put", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "record_stale", text: "" });
    mount(submit);
    await act(async () => dismissButton().click());
    expect(host.textContent).toContain("Reload to change its stage.");
    expect(replace).not.toHaveBeenCalled();
    expect(dismissButton().disabled).toBe(false);
  });
  it("reopens a closed job in the select's place and stays on the page", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 5 } });
    mount(submit, passed);
    expect(host.querySelector("select")).toBeNull();
    expect(host.querySelector('button[aria-label^="Dismiss"]')).toBeNull();
    // The closed stage still shows beside Reopen.
    expect(host.textContent).toContain("Passed");
    const reopen = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Reopen Example data service (move to Review)"]',
    );
    if (!reopen) throw new Error("no reopen");
    await act(async () => reopen.click());
    expect(submit).toHaveBeenCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 4,
      to_stage_id: "review",
    });
    expect(reload).toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });
});
