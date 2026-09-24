import { validateTheme } from "@rheo-stream/web-contract/theme";
import { describe, expect, it } from "vitest";

import { BUILT_IN_THEMES } from "./builtins";
import { contrastRatio } from "./contrast";
import seedDarkJson from "./themes/seed-dark.json";

const tokens: Readonly<Record<string, string>> = seedDarkJson.tokens;

// Transcribed from the site's DESIGN.md token table. These strings are the trace:
// the seed must carry them byte for byte.
const DESIGN_MD_VERBATIM: ReadonlyArray<readonly [token: string, value: string]> = [
  ["color.ground", "#06181b"],
  ["color.surface-1", "#0a2327"],
  ["color.ink", "#eaf6f3"],
  ["color.ink-dim", "#9fc2bd"],
  ["color.ink-faint", "#6e918d"],
  ["color.accent", "#57e0c6"],
  ["color.accent-strong", "#83f0dc"],
  ["color.hairline", "rgba(234,246,243,.10)"],
];

describe("seed-dark: DESIGN.md trace", () => {
  it.each(DESIGN_MD_VERBATIM)("%s is DESIGN.md's value verbatim", (token, value) => {
    expect(tokens[token]).toBe(value);
  });
});

describe("seed-dark: registry and contract", () => {
  it("is the registry's seed-dark entry", () => {
    expect(BUILT_IN_THEMES["seed-dark"]).toBe(seedDarkJson);
  });

  it("validates as a built-in v1 theme", () => {
    const result = validateTheme(BUILT_IN_THEMES["seed-dark"], { origin: "built-in" });
    expect(result.ok ? [] : result.errors).toEqual([]);
  });

  it("is labelled as the seed default, not the final canon", () => {
    const description = seedDarkJson.description.toLowerCase();
    expect(description).toContain("the seed default that run 2d replaces or extends");
    expect(description).toContain("not the final visual canon");
  });

  it("puts the vendored brand font first in font.sans", () => {
    expect(tokens["font.sans"].startsWith("var(--rs-font-brand),")).toBe(true);
  });
});

const AA_TEXT = 4.5;
const AA_NON_TEXT = 3;

const SURFACES = ["color.ground", "color.surface-1", "color.surface-2"] as const;

function pairs(
  foregrounds: readonly string[],
  backgrounds: readonly string[],
): Array<[string, string]> {
  return foregrounds.flatMap((fg) => backgrounds.map((bg): [string, string] => [fg, bg]));
}

function ratio(fg: string, bg: string): number {
  return contrastRatio(tokens[fg], tokens[bg]);
}

describe("seed-dark: contrast floor", () => {
  it.each(pairs(["color.ink", "color.ink-dim", "color.ink-faint"], SURFACES))(
    "%s on %s clears AA text contrast",
    (fg, bg) => {
      expect(ratio(fg, bg)).toBeGreaterThanOrEqual(AA_TEXT);
    },
  );

  it.each(
    pairs(
      [
        "color.accent",
        "color.accent-strong",
        "color.success",
        "color.warning",
        "color.danger",
        "color.info",
      ],
      SURFACES,
    ),
  )("derived text color %s on %s clears AA text contrast", (fg, bg) => {
    expect(ratio(fg, bg)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each([
    ["color.on-accent", "color.accent"],
    ["color.on-accent", "color.accent-strong"],
    ["color.ink", "color.control-bg"],
    ["color.control-placeholder", "color.control-bg"],
    ["chrome.ink", "chrome.surface"],
    ["chrome.danger", "chrome.surface"],
  ])("%s on %s clears AA text contrast", (fg, bg) => {
    expect(ratio(fg, bg)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each(
    pairs(
      ["color.control-border", "color.control-focus"],
      [...SURFACES, "color.control-bg"],
    ),
  )("%s against %s clears non-text contrast", (fg, bg) => {
    expect(ratio(fg, bg)).toBeGreaterThanOrEqual(AA_NON_TEXT);
  });
});
