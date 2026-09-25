import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { screens } from "./index";
import { DEDUP_CANDIDATES, ENTITY_LIST, GET, RECALL } from "./operations";
import { fakeShell } from "./testing/fake-shell";
import { field, ok } from "./testing/fixtures";
import { render, textOf } from "./testing/markup";

const { shell } = fakeShell("member", {
  [ENTITY_LIST]: ok("entity-list.json"),
  [RECALL]: ok("recall-populated.json"),
  [GET]: ok("memory-item.json"),
  [DEDUP_CANDIDATES]: ok("dedup-empty.json"),
});

describe("screens", () => {
  it("exports exactly the screen identifiers the module's routes name", () => {
    expect(Object.keys(screens).sort()).toEqual(["browse", "duplicates", "item", "search"]);
  });

  it.each([
    ["browse", {}, "Memory"],
    ["search", { q: "garden" }, "Sample garden project moves to raised beds"],
    ["item", { ref: field("memory-item.json", "ref") }, "Old watering note for the sample garden"],
    ["duplicates", {}, "Possible duplicates"],
  ] as const)("composes %s from its loader and its view", async (name, query, expected) => {
    const node = await screens[name]({ shell, query });
    expect(textOf(render(node as ReactElement))).toContain(expected);
  });
});
