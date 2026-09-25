import { describe, expect, it } from "vitest";

import { EMBEDDING_COVERAGE, RECALL } from "../../operations";
import { callsOutside, fakeShell, type Responder } from "../../testing/fake-shell";
import { field, fixture, ok, refusal } from "../../testing/fixtures";
import { render, textOf } from "../../testing/markup";
import { EMPTY_COPY, emptyKind, loadSearch, QUERY_MAX_LENGTH, RECALL_K } from "./load";
import { SearchView } from "./view";

const SEARCH_OPERATIONS = [RECALL, EMBEDDING_COVERAGE];

async function searchFor(
  q: string | undefined,
  recall: Responder = ok("recall-populated.json"),
  role = "member",
) {
  const { shell, calls } = fakeShell(role, {
    [RECALL]: recall,
    [EMBEDDING_COVERAGE]: ok("coverage.json"),
  });
  const state = await loadSearch(shell, { q });
  const markup = render(<SearchView state={state} />);
  return { state, calls, markup, text: textOf(markup) };
}

const EMPTY_FIXTURES = [
  ["recall-empty-degraded.json", "hybridDegraded"],
  ["recall-empty-dense-degraded.json", "denseDegraded"],
  ["recall-empty-lexical.json", "lexical"],
  ["recall-empty-dense.json", "dense"],
  ["recall-empty-hybrid.json", "hybrid"],
] as const;

describe("Search form", () => {
  it("is a plain GET form with the one search input", async () => {
    const { markup } = await searchFor(undefined);
    const form = markup.match(/<form[^>]*>/g) ?? [];
    const input = markup.match(/<input[^>]*>/g) ?? [];
    expect(form).toHaveLength(1);
    expect(input).toHaveLength(1);
    for (const attribute of ['role="search"', 'method="get"', 'action="/search"']) {
      expect(form[0]).toContain(attribute);
    }
    for (const attribute of ['type="search"', 'name="q"', 'maxLength="1000"']) {
      expect(input[0]).toContain(attribute);
    }
  });

  it("keeps the trimmed query in the input", async () => {
    const { markup } = await searchFor("  raised beds  ");
    expect(markup).toContain('value="raised beds"');
  });
});

describe("loadSearch", () => {
  it.each([
    ["a populated search", "garden", ok("recall-populated.json")],
    ["an empty search", "garden", ok("recall-empty-degraded.json")],
    ["a refused search", "garden", refusal("input_invalid")],
    ["the prompt", "   ", ok("recall-populated.json")],
  ] as const)("calls only recall and coverage for %s", async (_label, q, recall) => {
    for (const role of ["owner", "member"]) {
      const { calls } = await searchFor(q, recall, role);
      expect(callsOutside(calls, SEARCH_OPERATIONS)).toEqual([]);
    }
  });

  it.each([undefined, "", "   ", "\t\n "])(
    "makes no call and shows the prompt for %j",
    async (q) => {
      const { calls, markup } = await searchFor(q);
      expect(calls.filter((call) => call.operation === RECALL)).toEqual([]);
      expect(markup).toContain('data-state="prompt"');
    },
  );

  it("makes no call and shows a validation message when the trimmed query is too long", async () => {
    const { calls, markup } = await searchFor(` ${"a".repeat(QUERY_MAX_LENGTH + 1)} `);
    expect(calls.filter((call) => call.operation === RECALL)).toEqual([]);
    expect(markup).toContain('data-state="too-long"');
  });

  it("accepts a query of exactly the bound once trimmed", async () => {
    const q = "b".repeat(QUERY_MAX_LENGTH);
    const { calls } = await searchFor(`   ${q}   `);
    expect(calls.filter((call) => call.operation === RECALL)).toEqual([
      { operation: RECALL, input: { query: q, k: RECALL_K } },
    ]);
  });

  it("sends the trimmed query with k of ten", async () => {
    const { calls } = await searchFor("  raised beds ");
    expect(calls.find((call) => call.operation === RECALL)?.input).toEqual({
      query: "raised beds",
      k: RECALL_K,
    });
  });
});

