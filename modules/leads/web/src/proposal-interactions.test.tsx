// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OperationOutcome } from "@rheo-stream/web-contract/screen";
import type { Row } from "./data";
import { REFRESH_MS, WAITING_REFRESH_MS } from "./proposal";
import { ProposalWorkflow, type WorkflowProps } from "./proposal-workflow";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const REF = "leads.opportunity:0a1b2c3d-0000-7000-8000-00000000b001";
const LETTER = { id: "s0", position: 0, kind: "cover_letter", question: null };
const ANSWER = { id: "s1", position: 1, kind: "answer", question: "Rate?" };
function view(state: string, extra: Row = {}): Row {
  return {
    opportunity_ref: REF,
    state,
    questions: "Rate?",
    sections: [
      { ...LETTER, body: "The letter.", library_match_id: null, edited: false },
      { ...ANSWER, body: "Fifty.", library_match_id: null, edited: false },
    ],
    draft: "The letter.\n\n---\n\n**Rate?**\nFifty.",
    findings: { accuracy: [], accuracy_checked: true, style: [], analysis: {} },
    ...extra,
  };
}
const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const refused = (code: string): OperationOutcome => ({
  state: "refused",
  code,
  text: "",
});

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  window.location.hash = "";
  host = window.document.createElement("div");
  window.document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

function mount(props: Partial<WorkflowProps> & { proposal: Row }) {
  act(() =>
    root.render(
      <ProposalWorkflow
        opportunityRef={REF}
        posting="An example posting."
        {...props}
      />,
    ),
  );
}
function button(name: string): HTMLButtonElement {
  const found = Array.from(host.querySelectorAll("button")).find(
    (b) =>
      b.textContent?.trim() === name || b.getAttribute("aria-label") === name,
  );
  if (!found) throw new Error(`no button ${name}`);
  return found;
}
async function click(element: HTMLElement) {
  await act(async () => {
    element.click();
  });
}
function type(element: HTMLTextAreaElement | HTMLInputElement, text: string) {
  const proto = Object.getPrototypeOf(element) as object;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  act(() => {
    setter?.call(element, text);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function blur(element: HTMLElement) {
  await act(async () => {
    element.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
  });
}

describe("drafting over hand edits", () => {
  it("asks first, keeps the edits by default, and forces only on a yes", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce(refused("draft_edited"))
      .mockResolvedValueOnce(ok(view("queued")));
    mount({ submit, proposal: view("ready") });
    await click(button("Regenerate proposal"));
    expect(host.textContent).toContain("Draft again anyway?");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    await click(button("Replace my edits"));
    expect(submit.mock.calls.map((c) => c[1])).toEqual([
      { ref: REF, questions: "Rate?", force: false },
      { ref: REF, questions: "Rate?", force: true },
    ]);
    expect(host.textContent).toContain("Queued");
  });
});

describe("editing a section", () => {
  it("keeps the typed text open when the save fails, and saves on success", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce({ state: "unavailable" })
      .mockImplementationOnce(async (_name: string, input: Row) =>
        ok(
          view("ready", {
            sections: [
              { ...LETTER, body: "The letter.", edited: false },
              { ...ANSWER, body: input.body, edited: true },
            ],
          }),
        ),
      );
    mount({ submit, proposal: view("ready") });
    await click(button("Edit: Rate?"));
    const area = host.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Edit: Rate?"]',
    );
    if (!area) throw new Error("no editor");
    type(area, "Sixty, firm.");
    await blur(area);
    // Still open, with the typed text and the reason.
    const still = host.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Edit: Rate?"]',
    );
    expect(still?.value).toBe("Sixty, firm.");
    expect(host.textContent).toContain("Not saved: Could not reach the server.");
    await blur(still as HTMLTextAreaElement);
    expect(submit).toHaveBeenLastCalledWith("leads.proposalsection.set", {
      ref: REF,
      section_id: "s1",
      body: "Sixty, firm.",
    });
    expect(host.querySelector('textarea[aria-label="Edit: Rate?"]')).toBeNull();
    expect(host.textContent).toContain("Sixty, firm.");
    expect(host.textContent).toContain("Edited");
  });
  it("refuses an empty answer locally and Escape restores the saved text", async () => {
    const submit = vi.fn();
    mount({ submit, proposal: view("ready") });
    await click(button("Edit: Rate?"));
    const area = host.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Edit: Rate?"]',
    ) as HTMLTextAreaElement;
    type(area, "   ");
    await blur(area);
    expect(submit).not.toHaveBeenCalled();
    expect(host.textContent).toContain("An answer cannot be empty");
    await act(async () => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    expect(host.textContent).toContain("Fifty.");
  });
  it("offers the library answer once one is saved for the question", async () => {
    const submit = vi.fn().mockResolvedValue(
      ok({ id: "lib1", question: "Rate?", answer: "Fifty.", updated_at: "" }),
    );
    mount({ submit, proposal: view("ready") });
    expect(() => button("Use the saved library answer")).toThrow();
    await click(
      button(
        "Save this answer to the library (strip anything specific to this client first)",
      ),
    );
    expect(submit).toHaveBeenCalledWith("leads.answerlibrary.save", {
      question: "Rate?",
      answer: "Fifty.",
    });
    expect(button("Use the saved library answer")).toBeTruthy();
    expect(host.textContent).toContain("Saved to the library");
  });
});

