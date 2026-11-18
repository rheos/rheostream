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
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  replace.mockReset();
  window.localStorage.clear();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, replace },
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

  it("saves a new choice before the form reloads the view", async () => {
    let release = () => {};
    const savePreference = vi.fn(
      () => new Promise<void>((resolve) => (release = resolve)),
    );
    act(() => root.render(<JobQueue {...props({ savePreference })} />));
    const form = host.querySelector("form");
    const submitted = vi.fn((event: Event) => event.preventDefault());
    form?.addEventListener("submit", submitted);
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
    expect(submitted).not.toHaveBeenCalled();
    await act(async () => release());
    expect(submitted).toHaveBeenCalledTimes(1);
  });
});
