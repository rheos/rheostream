// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ProjectPicker } from "./forms";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const ITEM = "current.item:01900000-0000-7000-8000-000000000001";
const A = "current.project:01900000-0000-7000-8000-000000000003";
const B = "current.project:01900000-0000-7000-8000-000000000004";
const PROJECTS = [
  { ref: A, title: "Studio move", status: "active" },
  { ref: B, title: "Tax season", status: "paused" },
  { ref: "current.project:01900000-0000-7000-8000-000000000005", title: "Old launch", status: "done" },
];

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

function mount(submit: ReturnType<typeof vi.fn>, held: string | null, revision = 4) {
  act(() =>
    root.render(
      <ProjectPicker
        submit={submit as unknown as NonNullable<ShellApi["submit"]>}
        itemRef={ITEM}
        revision={revision}
        subject="Renew the example.com domain"
        held={held}
        projects={PROJECTS}
      />,
    ),
  );
}
const dialog = () => host.querySelector("dialog")!;
const button = (text: string) =>
  Array.from(host.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").startsWith(text),
  )!;
const openPicker = () => act(() => host.querySelector<HTMLButtonElement>("[aria-haspopup]")!.click());

describe("the project picker", () => {
  it("shows only its icon until opened, then lists live and paused projects", () => {
    mount(vi.fn(), null);
    expect(dialog().open).toBe(false);
    expect(dialog().querySelector("li")).toBeNull();
    openPicker();
    expect(dialog().open).toBe(true);
    const names = Array.from(dialog().querySelectorAll("li")).map((li) => li.textContent);
    expect(names).toEqual(["Studio move", "Tax seasonPaused"]);
  });

  it("marks the project held, offers a way out, and starts there", () => {
    mount(vi.fn(), A);
    openPicker();
    const held = dialog().querySelector('[aria-current="true"]');
    expect(held?.textContent).toBe("Studio moveNow in");
    expect(document.activeElement).toBe(held);
    expect(button("No project")).toBeTruthy();
  });

  it("starts on the first project when the item has none", () => {
    mount(vi.fn(), null);
    openPicker();
    expect(document.activeElement?.textContent).toBe("Studio move");
  });

  it("opens again after it was closed, with a fresh list", () => {
    mount(vi.fn(), null);
    openPicker();
    act(() => dialog().close());
    expect(dialog().open).toBe(false);
    expect(dialog().querySelector("li")).toBeNull();
    openPicker();
    expect(dialog().open).toBe(true);
    expect(dialog().querySelectorAll("li")).toHaveLength(2);
  });

  it("saves the pick with the item's revision and closes", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit, null);
    openPicker();
    await act(async () => button("Studio move").click());
    expect(submit).toHaveBeenCalledWith("current.item.update", {
      ref: ITEM,
      revision: 4,
      project_ref: A,
    });
    expect(dialog().open).toBe(false);
  });

  it("takes the item out of its project through clear", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit, A);
    openPicker();
    await act(async () => button("No project").click());
    expect(submit).toHaveBeenCalledWith("current.item.update", {
      ref: ITEM,
      revision: 4,
      clear: ["project_ref"],
    });
  });

  it("closes without saving when the project already held is picked", async () => {
    const submit = vi.fn();
    mount(submit, A);
    openPicker();
    await act(async () => button("Studio move").click());
    expect(submit).not.toHaveBeenCalled();
    expect(dialog().open).toBe(false);
  });

  it("stays open and explains a refusal", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue({ state: "refused", code: "record_stale", text: "item changed" });
    mount(submit, null);
    openPicker();
    await act(async () => button("Tax season").click());
    expect(dialog().open).toBe(true);
    expect(dialog().querySelector('[role="alert"]')?.textContent).toMatch(
      /changed since the page loaded/,
    );
  });

  it("uses the revision of the latest render, not the one it opened with", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit, null, 4);
    openPicker();
    mount(submit, null, 5);
    await act(async () => button("Studio move").click());
    expect(submit).toHaveBeenCalledWith("current.item.update", {
      ref: ITEM,
      revision: 5,
      project_ref: A,
    });
  });

  it("keeps a slow save from an earlier opening out of a later one", async () => {
    let finish: (value: unknown) => void = () => {};
    const submit = vi.fn(
      () => new Promise((resolve) => (finish = resolve)),
    );
    mount(submit, null);
    openPicker();
    act(() => button("Studio move").click());
    // Closed while saving (as a reload or Close would), then opened again.
    act(() => dialog().close());
    openPicker();
    await act(async () =>
      finish({ state: "refused", code: "record_stale", text: "item changed" }),
    );
    expect(dialog().open).toBe(true);
    expect(dialog().querySelector('[role="alert"]')).toBeNull();
  });
});
