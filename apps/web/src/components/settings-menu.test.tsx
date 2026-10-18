import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SettingsMenu, SettingsSection } from "./settings-menu";

describe("SettingsMenu (#408)", () => {
  const html = renderToStaticMarkup(
    <SettingsMenu>
      <SettingsSection title="Appearance">
        <p>theme fields</p>
      </SettingsSection>
    </SettingsMenu>,
  );

  it("is a closed native disclosure, so it opens without scripts", () => {
    expect(html).toMatch(/^<details class="[^"]*">/);
    expect(html).not.toContain("<details open");
    expect(html).toMatch(/<summary [^>]*title="Settings">/);
  });

  it("names its button for assistive technology", () => {
    expect(html).toMatch(/<summary[^>]*>.*<span class="[^"]*">Settings<\/span><\/summary>/);
  });

  it("holds its sections in the panel", () => {
    expect(html).toContain(">Appearance</h2>");
    expect(html).toContain("<p>theme fields</p>");
  });
});
