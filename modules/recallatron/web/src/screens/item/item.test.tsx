import { describe, expect, it } from "vitest";

import { NOT_FOUND } from "../../guards";
import { GET } from "../../operations";
import { callsOutside, fakeShell, type Responder } from "../../testing/fake-shell";
import { field, fixture, ok, refusal } from "../../testing/fixtures";
import { render, textOf } from "../../testing/markup";
import { loadItem } from "./load";
import { ItemView } from "./view";

const REF = field("memory-item.json", "ref");

async function itemFor(ref: string | undefined, get: Responder = ok("memory-item.json")) {
  const { shell, calls } = fakeShell("member", { [GET]: get });
  const state = await loadItem(shell, { ref });
  const markup = render(<ItemView state={state} />);
  return { calls, markup, text: textOf(markup) };
}

describe("Item", () => {
  it("gets the memory by the ref in the query", async () => {
    const { calls } = await itemFor(REF);
    expect(calls).toEqual([{ operation: GET, input: { ref: REF } }]);
  });

  it.each([
    ["a found memory", ok("memory-item.json")],
    ["a missing memory", refusal(NOT_FOUND)],
    ["an unavailable read", { state: "unavailable" } as const],
  ] as const)("calls only get for %s", async (_label, get) => {
    const { calls } = await itemFor(REF, get);
    expect(calls.length).toBeGreaterThan(0);
    expect(callsOutside(calls, [GET])).toEqual([]);
  });

  it("renders kind, title, full body, UTC times, revision and invalidation", async () => {
    const { text } = await itemFor(REF);
    expect(text).toContain("fact");
    expect(text).toContain("Old watering note for the sample garden");
    expect(text).toContain("Beds were watered every evening during the trial week.");
    expect(text).toContain("Occurred 2026-01-12 18:00 UTC");
    expect(text).toContain("Recorded 2026-01-12 18:30 UTC");
    expect(text).toContain("Revision 3");
    expect(text).toContain("Invalidated 2026-02-18 08:30 UTC");
    expect(text).toContain("source_superseded");
  });

  it("links the successor to its own Item view", async () => {
    const { markup } = await itemFor(REF);
    const successor = field("memory-item.json", "superseded_by");
    expect(markup).toContain(`href="/item?ref=${encodeURIComponent(successor)}"`);
  });

  it("renders a link by its display value, or its bare ref when display is null", async () => {
    const memory = fixture("memory-item.json") as { links: Record<string, unknown>[] };
    const bare = { ...memory.links[0], display: null };
    const { text } = await itemFor(REF, {
      state: "ok",
      result: { ...memory, links: [...memory.links, bare] },
    });
    expect(text).toContain("Trial week plan for the sample garden");
    expect(text).toContain(field("memory-item.json", "links", 0, "ref"));
  });

  it("omits the invalidation and successor rows for a current memory", async () => {
    // The window's second item: current, never replaced, and with no occurred_at.
    const current = (fixture("read-window.json") as { items: unknown[] }).items[1];
    const { text } = await itemFor(REF, { state: "ok", result: current });
    expect(text).not.toContain("Invalidated");
    expect(text).not.toContain("Replaced by");
    expect(text).not.toContain("Occurred");
  });

  it("says the memory is unavailable when it is not found", async () => {
    const { markup, text } = await itemFor(REF, refusal(NOT_FOUND));
    expect(markup).toContain('data-state="memory-not-found"');
    expect(text).toContain("This memory is unavailable.");
  });

  it("makes no call when there is no ref", async () => {
    const { calls, markup } = await itemFor("   ");
    expect(calls).toEqual([]);
    expect(markup).toContain('data-state="memory-not-found"');
  });

  it("falls back to the generic error otherwise", async () => {
    const { text } = await itemFor(REF, { state: "unavailable" });
    expect(text).toContain("Memory is unavailable right now.");
  });
});