describe("Search results", () => {
  it("renders each result's ref, kind, title, excerpt, UTC time and a link to Item", async () => {
    const { markup, text } = await searchFor("garden");
    const ref = field("recall-populated.json", "items", 0, "ref");
    expect(text).toContain(ref);
    expect(text).toContain("decision");
    expect(text).toContain("Sample garden project moves to raised beds");
    expect(text).toContain("The sample garden project will use four raised beds");
    expect(text).toContain("2026-03-02 10:00 UTC");
    // No occurred_at on the second item, so its recorded_at shows.
    expect(text).toContain("2026-02-20 14:05 UTC");
    expect(markup).toContain(`href="/item?ref=${encodeURIComponent(ref)}"`);
  });

  it("renders the provenance: the strategy and whether meaning search was available", async () => {
    const { text } = await searchFor("garden");
    expect(text).toContain("Strategy: hybrid");
    expect(text).toContain("Meaning search available: yes");
  });

  it("cuts a long body to its first 280 characters", async () => {
    const result = fixture("recall-populated.json") as { items: { body: string }[] };
    const long = "x".repeat(400);
    const items = result.items.map((item) => ({ ...item, body: long }));
    const { text } = await searchFor("garden", { state: "ok", result: { ...result, items } });
    expect(text).toContain(`${"x".repeat(280)}…`);
    expect(text).not.toContain("x".repeat(281));
  });

  it.each(EMPTY_FIXTURES)("renders %s as the %s empty state", async (name, kind) => {
    const { markup, text } = await searchFor("garden", ok(name));
    expect(markup).toContain(`data-state="empty-${kind}"`);
    expect(text).toContain(EMPTY_COPY[kind]);
  });

  it("renders five visibly distinct empty states", async () => {
    const texts = await Promise.all(
      EMPTY_FIXTURES.map(async ([name]) => {
        const { state } = await searchFor("garden", ok(name));
        if (state.results.state !== "empty") throw new Error(`${name} did not render empty`);
        return EMPTY_COPY[state.results.empty];
      }),
    );
    expect(new Set(texts).size).toBe(5);
  });

  it("keeps the degraded hybrid copy apart from the full hybrid copy", async () => {
    const degraded = await searchFor("garden", ok("recall-empty-degraded.json"));
    const full = await searchFor("garden", ok("recall-empty-hybrid.json"));
    expect(degraded.text).toContain(
      "No word matches. Meaning search isn't available in this workspace, so memories that match only by meaning can't be found.",
    );
    expect(full.text).toContain("Nothing matched by words or by meaning.");
    expect(degraded.text).not.toContain("Nothing matched by words or by meaning.");
    expect(full.text).not.toContain("No word matches.");
  });

  it("carries the five strings exactly as the spec words them", () => {
    expect(EMPTY_COPY).toEqual({
      hybridDegraded:
        "No word matches. Meaning search isn't available in this workspace, so memories that match only by meaning can't be found.",
      denseDegraded:
        "Meaning search isn't available in this workspace, so this search could not look at any memories.",
      lexical: "No word matches. This workspace searches by words only.",
      dense: "Nothing was close enough in meaning.",
      hybrid: "Nothing matched by words or by meaning.",
    });
  });

  it("treats a lexical search the same whether or not meaning search is available", () => {
    expect(emptyKind("lexical", true)).toBe("lexical");
    expect(emptyKind("lexical", false)).toBe("lexical");
  });

  it("falls back to the full hybrid copy for an unrecognised strategy", () => {
    expect(emptyKind("something-new", false)).toBe("hybrid");
    expect(emptyKind("something-new", true)).toBe("hybrid");
  });

  it("renders a degraded provenance with its strategy and no meaning search", async () => {
    const { text } = await searchFor("garden", ok("recall-empty-degraded.json"));
    expect(text).toContain("Strategy: hybrid");
    expect(text).toContain("Meaning search available: no");
  });

  it("falls back to the generic error on a refusal or an unavailable outcome", async () => {
    for (const outcome of [refusal("input_invalid"), { state: "unavailable" } as const]) {
      const { markup, text } = await searchFor("garden", outcome);
      expect(markup).toContain('data-state="error"');
      expect(text).toContain("Memory is unavailable right now.");
    }
  });
});

