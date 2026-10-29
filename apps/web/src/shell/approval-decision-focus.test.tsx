// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ApprovalActionError,
  ApprovalOutcomeHeading,
} from "@/shell/approval-decision-focus";
import { ApprovalDecisionForms } from "@/shell/approval-decision-forms";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("approval decision focus transfer", () => {
  it("focuses the refreshed durable result heading after hydration", async () => {
    const refuse = async () => ({ state: "idle" as const, message: "" });
    await act(async () => {
      root.render(<ApprovalDecisionForms refuse={refuse} />);
    });
    container.querySelector("button")!.focus();

    await act(async () => {
      root.render(<ApprovalOutcomeHeading>Current item created</ApprovalOutcomeHeading>);
    });

    const heading = container.querySelector("h1");
    expect(heading?.textContent).toBe("Current item created");
    expect(heading?.getAttribute("tabindex")).toBe("-1");
    expect(document.activeElement).toBe(heading);
  });

  it("focuses an action failure summary while retaining live-alert semantics", async () => {
    const prior = document.createElement("button");
    container.append(prior);
    prior.focus();

    await act(async () => {
      root.render(
        <ApprovalActionError>
          The decision was not applied. Reload the approval and review its current state.
        </ApprovalActionError>,
      );
    });

    const error = container.querySelector('[role="alert"]');
    expect(error?.getAttribute("tabindex")).toBe("-1");
    expect(document.activeElement).toBe(error);
  });

  it("moves focus from the submitted decision to its returned failure summary", async () => {
    const message = "The decision was not applied. Reload the approval and review its current state.";
    const refuse = async () => ({ state: "error" as const, message });

    await act(async () => {
      root.render(<ApprovalDecisionForms refuse={refuse} />);
    });
    const button = container.querySelector("button")!;
    button.focus();

    await act(async () => {
      container.querySelector("form")!.requestSubmit();
    });

    const error = container.querySelector('[role="alert"]');
    expect(error?.textContent).toBe(message);
    expect(document.activeElement).toBe(error);
    expect(container.querySelector("fieldset")?.disabled).toBe(true);
  });
});
