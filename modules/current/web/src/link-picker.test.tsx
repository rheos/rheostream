// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import {
  LinkPicker,
  PICK_DELAY_MS,
  PICK_SHOWN,
  type LinkSide,
} from "./link-picker";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const ITEM = "current.item:01900000-0000-7000-8000-000000000001";
const LINKED = "current.item:01900000-0000-7000-8000-000000000002";
const NAMED = "current.item:01900000-0000-7000-8000-00000000abcd";
const ref = (n: number) =>
  `current.item:01900000-0000-7000-8000-${String(n).padStart(12, "0")}`;
const summary = (r: string, title: string, status = "open") => ({
  ref: r,
  short_id: r.slice(-8),
  title,
  status,
  priority: "normal",
  revision: 1,
});
const page = (...items: unknown[]) => ok({ items, next_offset: null });

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.useFakeTimers();
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

function mount(
  side: LinkSide,
  read: ReturnType<typeof vi.fn>,
  submit = vi.fn(async () => ok({})),
  linked: readonly string[] = [ITEM, LINKED],
) {
  act(() =>
    root.render(
      <LinkPicker
        submit={submit as unknown as NonNullable<ShellApi["submit"]>}
        read={read as unknown as NonNullable<ShellApi["read"]>}
        itemRef={ITEM}
        subject="Renew the example.com domain"
        side={side}
        linked={linked}
        reloadHref="/current/item?ref=00000001"
      />,
    ),
  );
  return submit;
}
const opener = () =>
  host.querySelector<HTMLButtonElement>("button[aria-haspopup]")!;
const box = () => host.querySelector<HTMLInputElement>("dialog input")!;
const rows = () =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("dialog li button"));
const dialogOpen = () => host.querySelector("dialog")!.open;

function open() {
  act(() => opener().click());
}
async function type(text: string) {
  const input = box();
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )!.set!;
  act(() => {
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PICK_DELAY_MS);
  });
}

