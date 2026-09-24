import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { NOT_FOUND } from "../../guards";
import { EMBEDDING_COVERAGE, ENTITY_GET, ENTITY_LIST, READ } from "../../operations";
import { fakeShell, type Responder } from "../../testing/fake-shell";
import { field, fixture, ok, refusal, refused } from "../../testing/fixtures";
import { render, textOf } from "../../testing/markup";
import { LIST_LIMIT, LIST_RETRY_LIMIT, loadBrowse, READ_CONTEXT } from "./load";
import { BrowseView } from "./view";

const ENTITY = field("entity-item.json", "ref");
const AROUND = field("read-window.json", "items", 2, "ref");

function shellWith(overrides: Readonly<Record<string, Responder>> = {}, role = "member") {
  return fakeShell(role, {
    [ENTITY_LIST]: ok("entity-list.json"),
    [ENTITY_GET]: ok("entity-item.json"),
    [READ]: ok("read-window.json"),
    [EMBEDDING_COVERAGE]: ok("coverage.json"),
    ...overrides,
  });
}

async function browseText(
  query: Readonly<Record<string, string | undefined>>,
  overrides: Readonly<Record<string, Responder>> = {},
) {
  const { shell, calls } = shellWith(overrides);
  const state = await loadBrowse(shell, query);
  const markup = render(<BrowseView state={state} />);
  return { state, calls, markup, text: textOf(markup) };
}

describe("loadBrowse", () => {
  it("asks for the list at the operation's maximum, never its default", async () => {
    const { calls } = await browseText({});
    const list = calls.filter((call) => call.operation === ENTITY_LIST);
    expect(list).toEqual([{ operation: ENTITY_LIST, input: { limit: LIST_LIMIT } }]);
  });

  it("passes the kind filter through", async () => {
    const { calls } = await browseText({ kind: "project" });
    expect(calls.find((call) => call.operation === ENTITY_LIST)?.input).toEqual({
      kind: "project",
      limit: LIST_LIMIT,
    });
  });

  it("reads targetless, with the entity as the container, when no position is chosen", async () => {
    const { calls } = await browseText({ entity: ENTITY });
    const reads = calls.filter((call) => call.operation === READ);
    expect(reads).toEqual([
      { operation: READ, input: { container_ref: ENTITY, context: READ_CONTEXT } },
    ]);
  });

  it("re-centres with a targeted read over the same entity container", async () => {
    const { calls } = await browseText({ entity: ENTITY, around: AROUND });
    const reads = calls.filter((call) => call.operation === READ);
    expect(reads).toEqual([
      {
        operation: READ,
        input: { container_ref: ENTITY, target_ref: AROUND, context: READ_CONTEXT },
      },
    ]);
  });

  it("makes no entity or read call when no entity is selected", async () => {
    const { calls, markup } = await browseText({});
    expect(calls.map((call) => call.operation)).not.toContain(READ);
    expect(calls.map((call) => call.operation)).not.toContain(ENTITY_GET);
    expect(markup).toContain('data-state="no-entity-selected"');
  });
});

describe("Browse list pane", () => {
  it("renders the fixture's entities, each linking to its selection", async () => {
    const { text, markup } = await browseText({ entity: ENTITY });
    expect(text).toContain("Avery Example");
    expect(text).toContain("Sample Garden Project");
    expect(markup).toContain('aria-current="true"');
  });

  it("says when the list stops at the operation's maximum", async () => {
    const one = (fixture("entity-list.json") as { items: unknown[] }).items[0];
    const full = { items: Array.from({ length: LIST_LIMIT }, () => one) };
    const { markup, text } = await browseText(
      {},
      { [ENTITY_LIST]: { state: "ok", result: full } },
    );
    expect(markup).toContain('data-state="list-truncated"');
    expect(text).toContain(`Showing the first ${LIST_LIMIT} entities alphabetically`);
  });

  it("retries once with a limit of ten after a reference-budget refusal, and shows those ten", async () => {
    const { calls, markup, text } = await browseText(
      {},
      {
        [ENTITY_LIST]: (input) =>
          input.limit === LIST_LIMIT ? refused("entity-list-refused-budget.json") : ok("entity-list.json"),
      },
    );
    const limits = calls
      .filter((call) => call.operation === ENTITY_LIST)
      .map((call) => call.input.limit);
    expect(limits).toEqual([LIST_LIMIT, LIST_RETRY_LIMIT]);
    expect(markup).toContain('data-state="list-reduced"');
    expect(text).toContain("The full entity list is too large to load at once");
    expect(text).toContain("Avery Example");
  });

  it("renders list-too-large, with the kind links still shown, when the retry is refused too", async () => {
    const { calls, markup, text } = await browseText(
      {},
      { [ENTITY_LIST]: refused("entity-list-refused-budget.json") },
    );
    expect(calls.filter((call) => call.operation === ENTITY_LIST)).toHaveLength(2);
    expect(markup).toContain('data-state="list-too-large"');
    expect(text).toContain("The entity list is too large to load. Filter by kind to narrow it.");
    expect(markup).toContain('href="/browse?kind=person"');
    expect(text).not.toContain("Avery Example");
  });

  it("falls back to the generic error for any other list refusal, without retrying", async () => {
    const { calls, markup } = await browseText({}, { [ENTITY_LIST]: refusal("input_invalid") });
    expect(calls.filter((call) => call.operation === ENTITY_LIST)).toHaveLength(1);
    expect(markup).toContain('data-state="error"');
  });
});

