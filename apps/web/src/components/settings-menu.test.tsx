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

  it("is one closed button that names itself and the panel it opens", () => {
    expect(html).toMatch(/<button type="button"[^>]*aria-expanded="false"[^>]*aria-label="Settings"/);
    const controls = /aria-controls="([^"]+)"/.exec(html)?.[1];
    expect(controls).toBeTruthy();
    expect(html).toContain(`id="${controls}"`);
  });

  it("keeps the panel's contents in the markup while it is hidden", () => {
    expect(html).toMatch(/<div id="[^"]+" role="group" aria-label="Settings" class="[^"]*" hidden="">/);
    expect(html).toContain("<h2");
    expect(html).toContain(">Appearance</h2>");
    expect(html).toContain("<p>theme fields</p>");
  });
});