describe("Search arm counts", () => {
  // recall counts each arm before the permission walk, so a count can include memories
  // the caller cannot read. Distinctive values make any count that reaches the page
  // easy to spot; the fixture's shape is kept, only its arm values change.
  const LEXICAL = 7;
  const DENSE = 11;
  const STANDALONE_COUNT = new RegExp(`(?<![\\w.:-])(?:${LEXICAL}|${DENSE})(?![\\w:-])`);

  function withDistinctiveArms(name: string) {
    const result = fixture(name) as { provenance: Record<string, unknown> };
    return {
      state: "ok",
      result: {
        ...result,
        provenance: { ...result.provenance, arms: { lexical: LEXICAL, dense: DENSE } },
      },
    } as const;
  }

  function provenanceOf(markup: string): string {
    const line = /<p[^>]*data-provenance=""[^>]*>([\s\S]*?)<\/p>/.exec(markup);
    if (line?.[1] === undefined) throw new Error("no provenance line rendered");
    return textOf(line[1]);
  }

  const RENDERED: readonly (readonly [string, string])[] = [
    ["recall-populated.json", "results"],
    ...EMPTY_FIXTURES.map(([name, kind]) => [name, `empty-${kind}`] as const),
  ];

  // The exact key set of each state that reaches the view, so a count can't ride along
  // on a new field anywhere in it.
  const KEYS = {
    search: ["action", "coverage", "query", "results"],
    results: ["provenance", "rows", "state"],
    empty: ["empty", "provenance", "state"],
    provenance: ["denseAvailable", "strategy"],
    row: ["dateTime", "excerpt", "href", "kind", "ref", "title", "when"],
  };

  const keysOf = (value: object) => Object.keys(value).sort();

  /** React's static-markup escaping, to find the fixed empty copy in raw markup. */
  const escaped = (copy: string) =>
    copy.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

  it.each(RENDERED)("never renders an arm count for %s (%s)", async (name, rendered) => {
    const { state, markup, text } = await searchFor("garden", withDistinctiveArms(name));
    expect(markup).toContain(rendered === "results" ? "<ol" : `data-state="${rendered}"`);

    // The view state holds no count to render in the first place.
    expect(keysOf(state)).toEqual(KEYS.search);
    const results = state.results;
    if (results.state === "results") {
      expect(keysOf(results)).toEqual(KEYS.results);
      for (const row of results.rows) expect(keysOf(row)).toEqual(KEYS.row);
    } else if (results.state === "empty") {
      expect(keysOf(results)).toEqual(KEYS.empty);
    } else {
      throw new Error(`${name} did not reach a provenance state`);
    }
    expect(keysOf(results.provenance)).toEqual(KEYS.provenance);

    // Nor does the page, in its text or its raw markup, attributes included. The fixed
    // empty copy ("No word matches. …") is the one allowed use of "matches".
    const raw =
      results.state === "empty" ? markup.replace(escaped(EMPTY_COPY[results.empty]), "") : markup;
    expect(text).not.toMatch(/word matches:|meaning matches:/i);
    expect(raw).not.toMatch(/matches/i);
    expect(provenanceOf(markup)).not.toMatch(/\d/);
    expect(text).not.toMatch(STANDALONE_COUNT);
    expect(raw).not.toMatch(STANDALONE_COUNT);
  });
});
