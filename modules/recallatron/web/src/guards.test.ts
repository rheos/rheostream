import { describe, expect, it } from "vitest";

import {
  isDedupCandidates,
  isEmbeddingCoverageReport,
  isEntityItem,
  isEntityList,
  isEntityListRefusedBudget,
  isMemoryItem,
  isReadRefusedBudget,
  isReadRefusedWindow,
  isReadWindow,
  isRecallResult,
  isRefusalEnvelope,
} from "./guards";
import { fixture, fixtureNames } from "./testing/fixtures";

type Guard = (value: unknown) => boolean;

/** Every shared fixture and the guard that must accept it. */
const GUARD_FOR: Readonly<Record<string, Guard>> = {
  "recall-populated.json": isRecallResult,
  "recall-empty-hybrid.json": isRecallResult,
  "recall-empty-degraded.json": isRecallResult,
  "recall-empty-lexical.json": isRecallResult,
  "recall-empty-dense.json": isRecallResult,
  "recall-empty-dense-degraded.json": isRecallResult,
  "read-window.json": isReadWindow,
  "read-empty.json": isReadWindow,
  "entity-list.json": isEntityList,
  "entity-item.json": isEntityItem,
  "memory-item.json": isMemoryItem,
  "dedup-pairs.json": isDedupCandidates,
  "dedup-empty.json": isDedupCandidates,
  "coverage.json": isEmbeddingCoverageReport,
  "coverage-no-provider.json": isEmbeddingCoverageReport,
  "entity-list-refused-budget.json": isEntityListRefusedBudget,
  "read-refused-window.json": isReadRefusedWindow,
  "read-refused-budget.json": isReadRefusedBudget,
};

const REFUSALS = [
  "entity-list-refused-budget.json",
  "read-refused-window.json",
  "read-refused-budget.json",
];

function without(value: unknown, key: string): unknown {
  const copy = { ...(value as Record<string, unknown>) };
  delete copy[key];
  return copy;
}

describe("response guards", () => {
  it("name a guard for every fixture in the directory, and only those", () => {
    expect(fixtureNames()).toEqual(Object.keys(GUARD_FOR).sort());
  });

  it.each(Object.entries(GUARD_FOR))("accept %s", (name, guard) => {
    expect(guard(fixture(name))).toBe(true);
  });

  it.each(REFUSALS)("accept %s as a refusal envelope", (name) => {
    expect(isRefusalEnvelope(fixture(name))).toBe(true);
  });

  it("tell the two refusal codes apart", () => {
    expect(isReadRefusedWindow(fixture("read-refused-budget.json"))).toBe(false);
    expect(isReadRefusedBudget(fixture("read-refused-window.json"))).toBe(false);
    expect(isEntityListRefusedBudget(fixture("read-refused-window.json"))).toBe(false);
  });

  it.each(Object.keys(GUARD_FOR).filter((name) => !REFUSALS.includes(name)))(
    "do not read the result fixture %s as a refusal",
    (name) => {
      expect(isRefusalEnvelope(fixture(name))).toBe(false);
    },
  );

  it.each([
    ["recall-populated.json", isRecallResult, "provenance"],
    ["read-window.json", isReadWindow, "window_start"],
    ["entity-list.json", isEntityList, "items"],
    ["entity-item.json", isEntityItem, "mention_count"],
    ["memory-item.json", isMemoryItem, "recorded_at"],
    ["dedup-pairs.json", isDedupCandidates, "pairs"],
    ["coverage.json", isEmbeddingCoverageReport, "embedded"],
  ] as const)("refuse %s without its %s field", (name, guard, key) => {
    expect(guard(without(fixture(name), key))).toBe(false);
  });

  it("refuse a memory item whose link lost its display field", () => {
    const item = fixture("memory-item.json") as { links: Record<string, unknown>[] };
    const links = item.links.map((link) => without(link, "display"));
    expect(isMemoryItem({ ...item, links })).toBe(false);
  });

  it("refuse a recall item without its score", () => {
    const result = fixture("recall-populated.json") as { items: unknown[] };
    const items = result.items.map((each) => without(each, "score"));
    expect(isRecallResult({ ...result, items })).toBe(false);
  });

  it("refuse non-objects", () => {
    for (const value of [null, undefined, 1, "text", []]) {
      expect(isEntityList(value)).toBe(false);
      expect(isRefusalEnvelope(value)).toBe(false);
    }
  });
});
