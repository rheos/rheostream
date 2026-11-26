// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActionForm } from "./action-form";
import { pipelineRef } from "./data";
import { JobPaste } from "./job-paste";
import { PasteCapture } from "./paste-capture";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

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

const FUNNELS = [{ value: "leads.funnel:example", label: "Inquiries" }];
const capture = () => (
  <PasteCapture submit={vi.fn()} funnels={FUNNELS} receiptHref="/leads/item" />
);

// A form submitted before hydration falls back to the browser's own submit. With
// no method that is a GET, which puts every field in the URL (#350).
describe("form method in the server's markup", () => {
  it.each([
    [
      "an action form",
      <ActionForm
        key="action"
        submit={vi.fn()}
        operation="leads.intake.capture"
        button="Save"
        fields={[{ name: "person.email", label: "Email", type: "text" }]}
      />,
    ],
    ["the inquiry paste form", capture()],
    [
      "the job paste form",
      <JobPaste
        key="jobs"
        submit={vi.fn()}
        pipelines={[{ value: pipelineRef("example"), label: "Jobs" }]}
      />,
    ],
  ])("is post for %s", (_name, form) => {
    const forms = renderToStaticMarkup(form).match(/<form[^>]*>/g) ?? [];
    expect(forms).toHaveLength(1);
    expect(forms[0]).toContain('method="post"');
  });
});

describe("the inquiry review form", () => {
  it("is post too", () => {
    act(() => root.render(capture()));
    const source = host.querySelector("textarea")!;
    act(() => {
      Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value",
      )!.set!.call(source, "Hello, I need a short film. Pat, pat@example.com");
      source.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    act(() => host.querySelector<HTMLButtonElement>("form button")!.click());
    expect(host.textContent).toContain("Review your inquiry");
    expect(host.querySelector("form")!.getAttribute("method")).toBe("post");
  });
});
