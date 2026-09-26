import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LogoutForm } from "@/components/logout-form";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";

/** Behavior regression tests for the two header controls after their restyle. */

describe("LogoutForm", () => {
  it("is a plain POST form to the given same-host action", () => {
    const html = renderToStaticMarkup(<LogoutForm action="/logout-target" />);
    const form = /<form[^>]*>/.exec(html)?.[0] ?? "";
    expect(form).toContain('action="/logout-target"');
    expect(form).toContain('method="post"');
    expect(html).toContain('<button class="');
    expect(html).toContain('type="submit">Sign out</button>');
  });
});

describe("WorkspaceSwitcher", () => {
  const html = renderToStaticMarkup(
    <WorkspaceSwitcher
      action="/switch-target"
      memberships={[
        { workspace_id: "ws-alpha", display_name: "Alpha Desk", role: "owner" },
        { workspace_id: "ws-beta", display_name: "Beta Desk", role: "member" },
      ]}
      activeWorkspaceId="ws-beta"
    />,
  );

  it("keeps the labelled select with every membership and the active one selected", () => {
    expect(html).toContain('for="rheo-workspace"');
    expect(html).toContain('<select id="rheo-workspace" name="target_workspace_id">');
    expect(html).toContain('<option value="ws-alpha">Alpha Desk (owner)</option>');
    expect(html).toContain('<option value="ws-beta" selected="">Beta Desk (member)</option>');
    expect(html).toContain('<button type="submit">Switch</button>');
  });

  it("labels each option with the display name, keeping the id as its value only (#115)", () => {
    const options = html.match(/<option[^>]*>[^<]*<\/option>/g) ?? [];
    expect(options).toHaveLength(2);
    for (const option of options) {
      const label = />([^<]*)</.exec(option)?.[1] ?? "";
      expect(label).not.toContain("ws-");
    }
  });

  it("puts no workspace identifier or action in the form's markup", () => {
    // The target travels in a JSON body, so the form has no action or method a
    // plain submission could fall back to.
    const form = /<form[^>]*>/.exec(html)?.[0] ?? "";
    expect(form).not.toContain("action=");
    expect(form).not.toContain("method=");
  });
});
