import { validateTheme } from "@rheo-stream/web-contract/theme";
import { describe, expect, it } from "vitest";
import { BUILT_IN_THEMES, THEME_CHOICES, themeId } from "./builtins";
import { contrastRatio } from "./contrast";
describe("built-in themes", () => {
  for (const choice of THEME_CHOICES) {
    const raw = BUILT_IN_THEMES[choice.id];
    const tokens: Record<string, string> = raw.tokens;
    it(`${choice.id} validates and clears text/control contrast`, () => {
      expect(validateTheme(raw, { origin: "built-in" }).ok).toBe(true);
      for (const bg of ["ground", "surface-1", "surface-2"]) {
        for (const fg of [
          "ink",
          "ink-dim",
          "ink-faint",
          "accent",
          "success",
          "warning",
          "danger",
          "info",
          "control-placeholder",
        ])
          expect(
            contrastRatio(tokens[`color.${fg}`]!, tokens[`color.${bg}`]!),
          ).toBeGreaterThanOrEqual(4.5);
        expect(
          contrastRatio(
            tokens["color.control-border"]!,
            tokens[`color.${bg}`]!,
          ),
        ).toBeGreaterThanOrEqual(3);
      }
      for (const bg of ["accent", "accent-strong"])
        expect(
          contrastRatio(tokens["color.on-accent"]!, tokens[`color.${bg}`]!),
        ).toBeGreaterThanOrEqual(4.5);
    });
    it(`${choice.id} keeps confirmation chrome fixed`, () => {
      for (const [key, value] of Object.entries(
        BUILT_IN_THEMES["seed-dark"].tokens,
      ))
        if (key.startsWith("chrome.")) expect(tokens[key]).toBe(value);
    });
  }
  it("keeps type and shape identical across Novadiem modes", () => {
    for (const [key, value] of Object.entries(
      BUILT_IN_THEMES["novadiem-dark"].tokens,
    ))
      if (!key.startsWith("color."))
        expect(
          (BUILT_IN_THEMES["novadiem-light"].tokens as Record<string, string>)[
            key
          ],
        ).toBe(value);
  });
  it("falls back on unknown stored theme names", () =>
    expect(themeId("untrusted")).toBe("greenstream-dark"));
});
