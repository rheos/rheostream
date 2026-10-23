// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import type { Row } from "./data";
import {
  dismissedHref,
  PREFS_KEY,
  reopenFor,
  undoFor,
  undoTarget,
} from "./queue";
import { JobQueue, type QueueProps } from "./queue-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const job: Row = {
  id: "01a133b6-b93c-73c0-975f-cf6437992a85",
  ref: "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a85",
  title: "Example data service",
  pipeline_id: "jobs",
  stage_id: "review",
  stage_label: "Review",
  stage_kind: "open",
  revision: 3,
  fields: {},
  transitions: [
    { stage_id: "qualified", label: "Qualified", kind: "open" },
    { stage_id: "passed", label: "Passed", kind: "terminal" },
  ],
  dismiss: {
    stage_id: "passed",
    label: "Passed",
    undo: { stage_id: "review", label: "Review" },
  },
};
const withFollowup: Row = {
  ...job,
  followup: { due_on: "2026-11-20", action: "Send the draft" },
};
const other: Row = {
  ...job,
  id: "01a133b6-0000-7000-8000-00000000abcd",
  ref: "leads.opportunity:01a133b6-0000-7000-8000-00000000abcd",
  title: "Second example",
};
const closed: Row = {
  ...job,
  stage_id: "passed",
  stage_label: "Passed",
  stage_kind: "terminal",
  revision: 4,
  transitions: [{ stage_id: "review", label: "Review", kind: "open" }],
  dismiss: null,
};
const PAGE = "https://leads.example.com/queue?stage=review";

let host: HTMLDivElement;
let root: Root;
const replace = vi.fn();
const replaceState = vi.fn();
function at(href: string) {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, href, replace },
  });
}
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  replace.mockReset();
  replaceState.mockReset();
  at(PAGE);
  vi.spyOn(window.history, "replaceState").mockImplementation(replaceState);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

function mount(submit: ReturnType<typeof vi.fn>, overrides: Partial<QueueProps> = {}) {
  const props: QueueProps = {
    items: [job, other],
    now: 0,
    pipelines: [{ value: "jobs", label: "Job leads" }],
    stages: [{ value: "review", label: "Review" }],
    pipeline: "jobs",
    stage: "review",
    hideFull: false,
    sort: "score",
    explicit: true,
    filtered: false,
    hiddenOnly: false,
    action: PAGE,
    hrefs: { [String(job.id)]: "/leads/detail?ref=92a85" },
    noun: ["job", "jobs"],
    limited: false,
    nothingYet: <p>Nothing yet.</p>,
    submit: submit as unknown as NonNullable<ShellApi["submit"]>,
    ...overrides,
  };
  act(() => root.render(<JobQueue {...props} />));
}
function button(label: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(`button[aria-label^="${label}"]`);
  if (!found) throw new Error(`no ${label}`);
  return found;
}
const count = () => host.querySelector('[aria-live="polite"]')?.textContent;

