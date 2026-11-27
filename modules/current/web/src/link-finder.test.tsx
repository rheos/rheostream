// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const ITEM = "current.item:01900000-0000-7000-8000-000000000001";
const MATCH = "current.item:01900000-0000-7000-8000-000000000009";
const summary = (ref: string, title: string) => ({
  ref,
  title,
  status: "open",
  priority: "normal",
  area: null,
  project_ref: null,
  due_on: null,
  snoozed_until: null,
  woke: false,
  overdue: false,
  blocked: false,
  event_count: 1,
  revision: 1,
  updated_at: "2026-01-02T10:00:00Z",
});

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

async function mounted() {
  const answers: Record<string, OperationOutcome> = {
    "current.item.get": ok({
      ...summary(ITEM, "Renew the example.com domain"),
      body: "",
      provenance: "manual",
      source_kind: null,
      source_ref: null,
      created_at: "2026-01-01T08:00:00Z",
      closed_at: null,
      project: null,
      blockers: [],
      blocking: [],
      comments: [],
    }),
    "current.area.list": ok({ items: [] }),
    "current.project.list": ok({ items: [] }),
    "current.item.list": ok({
      items: [summary(MATCH, "Pay the hosting bill")],
      next_offset: null,
    }),
  };
  const submit = vi.fn(async () => ok({}));
  const api = {
    role: "owner",
    call: vi.fn(
      async (name: string): Promise<OperationOutcome> =>
        answers[name] ?? { state: "unavailable" },
    ),
    submit,
    href: (id: string, q?: Record<string, string | undefined>) =>
      `/current/${id}?${new URLSearchParams(q as Record<string, string>)}`,
  } satisfies ShellApi;
  const page = await screens.item({ shell: api, query: { ref: ITEM, find: "hosting" } });
  act(() => root.render(page));
  return submit;
}

async function press(label: string) {
  const button = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  expect(button).not.toBeNull();
  await act(async () => button!.click());
}

describe("linking a found item (#645)", () => {
  it("Blocks this makes the match a blocker of this item", async () => {
    const submit = await mounted();
    await press("Pay the hosting bill blocks this item");
    expect(submit).toHaveBeenCalledExactlyOnceWith("current.dependency.add", {
      item_ref: ITEM,
      blocker_ref: MATCH,
    });
  });
  it("Waits on this makes this item a blocker of the match", async () => {
    const submit = await mounted();
    await press("Pay the hosting bill waits on this item");
    expect(submit).toHaveBeenCalledExactlyOnceWith("current.dependency.add", {
      item_ref: MATCH,
      blocker_ref: ITEM,
    });
  });
});
