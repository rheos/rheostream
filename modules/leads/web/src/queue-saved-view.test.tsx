// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_PREFS, PREFS_KEY } from "./queue";
import { JobQueue, type QueueProps } from "./queue-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let host: HTMLDivElement;
let root: Root;
const replace = vi.fn();
const assign = vi.fn();
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  replace.mockReset();
  assign.mockReset();
  window.localStorage.clear();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, replace, assign },
  });
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function props(overrides: Partial<QueueProps> = {}): QueueProps {
  return {
    items: [],
    now: 0,
    zone: "UTC",
    pipelines: [{ value: "jobs", label: "Job leads" }],
    stages: [
      { value: "open", label: "Open stages" },
      { value: "review", label: "Review" },
    ],
    pipeline: "jobs",
    stage: "review",
    hideFull: true,
    sort: "score",
    explicit: false,
    filtered: false,
    hiddenOnly: false,
    action: "https://example.test/leads/",
    hrefs: {},
    noun: ["job", "jobs"],
    limited: false,
    nothingYet: <p>Nothing yet.</p>,
    ...overrides,
  };
}

describe("saved queue view (#369)", () => {
  it("hands choices saved only in the browser to the server once", () => {
    const text = JSON.stringify({ ...DEFAULT_PREFS, sort: "newest" });
    window.localStorage.setItem(PREFS_KEY, text);
    const savePreference = vi.fn(async () => {});
    act(() => root.render(<JobQueue {...props({ savePreference })} />));
    expect(savePreference).toHaveBeenCalledWith("queue", text);
    // Without a server copy this render is still the default view, so the
    // browser moves to the saved one as before.
    expect(replace).toHaveBeenCalledWith(expect.stringContaining("sort=newest"));
  });

  it("neither saves nor redirects when the server already rendered the saved view", () => {
    window.localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...DEFAULT_PREFS, sort: "newest" }),
    );
    const savePreference = vi.fn(async () => {});
    act(() =>
      root.render(
        <JobQueue
          {...props({ sort: "newest", serverSaved: true, savePreference })}
        />,
      ),
    );
    expect(savePreference).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("never redirects to an older browser copy when the server holds a saved view", () => {
    // The server rendered "newest" from its cookie; this browser still holds
    // "score" from before (another device chose, or a storage write failed).
    window.localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...DEFAULT_PREFS, sort: "score", stage: "open" }),
    );
    const savePreference = vi.fn(async () => {});
    act(() =>
      root.render(
        <JobQueue
          {...props({ sort: "newest", serverSaved: true, savePreference })}
        />,
      ),
    );
    expect(replace).not.toHaveBeenCalled();
    expect(savePreference).not.toHaveBeenCalled();
  });

  it("saves a new choice, then reloads the view it names", async () => {
    let release = () => {};
    const savePreference = vi.fn(
      () => new Promise<void>((resolve) => (release = resolve)),
    );
    act(() => root.render(<JobQueue {...props({ savePreference })} />));
    const select = host.querySelector<HTMLSelectElement>('select[name="sort"]');
    if (!select) throw new Error("no sort control");
    act(() => {
      select.value = "newest";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(savePreference).toHaveBeenCalledWith(
      "queue",
      expect.stringContaining('"sort":"newest"'),
    );
    expect(assign).not.toHaveBeenCalled();
    // The select is controlled, so React has already put "score" back in it;
    // the reload still carries the choice, read before the save finished.
    expect(select.value).toBe("score");
    await act(async () => release());
    expect(assign).toHaveBeenCalledTimes(1);
    const url = new URL(String(assign.mock.calls[0]?.[0]));
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      pipeline: "jobs",
      stage: "review",
      full: "hide",
      sort: "newest",
    });
  });

  it("reloads with the choice even when the save fails", async () => {
    const savePreference = vi.fn(async () => {
      throw new Error("refused");
    });
    act(() => root.render(<JobQueue {...props({ savePreference })} />));
    const select = host.querySelector<HTMLSelectElement>('select[name="sort"]');
    if (!select) throw new Error("no sort control");
    act(() => {
      select.value = "newest";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {});
    expect(String(assign.mock.calls[0]?.[0])).toContain("sort=newest");
  });
});