describe("the undo rules", () => {
  it("offers an undo only for the card's own dismiss", () => {
    expect(undoFor(job, { stage_id: "passed", label: "Passed", kind: "terminal" })).toEqual(
      { stage_id: "review", label: "Review", kind: "open" },
    );
    expect(undoFor(job, { stage_id: "qualified", label: "Qualified", kind: "open" })).toBeNull();
    expect(undoFor(job, { stage_id: "lost", label: "Lost", kind: "terminal" })).toBeNull();
    const noWayBack = { ...job, dismiss: { stage_id: "passed", label: "Passed", undo: null } };
    expect(undoFor(noWayBack, { stage_id: "passed", label: "Passed", kind: "terminal" })).toBeNull();
  });
  it("reopens only a closed card, to its first open stage", () => {
    expect(reopenFor(job)).toBeNull();
    expect(reopenFor(closed)).toEqual({ stage_id: "review", label: "Review", kind: "open" });
    expect(reopenFor({ ...closed, transitions: [] })).toBeNull();
  });
  it("prefers the stage a dismissed job left, when it may go back there", () => {
    const two = {
      ...closed,
      transitions: [
        { stage_id: "review", label: "Review", kind: "open" },
        { stage_id: "qualified", label: "Qualified", kind: "open" },
      ],
    };
    expect(undoTarget(two, "qualified")?.stage_id).toBe("qualified");
    expect(undoTarget(two, "applied")?.stage_id).toBe("review");
    expect(undoTarget(closed, "")?.stage_id).toBe("review");
  });
  it("adds the dismissed job to the queue address", () => {
    expect(dismissedHref("/leads/", "92a85", "review")).toBe(
      "/leads/?dismissed=92a85&from=review",
    );
    expect(dismissedHref("/leads/?pipeline=jobs", "a b", "review")).toBe(
      "/leads/?pipeline=jobs&dismissed=a+b&from=review",
    );
  });
});

describe("the queue card's Undo", () => {
  it("leaves an Undo line in the card's place, and Undo brings the card back", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "ok", result: { revision: 6 } })
      .mockResolvedValueOnce({ state: "ok", result: { revision: 4 } })
      .mockResolvedValueOnce({ state: "ok", result: { revision: 5 } });
    mount(submit);
    expect(count()).toBe("2 jobs");
    await act(async () => button("Dismiss Example data service").click());
    expect(submit).toHaveBeenLastCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 3,
      to_stage_id: "passed",
    });
    expect(host.textContent).toContain("Dismissed Example data service.");
    expect(host.querySelector('a[href="/leads/detail?ref=92a85"]')).not.toBeNull();
    expect(count()).toBe("1 job");
    // The line sits where the card was: before the second card.
    expect(host.textContent?.indexOf("Dismissed")).toBeLessThan(
      host.textContent?.indexOf("Second example") ?? -1,
    );

    await act(async () => button("Undo: reopen Example data service").click());
    expect(submit).toHaveBeenLastCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 4,
      to_stage_id: "review",
    });
    expect(host.textContent).not.toContain("Dismissed");
    expect(count()).toBe("2 jobs");
    // The card is back with the latest revision: a second dismiss is current.
    await act(async () => button("Dismiss Example data service").click());
    expect(submit).toHaveBeenLastCalledWith(
      "leads.opportunity.transition",
      expect.objectContaining({ revision: 5 }),
    );
  });
  it("shows no follow-up after Undo: the dismiss cancelled it", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce({ state: "ok", result: { revision: 4 } })
      .mockResolvedValueOnce({ state: "ok", result: { revision: 5 } });
    mount(submit, { items: [withFollowup, other] });
    expect(host.textContent).toContain("Send the draft");
    await act(async () => button("Dismiss Example data service").click());
    await act(async () => button("Undo: reopen").click());
    expect(host.textContent).toContain("Example data service");
    expect(host.textContent).not.toContain("Send the draft");
  });
  it("waits for the dismiss before Undo can run", async () => {
    let settle: (value: unknown) => void = () => {};
    const submit = vi.fn(() => new Promise((resolve) => (settle = resolve)));
    mount(submit);
    await act(async () => button("Dismiss Example data service").click());
    expect(button("Undo: reopen").disabled).toBe(true);
    await act(async () => settle({ state: "ok", result: { revision: 4 } }));
    expect(button("Undo: reopen").disabled).toBe(false);
  });
  it("brings the card back with the reason when the dismiss fails", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "record_stale", text: "" });
    mount(submit);
    await act(async () => button("Dismiss Example data service").click());
    expect(host.textContent).not.toContain("Dismissed");
    expect(host.textContent).toContain("Reload to change its stage.");
  });
  it("keeps the Undo line and says so when the undo fails", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce({ state: "ok", result: { revision: 4 } })
      .mockResolvedValueOnce({ state: "unavailable" });
    mount(submit);
    await act(async () => button("Dismiss Example data service").click());
    await act(async () => button("Undo: reopen").click());
    expect(host.textContent).toContain("Dismissed Example data service.");
    expect(host.textContent).toContain("Could not undo. Try again, or reopen it from Passed.");
    expect(button("Undo: reopen").disabled).toBe(false);
  });
  it("sends one undo for a double click", async () => {
    let settle: (value: unknown) => void = () => {};
    const submit = vi
      .fn()
      .mockResolvedValueOnce({ state: "ok", result: { revision: 4 } })
      .mockImplementation(() => new Promise((resolve) => (settle = resolve)));
    mount(submit);
    await act(async () => button("Dismiss Example data service").click());
    const undo = button("Undo: reopen");
    await act(async () => {
      undo.click();
      undo.click();
    });
    expect(submit).toHaveBeenCalledTimes(2);
    await act(async () => settle({ state: "ok", result: { revision: 5 } }));
    expect(host.textContent).not.toContain("Dismissed");
  });
  it("reopens a closed card in the Passed view, which it then leaves", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 5 } });
    mount(submit, { items: [closed], stage: "passed" });
    expect(host.querySelector("select[aria-label^='Stage for']")).toBeNull();
    await act(async () => button("Reopen Example data service (move to Review)").click());
    expect(submit).toHaveBeenCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 4,
      to_stage_id: "review",
    });
    expect(count()).toBe("0 jobs");
  });
});

