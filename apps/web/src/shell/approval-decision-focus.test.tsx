// @vitest-environment happy-dom

import { act, type ReactElement } from "react";
import { createRoot, hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
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

/** Deliver `page` as server HTML, then hydrate it the way a document load does. */
async function hydrateServerPage(page: ReactElement): Promise<{ host: HTMLDivElement; hydrated: Root }> {
  const host = document.createElement("div");
  host.innerHTML = renderToString(page);
  document.body.append(host);
  let hydrated!: Root;
  await act(async () => {
    hydrated = hydrateRoot(host, page);
  });
  // Let any post-hydration re-render and its effects settle before asserting.
  await act(async () => {});
  return { host, hydrated };
}

function ResolvedPage() {
  return (
    <>
      <a href="#rheo-main">Skip to content</a>
      <main id="rheo-main" tabIndex={-1}>
        <ApprovalOutcomeHeading>Current item created</ApprovalOutcomeHeading>
      </main>
    </>
  );
}

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

  it("leaves focus on the document when a resolved approval page loads without a decision", async () => {
    (document.activeElement as HTMLElement | null)?.blur();
    const { host, hydrated } = await hydrateServerPage(<ResolvedPage />);

    const heading = host.querySelector("h1");
    expect(heading?.textContent).toBe("Current item created");
    expect(heading?.getAttribute("tabindex")).toBe("-1");
    expect(document.activeElement).not.toBe(heading);
    expect(document.activeElement).toBe(document.body);

    await act(async () => hydrated.unmount());
    host.remove();
  });

  it("focuses the outcome heading when a decision replaces a hydrated review in place", async () => {
    const refuse = async () => ({ state: "idle" as const, message: "" });
    const review = <ApprovalDecisionForms refuse={refuse} />;
    const { host, hydrated } = await hydrateServerPage(review);
    host.querySelector("button")!.focus();

    // The decision's refresh swaps the review for the durable result on the client.
    await act(async () => {
      hydrated.render(<ResolvedPage />);
    });

    const heading = host.querySelector("h1");
    expect(heading?.textContent).toBe("Current item created");
    expect(document.activeElement).toBe(heading);

    await act(async () => hydrated.unmount());
    host.remove();
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
