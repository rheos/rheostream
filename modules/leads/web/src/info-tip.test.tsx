// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { InfoTip } from "./info-tip";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
  act(() =>
    root.render(
      <>
        <InfoTip label="About pricing">
          <p>Priced from the day rate.</p>
        </InfoTip>
        <button type="button">Elsewhere</button>
      </>,
    ),
  );
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const button = () =>
  host.querySelector<HTMLButtonElement>('button[aria-label="About pricing"]')!;
const panel = () => host.querySelector<HTMLElement>('[role="note"]')!;

describe("the info tip (#408)", () => {
  it("starts closed, with its text in the markup", () => {
    expect(button().getAttribute("aria-expanded")).toBe("false");
    expect(panel().hidden).toBe(true);
    expect(panel().textContent).toBe("Priced from the day rate.");
    expect(button().getAttribute("aria-controls")).toBe(panel().id);
  });

  it("opens on click and closes on a second click", () => {
    act(() => button().click());
    expect(button().getAttribute("aria-expanded")).toBe("true");
    expect(panel().hidden).toBe(false);
    act(() => button().click());
    expect(panel().hidden).toBe(true);
  });

  it("closes on Escape and gives focus back to its button", () => {
    act(() => button().click());
    button().focus();
    act(() => {
      button().dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    expect(panel().hidden).toBe(true);
    expect(window.document.activeElement).toBe(button());
  });

  it("leaves focus in a field elsewhere when Escape closes it from there", () => {
    act(() => button().click());
    const elsewhere = host.querySelector<HTMLButtonElement>("button:not([aria-label])")!;
    elsewhere.focus();
    act(() => {
      elsewhere.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    expect(panel().hidden).toBe(true);
    expect(window.document.activeElement).toBe(elsewhere);
  });

  it("closes when the pointer goes down outside it", () => {
    act(() => button().click());
    const elsewhere = host.querySelector<HTMLButtonElement>("button:not([aria-label])")!;
    act(() => {
      elsewhere.dispatchEvent(new window.PointerEvent("pointerdown", { bubbles: true }));
    });
    expect(panel().hidden).toBe(true);
  });
});