describe("the Undo for a job dismissed on its page", () => {
  const dismissed = { item: closed, to: { stage_id: "review", label: "Review", kind: "open" }, href: "/leads/detail?ref=92a85" };
  it("shows once, then drops the parameters from the address", () => {
    at(`${PAGE}&dismissed=92a85&from=review`);
    mount(vi.fn(), { items: [other], dismissed });
    expect(host.textContent).toContain("Dismissed Example data service.");
    expect(replaceState.mock.calls[0]?.slice(1)).toEqual(["", PAGE]);
  });
  it("reopens the job and reloads the queue to show it", async () => {
    at(`${PAGE}&dismissed=92a85&from=review`);
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: { revision: 5 } });
    mount(submit, { items: [other], dismissed });
    await act(async () => button("Undo: reopen Example data service").click());
    expect(submit).toHaveBeenCalledWith("leads.opportunity.transition", {
      ref: job.ref,
      revision: 4,
      to_stage_id: "review",
    });
    expect(replace).toHaveBeenCalledWith(PAGE);
  });
  it("says why when the job changed since", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "record_stale", text: "" });
    mount(submit, { items: [other], dismissed });
    await act(async () => button("Undo: reopen").click());
    expect(host.textContent).toContain("This changed since it was dismissed.");
    expect(replace).not.toHaveBeenCalled();
  });
  it("keeps the parameters through the saved-view redirect", () => {
    const store: Record<string, string> = {
      [PREFS_KEY]: JSON.stringify({ pipeline: "jobs", stage: "qualified", hideFull: false, sort: "score" }),
    };
    vi.spyOn(window, "localStorage", "get").mockReturnValue({
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => void (store[key] = value),
    } as unknown as Storage);
    at(`https://leads.example.com/queue?dismissed=92a85&from=review`);
    mount(vi.fn(), {
      items: [other],
      dismissed,
      explicit: false,
      stages: [
        { value: "review", label: "Review" },
        { value: "qualified", label: "Qualified" },
      ],
      action: "https://leads.example.com/queue",
    });
    const target = new URL(String(replace.mock.calls[0]?.[0]));
    expect(target.searchParams.get("stage")).toBe("qualified");
    expect(target.searchParams.get("dismissed")).toBe("92a85");
    expect(target.searchParams.get("from")).toBe("review");
    expect(replaceState).not.toHaveBeenCalled();
  });
});
