// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { ModalForm } from "./modal-form";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

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

function mount(submit: ReturnType<typeof vi.fn>) {
  act(() =>
    root.render(
      <ModalForm
        submit={submit as unknown as NonNullable<ShellApi["submit"]>}
        trigger="Add note"
        title="Add a note"
        operation="leads.opportunity.add_note"
        base={{ ref: "leads.opportunity:x", revision: 1 }}
        button="Save note"
        fields={[{ name: "body", label: "Note", type: "textarea", required: true }]}
      />,
    ),
  );
}
const dialog = () => host.querySelector("dialog")!;

describe("the modal form (#418)", () => {
  it("shows only its button until opened", () => {
    mount(vi.fn());
    expect(dialog().open).toBe(false);
    expect(dialog().getAttribute("aria-labelledby")).toBe(
      host.querySelector("h2")?.id,
    );
  });

  it("opens on its button and closes itself once the form saves", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "ok", result: {} });
    mount(submit);
    const open = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent === "Add note",
    )!;
    act(() => open.click());
    expect(dialog().open).toBe(true);
    const text = host.querySelector("textarea")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value",
      )!.set!;
      setter.call(text, "Call on Tuesday.");
      text.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    await act(async () => {
      host.querySelector("form")!.requestSubmit();
    });
    expect(submit).toHaveBeenCalledWith(
      "leads.opportunity.add_note",
      expect.objectContaining({ body: "Call on Tuesday." }),
    );
    expect(dialog().open).toBe(false);
  });

  it("never lets a save from an earlier opening close a reopened dialog", async () => {
    let finish: (value: unknown) => void = () => {};
    const submit = vi.fn(() => new Promise((resolve) => (finish = resolve)));
    mount(submit);
    const open = () =>
      Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Add note")!;
    act(() => open().click());
    await act(async () => {
      const text = host.querySelector("textarea")!;
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!.call(
        text,
        "First.",
      );
      text.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    await act(async () => host.querySelector("form")!.requestSubmit());
    act(() => host.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.click());
    act(() => open().click());
    expect(dialog().open).toBe(true);
    await act(async () => finish({ state: "ok", result: {} }));
    expect(dialog().open).toBe(true);
  });

    it("closes from its Close button", () => {
    mount(vi.fn());
    act(() =>
      Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Add note")!.click(),
    );
    act(() => host.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.click());
    expect(dialog().open).toBe(false);
  });
});
