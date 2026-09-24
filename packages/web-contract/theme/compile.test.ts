import { describe, expect, it } from "vitest";

import { compileTheme } from "./compile";
import { CONTRACT_V1, cssPropertyName } from "./contract";
import { builtInTheme, thirdPartyTheme } from "./testing";

const CHROME = CONTRACT_V1.filter((token) => token.reserved).map((token) => token.name);
const NON_CHROME = CONTRACT_V1.filter((token) => !token.reserved).map((token) => token.name);

/** The declared value of `token`'s custom property in `css`, or null. */
function declared(css: string, token: string): string | null {
  const line = css.split("\n").find((text) => text.trim().startsWith(`${cssPropertyName(token)}:`));
  return line === undefined ? null : line.slice(line.indexOf(":") + 1).trim().replace(/;$/, "");
}

describe("compileTheme", () => {
  it("compiles a base theme alone, every token plus color-scheme", () => {
    const base = builtInTheme({ "color.accent": "#57e0c6", "chrome.ink": "#ffffff" });
    const css = compileTheme(base);
    for (const name of [...CHROME, ...NON_CHROME]) {
      expect(declared(css, name)).toBe(base.tokens[name]);
    }
    expect(css).toContain("color-scheme: dark;");
  });

  it("layers the overlay over every non-chrome token and the scheme", () => {
    const base = builtInTheme();
    const overlay = thirdPartyTheme({ "color.accent": "#abcdef", "space.3": "9px" });
    const css = compileTheme(base, overlay);
    expect(declared(css, "color.accent")).toBe("#abcdef");
    expect(declared(css, "space.3")).toBe("9px");
    expect(css).toContain("color-scheme: light;");
  });

  it("takes chrome values from base even when an overlay carries them (AC 5)", () => {
    // Hypothetical: an overlay that bypassed validateTheme and sets every chrome
    // token to something else. The exclusion must hold in compileTheme itself.
    const base = builtInTheme({
      "chrome.surface": "#0a0a0a",
      "chrome.ink": "#f0f0f0",
      "chrome.border": "#303030",
      "chrome.danger": "#d04040",
      "chrome.font": "system-ui",
      "chrome.font-size": "14px",
    });
    const hostile: Record<string, string> = {};
    for (const name of CHROME) hostile[name] = name === "chrome.font-size" ? "99px" : "#ff00ff";
    hostile["chrome.font"] = "Hostile";
    const overlay = thirdPartyTheme(hostile);

    const css = compileTheme(base, overlay);
    for (const name of CHROME) {
      expect(declared(css, name)).toBe(base.tokens[name]);
    }
    expect(css).not.toContain("#ff00ff");
    expect(css).not.toContain("99px");
    expect(css).not.toContain("Hostile");
  });

  it("emits the chrome rule first and closes it before any other declaration", () => {
    const css = compileTheme(builtInTheme(), thirdPartyTheme());
    const chromeClose = css.indexOf("}");
    const lastChrome = Math.max(...CHROME.map((name) => css.indexOf(`${cssPropertyName(name)}:`)));
    const firstOther = Math.min(
      ...NON_CHROME.map((name) => css.indexOf(`${cssPropertyName(name)}:`)),
    );
    expect(css.startsWith(":root {")).toBe(true);
    expect(lastChrome).toBeGreaterThan(0);
    expect(lastChrome).toBeLessThan(chromeClose);
    expect(chromeClose).toBeLessThan(firstOther);
    expect(css.slice(chromeClose)).toMatch(/^}\n:root \{/);
    expect(css.indexOf("color-scheme")).toBeGreaterThan(chromeClose);
  });

  it("fails loudly on a base missing a token instead of dropping the property", () => {
    const base = builtInTheme();
    const tokens: Record<string, string> = { ...base.tokens };
    delete tokens["chrome.ink"];
    expect(() => compileTheme({ ...base, tokens })).toThrow(/chrome\.ink/);
  });
});
