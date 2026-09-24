import { describe, expect, it } from "vitest";

import { DEDUP_CANDIDATES, GET } from "../../operations";
import { fakeShell, type Responder } from "../../testing/fake-shell";
import { field, fixture, ok, refusal } from "../../testing/fixtures";
import { render, textOf } from "../../testing/markup";
import { loadDuplicates, PAIR_LIMIT } from "./load";
import { DuplicatesView } from "./view";

const ALLOWED = new Set([DEDUP_CANDIDATES, GET]);
const REF_A = field("dedup-pairs.json", "pairs", 0, "ref_a");

function titled(input: Readonly<Record<string, unknown>>) {
  const memory = fixture("memory-item.json") as Record<string, unknown>;
  return {
    state: "ok" as const,
    result: { ...memory, ref: input.ref, title: `Title of ${String(input.ref).slice(-2)}` },
  };
}

async function duplicates(dedup: Responder, get: Responder = titled) {
  const { shell, calls } = fakeShell("owner", { [DEDUP_CANDIDATES]: dedup, [GET]: get });
  const state = await loadDuplicates(shell);
  const markup = render(<DuplicatesView state={state} />);
  return { calls, markup, text: textOf(markup) };
}

describe("loadDuplicates", () => {
  it("calls only dedup_candidates and get", async () => {
    const { calls } = await duplicates(ok("dedup-pairs.json"));
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.filter((call) => !ALLOWED.has(call.operation))).toEqual([]);
    expect(calls[0]).toEqual({ operation: DEDUP_CANDIDATES, input: { limit: PAIR_LIMIT } });
  });

  it("gets every ref in the pairs, at most two calls per pair", async () => {
    const { calls } = await duplicates(ok("dedup-pairs.json"));
    const pairs = (fixture("dedup-pairs.json") as { pairs: { ref_a: string; ref_b: string }[] })
      .pairs;
    const gets = calls.filter((call) => call.operation === GET).map((call) => call.input.ref);
    expect(new Set(gets)).toEqual(new Set(pairs.flatMap((pair) => [pair.ref_a, pair.ref_b])));
    expect(gets.length).toBeLessThanOrEqual(pairs.length * 2);
  });

  it("renders both titles linking to Item and the score as a similarity percentage", async () => {
    const { markup, text } = await duplicates(ok("dedup-pairs.json"));
    expect(text).toContain("Title of 31");
    expect(text).toContain("Title of 32");
    expect(text).toContain("94% similar");
    expect(text).toContain("83% similar");
    expect(markup).toContain(`href="/item?ref=${encodeURIComponent(REF_A)}"`);
  });

  it("renders no form and no button", async () => {
    for (const dedup of [ok("dedup-pairs.json"), ok("dedup-empty.json"), refusal("x")]) {
      const { markup } = await duplicates(dedup);
      expect(markup).not.toMatch(/<form[\s>]/);
      expect(markup).not.toMatch(/<button[\s>]/);
    }
  });

  it("renders no pairs as one honest empty state, not an error", async () => {
    const { calls, markup } = await duplicates(ok("dedup-empty.json"));
    expect(markup).toContain('data-state="no-duplicates"');
    expect(markup).not.toContain('data-state="error"');
    expect(calls.filter((call) => call.operation === GET)).toEqual([]);
  });

  it("shows unavailable for one title whose get refuses, and keeps the rest", async () => {
    const { text } = await duplicates(ok("dedup-pairs.json"), (input) =>
      input.ref === REF_A ? refusal("not_found") : titled(input),
    );
    expect(text).toContain("unavailable");
    expect(text).toContain("Title of 32");
    expect(text).toContain("94% similar");
  });

  it("falls back to the generic error when the candidate read itself fails", async () => {
    const { text } = await duplicates({ state: "unavailable" });
    expect(text).toContain("Memory is unavailable right now.");
  });
});