describe("the whole draft", () => {
  it("holds unsaved text: no regenerate or going back until saved or undone", async () => {
    const submit = vi.fn().mockResolvedValue(
      ok(view("ready", { draft: "New letter.\n\n---\n\n**Rate?**\nFifty." })),
    );
    mount({ submit, proposal: view("ready") });
    await click(button("Edit the whole draft"));
    const area = host.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="The whole draft"]',
    ) as HTMLTextAreaElement;
    type(area, "New letter.\n\n---\n\n**Rate?**\nFifty.");
    expect(button("Regenerate proposal").disabled).toBe(true);
    expect(button("Back to sections").disabled).toBe(true);
    await click(button("Save the draft"));
    expect(submit).toHaveBeenCalledWith("leads.jobproposal.update", {
      ref: REF,
      draft: "New letter.\n\n---\n\n**Rate?**\nFifty.",
    });
    expect(host.textContent).toContain("Saved.");
    expect(button("Regenerate proposal").disabled).toBe(false);
  });
});

describe("following a draft", () => {
  it("reloads while drafting, and stops once someone types on the page", async () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    mount({ proposal: view("drafting", { sections: [] }), reload });
    act(() => vi.advanceTimersByTime(REFRESH_MS));
    expect(reload).toHaveBeenCalledTimes(1);
    // Someone types elsewhere on the page (a note, the paste form).
    const note = window.document.createElement("textarea");
    window.document.body.appendChild(note);
    type(note, "half a note");
    act(() => vi.advanceTimersByTime(REFRESH_MS * 5));
    expect(reload).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("stopped following the draft");
    await click(button("Check progress"));
    expect(reload).toHaveBeenCalledTimes(2);
    note.remove();
  });
  it("looks only once a minute while the account holds the draft, and says why", () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    mount({
      proposal: view("queued", {
        sections: [],
        failure_kind: "credential_invalid",
      }),
      reload,
    });
    expect(host.textContent).toContain("the model login needs attention");
    act(() => vi.advanceTimersByTime(REFRESH_MS * 3));
    expect(reload).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(WAITING_REFRESH_MS));
    expect(reload).toHaveBeenCalledTimes(1);
    expect(button("Stop drafting")).toBeTruthy();
  });
  it("follows in place with the shell's refresh and takes in the newest proposal (#443)", async () => {
    vi.useFakeTimers();
    const refresh = vi.fn().mockResolvedValue(undefined);
    mount({ proposal: view("drafting", { sections: [] }), refresh });
    act(() => vi.advanceTimersByTime(REFRESH_MS * 2));
    expect(refresh).toHaveBeenCalledTimes(2);
    // The refresh renders the screen again with the settled draft.
    mount({ proposal: view("ready"), refresh });
    expect(host.textContent).toContain("Fifty.");
    expect(host.querySelector('[role="status"]')).toBeNull();
    act(() => vi.advanceTimersByTime(REFRESH_MS * 5));
    expect(refresh).toHaveBeenCalledTimes(2);
  });
  it("never reloads a settled draft", () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    mount({ proposal: view("ready"), reload });
    act(() => vi.advanceTimersByTime(REFRESH_MS * 10));
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("a failed regenerate", () => {
  it("keeps the earlier draft visible and usable", () => {
    mount({
      submit: vi.fn(),
      proposal: view("failed", { failure_kind: "output_invalid" }),
    });
    expect(host.textContent).toContain("The earlier draft below is unchanged.");
    expect(host.textContent).toContain("Earlier draft");
    expect(host.textContent).toContain("Fifty.");
    expect(button("Delete draft")).toBeTruthy();
  });
});

describe("the pricing tab", () => {
  const MILESTONE = {
    id: "m1",
    position: 0,
    title: "Discovery",
    description: "",
    amount: "200.00",
    due_on: "2026-10-19",
  };
  function priced(extra: Row = {}): Row {
    return view("ready", {
      pricing: {
        available: true,
        state: "ready",
        milestones: [MILESTONE],
        suggestion: { rate: "40.00", hours: "5.0", fixed_total: "200.00", basis: "b" },
        ...extra,
      },
    });
  }
  it("asks before replacing milestones and keeps them by default", async () => {
    const submit = vi
      .fn()
      .mockResolvedValue(ok(priced({ state: "queued", milestones: [] })));
    mount({ submit, proposal: priced() });
    await click(button("Regenerate milestones"));
    expect(submit).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Your edits to them are replaced.");
    await click(button("Regenerate"));
    expect(submit).toHaveBeenCalledWith("leads.jobpricing.request", { ref: REF });
    expect(host.textContent).toContain("Suggesting milestones");
  });
  it("keeps a typed amount when its save fails, and saves a good one", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce(refused("pricing_in_progress"))
      .mockImplementationOnce(async (_name: string, input: Row) =>
        ok(priced({ milestones: [{ ...MILESTONE, amount: String(input.amount) }] })),
      );
    mount({ submit, proposal: priced() });
    // A milestone reads as text until its Edit button opens the fields.
    expect(host.querySelector('input[type="number"]')).toBeNull();
    expect(host.textContent).toContain("$200.00, due 2026-10-19");
    await click(button("Edit: Discovery"));
    const amount = host.querySelector<HTMLInputElement>('input[type="number"]');
    if (!amount) throw new Error("no amount");
    type(amount, "350");
    await blur(amount);
    expect(host.textContent).toContain("Not saved: Milestones are already being prepared.");
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe("350");
    await blur(host.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement);
    expect(submit).toHaveBeenLastCalledWith("leads.proposalmilestone.set", {
      ref: REF,
      milestone_id: "m1",
      amount: "350",
    });
    type(host.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement, "-5");
    await blur(host.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement);
    expect(submit).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("zero or more");
  });
  it("never shows an unsaved title as saved after Done", async () => {
    const submit = vi.fn().mockResolvedValue(refused("pricing_in_progress"));
    mount({ submit, proposal: priced() });
    await click(button("Edit: Discovery"));
    const title = host.querySelector<HTMLInputElement>('input[aria-label="Milestone title"]');
    if (!title) throw new Error("no title");
    type(title, "Renamed");
    await blur(title);
    expect(host.textContent).toContain("Not saved: Milestones are already being prepared.");
    await click(button("Done"));
    // Back to the stored title, with the reason still in view.
    expect(host.querySelector("article[aria-label^=\"Milestone\"] h3")?.textContent).toBe("Discovery");
    expect(host.textContent).not.toContain("Renamed");
    expect(host.textContent).toContain("Not saved: Milestones are already being prepared.");
  });
    it("reverts only the field whose save failed", async () => {
    const submit = vi
      .fn()
      .mockResolvedValueOnce(refused("pricing_in_progress"))
      .mockImplementationOnce(async (_name: string, input: Row) =>
        ok(priced({ milestones: [{ ...MILESTONE, description: String(input.description) }] })),
      );
    mount({ submit, proposal: priced() });
    await click(button("Edit: Discovery"));
    const title = host.querySelector<HTMLInputElement>('input[aria-label="Milestone title"]')!;
    type(title, "Renamed");
    await blur(title);
    const text = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="Milestone description"]')!;
    type(text, "Read the code.");
    await blur(text);
    await click(button("Done"));
    const card = host.querySelector('article[aria-label^="Milestone"]')!;
    // The refused title is back to the stored one; the saved description stays.
    expect(card.querySelector("h3")?.textContent).toBe("Discovery");
    expect(card.textContent).toContain("Read the code.");
    expect(card.textContent).toContain("Not saved: Milestones are already being prepared.");
  });
    it("clears a due date on the server too", async () => {
    const submit = vi.fn().mockResolvedValue(ok(priced({ milestones: [{ ...MILESTONE, due_on: null }] })));
    mount({ submit, proposal: priced() });
    await click(button("Edit: Discovery"));
    const due = host.querySelector<HTMLInputElement>('input[type="date"]') as HTMLInputElement;
    type(due, "");
    await blur(due);
    expect(submit).toHaveBeenCalledWith("leads.proposalmilestone.set", {
      ref: REF,
      milestone_id: "m1",
      due_on: null,
    });
  });
  it("words a refusal to price", async () => {
    const submit = vi.fn().mockResolvedValue(refused("pricing_not_set"));
    mount({ submit, proposal: view("ready", { pricing: { available: true, state: "idle", milestones: [] } }) });
    await click(button("Suggest milestones"));
    expect(host.textContent).toContain("Pricing is not set up yet");
  });
});
