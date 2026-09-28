import { describe, expect, it } from "vitest";

import { HISTORY_GET } from "../../operations";
import { fakeShell } from "../../testing/fake-shell";
import { field, ok } from "../../testing/fixtures";
import { render, textOf } from "../../testing/markup";
import { HistoryItemView, loadHistoryItem } from "./screen";

const ID = field("history-item.json", "id");

describe("historical record", () => {
  it("shows the full source body and its unconfirmed status to the owner", async () => {
    const { shell, calls } = fakeShell("owner", { [HISTORY_GET]: ok("history-item.json") });
    const state = await loadHistoryItem(shell, { id: ID });
    const output = textOf(render(<HistoryItemView state={state} />));
    expect(calls).toEqual([{ operation: HISTORY_GET, input: { id: ID } }]);
    expect(output).toContain("A synthetic, unconfirmed way to label garden trays.");
    expect(output).toContain("unconfirmed");
    expect(output).toContain("not an accepted memory");
  });

  it("never calls the owner-only operation for a member", async () => {
    const { shell, calls } = fakeShell("member", { [HISTORY_GET]: ok("history-item.json") });
    const state = await loadHistoryItem(shell, { id: ID });
    const output = textOf(render(<HistoryItemView state={state} />));
    expect(calls).toEqual([]);
    expect(output).toContain("unavailable");
  });
});