describe("link picker (#645)", () => {
  it("opens on the search box and reads nothing until something is typed", () => {
    const read = vi.fn();
    mount("blocker", read);
    expect(opener().textContent).toBe("Add a blocker");
    open();
    expect(dialogOpen()).toBe(true);
    expect(window.document.activeElement).toBe(box());
    expect(rows()).toHaveLength(0);
    expect(read).not.toHaveBeenCalled();
  });

  it("searches titles as you type, leaving out this item and those already linked", async () => {
    const read = vi.fn(async () =>
      page(
        summary(ITEM, "Renew the example.com domain"),
        summary(LINKED, "Find the registrar login"),
        summary(ref(9), "Pay the hosting bill"),
      ),
    );
    mount("blocker", read);
    open();
    await type("  pay ");
    expect(read).toHaveBeenCalledExactlyOnceWith("current.item.list", {
      status: "active",
      query: "pay",
      match: "title",
      limit: PICK_SHOWN + 1 + 2,
    });
    expect(rows().map((r) => r.textContent)).toEqual([
      "Pay the hosting bill00000009",
    ]);
  });

  it("waits for typing to pause, and clearing the box clears the list", async () => {
    const read = vi.fn(async () =>
      page(summary(ref(9), "Pay the hosting bill")),
    );
    mount("blocker", read);
    open();
    const input = box();
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!;
    for (const text of ["p", "pa", "pay"]) {
      act(() => {
        setter.call(input, text);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(PICK_DELAY_MS / 2);
      });
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PICK_DELAY_MS);
    });
    expect(read).toHaveBeenCalledOnce();
    expect(rows()).toHaveLength(1);
    await type("");
    expect(rows()).toHaveLength(0);
    expect(host.querySelector("dialog")!.textContent).not.toContain(
      "No unfinished item",
    );
    expect(read).toHaveBeenCalledOnce();
  });

  it("drops an answer to an older search that arrives after a newer one", async () => {
    let slow: (value: OperationOutcome) => void = () => {};
    const read = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<OperationOutcome>((resolve) => (slow = resolve)),
      )
      .mockImplementationOnce(async () =>
        page(summary(ref(11), "Order supplies")),
      );
    mount("blocker", read);
    open();
    await type("pay");
    await type("order");
    expect(rows().map((r) => r.textContent)).toEqual([
      "Order supplies00000011",
    ]);
    await act(async () => slow(page(summary(ref(9), "Pay the hosting bill"))));
    expect(rows().map((r) => r.textContent)).toEqual([
      "Order supplies00000011",
    ]);
  });

  it.each([
    ["blocker", { item_ref: ITEM, blocker_ref: ref(9) }],
    ["blocked", { item_ref: ref(9), blocker_ref: ITEM }],
  ] as const)(
    "a pick on the %s side links the right way round and closes",
    async (side, input) => {
      const read = vi.fn(async () =>
        page(summary(ref(9), "Pay the hosting bill")),
      );
      const submit = mount(side, read);
      open();
      await type("pay");
      await act(async () => rows()[0]!.click());
      expect(submit).toHaveBeenCalledExactlyOnceWith(
        "current.dependency.add",
        input,
      );
      expect(dialogOpen()).toBe(false);
    },
  );

  it("stays open and says why when the link is refused", async () => {
    const read = vi.fn(async () =>
      page(summary(ref(9), "Pay the hosting bill")),
    );
    const submit = vi.fn(async (): Promise<OperationOutcome> => ({
      state: "refused",
      code: "dependency_cycle",
      text: "That would make a loop.",
    }));
    mount("blocker", read, submit);
    open();
    await type("pay");
    await act(async () => rows()[0]!.click());
    expect(dialogOpen()).toBe(true);
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
  });

  it("says so when nothing matches, and when the search fails", async () => {
    const read = vi
      .fn()
      .mockResolvedValueOnce(page())
      .mockResolvedValueOnce({ state: "unavailable" });
    mount("blocked", read);
    open();
    await type("zzz");
    expect(host.querySelector("dialog")!.textContent).toContain(
      "No unfinished item has that in its title.",
    );
    await type("yyy");
    expect(host.querySelector("dialog")!.textContent).toContain(
      "The search did not answer.",
    );
  });

  it("asks for at most a page however many links the item has", async () => {
    const read = vi.fn(async () => page());
    const many = Array.from({ length: 120 }, (_, n) => ref(1000 + n));
    mount("blocker", read, undefined, [ITEM, ...many]);
    open();
    await type("pay");
    expect(read).toHaveBeenCalledWith("current.item.list", expect.objectContaining({ limit: 100 }));
  });

  it("says more match once there are more than it shows", async () => {
    const read = vi.fn(async () =>
      page(
        ...Array.from({ length: PICK_SHOWN + 1 }, (_, n) =>
          summary(ref(100 + n), `Hosting task ${n}`),
        ),
      ),
    );
    mount("blocker", read);
    open();
    await type("hosting");
    expect(rows()).toHaveLength(PICK_SHOWN);
    expect(host.querySelector("dialog")!.textContent).toContain(
      "More match. Add a word to narrow it.",
    );
  });

  it("says why a refused search will not work on a retry, with a reload", async () => {
    const read = vi.fn(async (): Promise<OperationOutcome> => ({
      state: "refused",
      code: "workspace_changed",
      text: "Your workspace changed. Reload the page.",
    }));
    mount("blocker", read);
    open();
    await type("pay");
    const text = host.querySelector("dialog")!.textContent!;
    expect(text).not.toContain("Try again");
    expect(
      host.querySelector('dialog a[href="/current/item?ref=00000001"]'),
    ).not.toBeNull();
  });

  it("finds an item by a pasted ID, but not a done one", async () => {
    const read = vi.fn(async (operation: string) =>
      operation === "current.item.get"
        ? ok(summary(NAMED, "Book the venue"))
        : page(),
    );
    mount("blocker", read);
    open();
    await type("00000000abcd");
    expect(read).toHaveBeenCalledWith("current.item.get", {
      ref: "00000000abcd",
    });
    expect(rows().map((r) => r.textContent)).toEqual([
      "Book the venue0000abcd",
    ]);

    read.mockImplementation(async (operation: string) =>
      operation === "current.item.get"
        ? ok(summary(NAMED, "Book the venue", "done"))
        : page(),
    );
    await type("00000000abce");
    expect(rows()).toHaveLength(0);
  });
});