describe("Browse detail pane", () => {
  it("shows the entity's fields, its readable count and its window, each item linking to Item", async () => {
    const { markup, text } = await browseText({ entity: ENTITY });
    expect(text).toContain("Sample Garden Project");
    expect(text).toContain(ENTITY);
    expect(text).toContain(field("entity-item.json", "backing_ref"));
    expect(text).toContain("12 memories you can read");
    expect(text).toContain("Seed order placed for the sample garden");
    expect(markup).toContain(`href="/item?ref=${encodeURIComponent(AROUND)}"`);
  });

  it("links Older and Newer as targeted re-centres when the window has more on either side", async () => {
    const { markup } = await browseText({ entity: ENTITY });
    const first = field("read-window.json", "items", 0, "ref");
    const last = field("read-window.json", "items", 4, "ref");
    const query = (ref: string) =>
      new URLSearchParams({ entity: ENTITY, around: ref }).toString().replace(/&/g, "&amp;");
    expect(markup).toContain(`href="/browse?${query(first)}">Older</a>`);
    expect(markup).toContain(`href="/browse?${query(last)}">Newer</a>`);
  });

  it("renders the empty window as an empty state, not an error", async () => {
    const { markup, text } = await browseText(
      { entity: ENTITY },
      { [READ]: ok("read-empty.json") },
    );
    expect(markup).toContain('data-state="empty-window"');
    expect(text).toContain("No memories recorded around this entity yet.");
    expect(markup).not.toContain('data-state="error"');
    expect(text).not.toContain("Older");
  });

  it("renders entity-too-connected from the read's reference-budget refusal", async () => {
    const { markup, text } = await browseText(
      { entity: ENTITY },
      { [READ]: refused("read-refused-budget.json") },
    );
    expect(markup).toContain('data-state="entity-too-connected"');
    expect(text).toContain("This entity's memories link to too many records to show here.");
    expect(text).not.toContain("Seed order placed");
  });

  it("renders entity-too-connected from entity.get's reference-budget refusal", async () => {
    const { markup } = await browseText(
      { entity: ENTITY },
      { [ENTITY_GET]: refused("read-refused-budget.json") },
    );
    expect(markup).toContain('data-state="entity-too-connected"');
  });

  it("renders entity-too-large from the read's window-scan refusal, pointing at Search", async () => {
    const { markup, text } = await browseText(
      { entity: ENTITY },
      { [READ]: refused("read-refused-window.json") },
    );
    expect(markup).toContain('data-state="entity-too-large"');
    expect(text).toContain(
      "This entity is mentioned by more memories than one view can scan. Use Search to find a specific memory.",
    );
    expect(markup).toContain('href="/search"');
    expect(text).not.toContain("Seed order placed");
  });

  it("keeps the three named states distinct from each other and from the generic error", async () => {
    const states = await Promise.all([
      browseText({}, { [ENTITY_LIST]: refused("entity-list-refused-budget.json") }),
      browseText({ entity: ENTITY }, { [READ]: refused("read-refused-budget.json") }),
      browseText({ entity: ENTITY }, { [READ]: refused("read-refused-window.json") }),
      browseText({ entity: ENTITY }, { [READ]: { state: "unavailable" } }),
    ]);
    const detailTexts = states.map(({ state }) =>
      state.detail.state === "none" ? state.list.state : state.detail.state,
    );
    expect(new Set(detailTexts).size).toBe(4);
  });

  it("says an entity is unavailable when it is not found", async () => {
    const { markup, text } = await browseText(
      { entity: ENTITY },
      { [ENTITY_GET]: refusal(NOT_FOUND), [READ]: refusal(NOT_FOUND) },
    );
    expect(markup).toContain('data-state="entity-not-found"');
    expect(text).toContain("This entity is unavailable.");
  });

  it("falls back to the generic error when an operation is unavailable", async () => {
    const { text } = await browseText({ entity: ENTITY }, { [ENTITY_GET]: { state: "unavailable" } });
    expect(text).toContain("Memory is unavailable right now.");
  });

  it("renders only the entity's own five fields, even when the response carries more", async () => {
    const entity = { ...(fixture("entity-item.json") as object), extra_score: 0.4242 };
    const { text } = await browseText(
      { entity: ENTITY },
      { [ENTITY_GET]: { state: "ok", result: entity } },
    );
    expect(text).toContain("Sample Garden Project");
    expect(text).not.toContain("0.4242");
    expect(text).not.toContain("extra_score");
  });
});

describe("Browse source", () => {
  const DIR = fileURLToPath(new URL(".", import.meta.url));
  // The fields and affordances the predecessor's entity screen had and this module's
  // entity does not. Built from parts so this scan does not flag its own file.
  const FORBIDDEN = new RegExp(["confid", "ence|confirm", "ed|relat", "ion"].join(""), "i");

  it("has no reference to a field the entity model does not have", () => {
    const files = readdirSync(DIR, { recursive: true, encoding: "utf8" }).filter((name) =>
      /\.(?:ts|tsx|css)$/.test(name),
    );
    expect(files).toContain("view.tsx");
    const offenders = files
      .filter((name) => FORBIDDEN.test(readFileSync(join(DIR, name), "utf8")));
    expect(offenders).toEqual([]);
  });

  it("scans with a pattern that catches each forbidden word", () => {
    for (const word of ["Confid" + "ence", "confir" + "med", "relati" + "on"]) {
      expect(FORBIDDEN.test(`entity.${word}`)).toBe(true);
    }
  });
});
