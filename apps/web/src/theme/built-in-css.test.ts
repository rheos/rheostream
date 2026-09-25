import { describe, expect, it } from "vitest";

import { builtInThemeCss } from "./built-in-css";
import seedDarkJson from "./themes/seed-dark.json";

describe("builtInThemeCss", () => {
  it("compiles the seed theme", () => {
    expect(builtInThemeCss(seedDarkJson)).toContain("--rs-color-ground: #06181b;");
  });

  // Each theme below has every contract token with a grammar-valid value, so
  // compileTheme alone would accept it. Only the validateTheme refusal branch
  // stops it, which is what proves that branch is live.
  it("throws on a token the contract does not have", () => {
    const theme = {
      ...seedDarkJson,
      tokens: { ...seedDarkJson.tokens, "color.not-in-contract": "#000000" },
    };
    expect(() => builtInThemeCss(theme)).toThrow(
      "built-in theme failed validation: color.not-in-contract: unknown",
    );
  });

  it("throws on a contract version it does not know", () => {
    expect(() => builtInThemeCss({ ...seedDarkJson, contract: 2 })).toThrow(
      "built-in theme failed validation: contract: contract-version",
    );
  });

  it("throws on a theme with no name", () => {
    expect(() => builtInThemeCss({ ...seedDarkJson, name: "" })).toThrow(
      "built-in theme failed validation: name: invalid-value",
    );
  });
});
