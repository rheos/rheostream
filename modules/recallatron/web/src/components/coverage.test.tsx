import { describe, expect, it } from "vitest";

import { BrowseView } from "../screens/browse/view";
import { loadBrowse } from "../screens/browse/load";
import { loadSearch } from "../screens/search/load";
import { SearchView } from "../screens/search/view";
import { EMBEDDING_COVERAGE, ENTITY_LIST, RECALL } from "../operations";
import { fakeShell } from "../testing/fake-shell";
import { ok, refusal } from "../testing/fixtures";
import { render, textOf } from "../testing/markup";
import { CoverageFigure, loadCoverage } from "./coverage";

/**
 * Every shell here answers the coverage call with a real report, whatever the role, so
 * the member-role cases prove the screen does not ask, rather than relying on the
 * operation's own owner-only refusal to hide the figure.
 */
function shell(role: string, coverage = ok("coverage.json")) {
  return fakeShell(role, {
    [EMBEDDING_COVERAGE]: coverage,
    [ENTITY_LIST]: ok("entity-list.json"),
    [RECALL]: ok("recall-populated.json"),
  });
}

describe("coverage figure, owner", () => {
  it("shows how many memories meaning search can reach", async () => {
    const coverage = await loadCoverage(shell("owner").shell);
    expect(textOf(render(<CoverageFigure coverage={coverage} />))).toBe(
      "96 of 120 memories are searchable by meaning",
    );
  });

  it("says meaning search is off when no provider is configured", async () => {
    const coverage = await loadCoverage(shell("owner", ok("coverage-no-provider.json")).shell);
    expect(textOf(render(<CoverageFigure coverage={coverage} />))).toBe(
      "meaning search is off: no embedding provider is configured",
    );
  });

  it("appears on Browse and on Search", async () => {
    const owner = shell("owner").shell;
    const browse = render(<BrowseView state={await loadBrowse(owner, {})} />);
    const search = render(<SearchView state={await loadSearch(owner, { q: "garden" })} />);
    expect(browse).toContain("data-coverage=");
    expect(search).toContain("data-coverage=");
  });

  it("does not take the page down when the coverage read fails", async () => {
    const coverage = await loadCoverage(shell("owner", refusal("retention_unavailable")).shell);
    expect(coverage).toEqual({ state: "error" });
  });
});

describe("coverage figure, member", () => {
  it("makes no coverage call and renders nothing at all", async () => {
    const { shell: member, calls } = shell("member");
    const coverage = await loadCoverage(member);
    expect(calls).toEqual([]);
    expect(coverage).toBeNull();
    expect(render(<CoverageFigure coverage={coverage} />)).toBe("");
  });

  it("is absent from Browse and Search, not hidden", async () => {
    const { shell: member, calls } = shell("member");
    const browse = render(<BrowseView state={await loadBrowse(member, {})} />);
    const search = render(<SearchView state={await loadSearch(member, { q: "garden" })} />);
    for (const markup of [browse, search]) {
      expect(markup).not.toContain("data-coverage");
      expect(markup).not.toContain("searchable by meaning");
      expect(markup).not.toContain("meaning search is off");
    }
    expect(calls.map((call) => call.operation)).not.toContain(EMBEDDING_COVERAGE);
  });
});
