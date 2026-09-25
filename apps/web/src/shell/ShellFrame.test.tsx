import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { CoreHealthResult } from "@/lib/core-health";
import { ShellFrame, type ShellAccount } from "@/shell/ShellFrame";

/**
 * A component-level regression test, not the carrier for the repository-wide
 * "no search input outside the memory Search screen" rule (that is
 * `scripts/check_search_boundary.py`). It exists so the frame itself never grows a
 * search box, whatever state it renders in.
 */

const HEALTHY: CoreHealthResult = { status: "ok", contractVersion: 7 };
const ACCOUNT: ShellAccount = {
  memberships: [
    { workspace_id: "ws-alpha", role: "owner" },
    { workspace_id: "ws-beta", role: "member" },
  ],
  activeWorkspaceId: "ws-alpha",
  switcherAction: "/switch-target",
  logoutAction: "/logout-target",
};

function render(health: CoreHealthResult, account: ShellAccount | null): string {
  return renderToStaticMarkup(
    <ShellFrame health={health} account={account}>
      <p>page body</p>
    </ShellFrame>,
  );
}

const SEARCH_INPUT = /type="search"|role="search"/;

describe("ShellFrame", () => {
  it.each([
    ["signed in", ACCOUNT],
    ["signed out", null],
  ] as const)("renders no search input when %s", (_label, account) => {
    expect(render(HEALTHY, account)).not.toMatch(SEARCH_INPUT);
    expect(render({ status: "unavailable" }, account)).not.toMatch(SEARCH_INPUT);
  });

  it("keeps the literal `contract v` that make demo greps for", () => {
    expect(render(HEALTHY, null)).toContain("core: ok (contract v7)");
  });

  it("says core is unavailable without dropping the page", () => {
    const html = render({ status: "unavailable" }, ACCOUNT);
    expect(html).toContain("core: unavailable");
    expect(html).not.toContain("contract v");
    expect(html).toContain("<p>page body</p>");
  });

  it("offers the switcher and logout only to a signed-in account", () => {
    const signedIn = render(HEALTHY, ACCOUNT);
    expect(signedIn).toMatch(/<form[^>]* action="\/logout-target"[^>]*>/);
    expect(/<form[^>]* action="\/logout-target"[^>]*>/.exec(signedIn)?.[0]).toContain(
      'method="post"',
    );
    expect(signedIn).toContain('id="rheo-workspace"');
    expect(signedIn).toContain('<option value="ws-alpha" selected="">');

    const signedOut = render(HEALTHY, null);
    expect(signedOut).not.toContain("Sign out");
    expect(signedOut).not.toContain("rheo-workspace");
  });

  it("renders the composed navigation with aria-current on the active entry only", () => {
    const html = renderToStaticMarkup(
      <ShellFrame
        health={HEALTHY}
        navigation={[
          { key: "m.one", label: "One", href: "/one-target", current: false },
          { key: "m.two", label: "Two", href: "/two-target", current: true },
        ]}
      >
        <p>page body</p>
      </ShellFrame>,
    );
    expect(html).toMatch(/<nav[^>]*aria-label="Workspace"/);
    expect(html).toMatch(/<a[^>]*href="\/one-target">One<\/a>/);
    expect(html).toMatch(/<a[^>]*href="\/two-target" aria-current="page">Two<\/a>/);
    expect(html.match(/aria-current/g)).toHaveLength(1);
  });

  it("renders no nav element when nothing is composed", () => {
    expect(render(HEALTHY, ACCOUNT)).not.toContain("<nav");
  });

  it("renders the page inside main", () => {
    expect(render(HEALTHY, null)).toMatch(/<main[^>]*><p>page body<\/p><\/main>/);
  });

  it.each([
    ["signed in", ACCOUNT],
    ["signed out", null],
  ] as const)(
    "opens with a skip link to main, ahead of every other focusable element, when %s",
    (_label, account) => {
      const html = renderToStaticMarkup(
        <ShellFrame
          health={HEALTHY}
          account={account}
          navigation={[{ key: "m.one", label: "One", href: "/one-target", current: false }]}
        >
          <p>page body</p>
        </ShellFrame>,
      );
      const firstFocusable = /<(a|button|select|input|textarea)\b[^>]*>/.exec(html)?.[0];
      expect(firstFocusable).toMatch(/^<a [^>]*href="#rheo-main"/);
      expect(html).toMatch(/<a [^>]*href="#rheo-main"[^>]*>Skip to content<\/a>/);
      const main = /<main\b[^>]*>/.exec(html)?.[0];
      expect(main).toContain('id="rheo-main"');
      expect(main).toContain('tabindex="-1"');
    },
  );
});
