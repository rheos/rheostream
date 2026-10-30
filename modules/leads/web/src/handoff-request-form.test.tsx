// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import { formInput } from "./action-form";
import { HandoffRequestForm } from "./handoff-request-form";

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
      <HandoffRequestForm
        submit={submit as unknown as NonNullable<ShellApi["submit"]>}
        opportunityRef="leads.opportunity:019bf0b0-0000-7000-8000-000000000003"
        revision={7}
        title="Prepare Northwind onboarding workshop"
      />,
    ),
  );
}

describe("qualified Current handoff request", () => {
  it("nests only the strict Current item fields under the server-derived route", () => {
    expect(
      formInput(
        "leads.handoff.request",
        {
          ref: "leads.opportunity:synthetic",
          revision: 7,
          purpose: "internal_analysis",
          destination_ref: "current",
          idempotency_key: "stable-request-key",
        },
        {
          title: "Prepare Northwind onboarding workshop",
          body: "Confirm agenda and access requirements.",
          priority: "normal",
          area: "client_delivery",
          due_on: "",
        },
      ),
    ).toEqual({
      ref: "leads.opportunity:synthetic",
      revision: 7,
      purpose: "internal_analysis",
      destination_ref: "current",
      idempotency_key: "stable-request-key",
      payload: {
        title: "Prepare Northwind onboarding workshop",
        body: "Confirm agenda and access requirements.",
        priority: "normal",
        area: "client_delivery",
        due_on: null,
      },
    });
  });

  it("keeps one user request key across a lost response and retry", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "unavailable" });
    mount(submit);
    const form = host.querySelector("form");
    if (!form) throw new Error("handoff form missing");

    await act(async () => form.requestSubmit());
    // A shell refresh may render this client boundary again after an unavailable
    // response. The mounted request still represents the same person's action.
    mount(submit);
    await act(async () => form.requestSubmit());

    expect(submit).toHaveBeenCalledTimes(2);
    const first = submit.mock.calls[0]?.[1] as Record<string, unknown>;
    const second = submit.mock.calls[1]?.[1] as Record<string, unknown>;
    expect(first.idempotency_key).toEqual(expect.any(String));
    expect(first.idempotency_key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(first.idempotency_key).toBe(second.idempotency_key);
    expect(first).not.toHaveProperty("source_ref");
    expect(first).toMatchObject({
      destination_ref: "current",
      purpose: "internal_analysis",
      payload: {
        title: "Prepare Northwind onboarding workshop",
        priority: "normal",
      },
    });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      "We could not confirm the save",
    );
  });
});
