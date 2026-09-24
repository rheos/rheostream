import { describe, expect, it } from "vitest";

import { contrastRatio, relativeLuminance } from "./contrast";

describe("contrastRatio", () => {
  it("gives the WCAG endpoints", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#57e0c6", "#57e0c6")).toBe(1);
  });

  it("is symmetric", () => {
    expect(contrastRatio("#06181b", "#eaf6f3")).toBe(contrastRatio("#eaf6f3", "#06181b"));
  });

  it("matches a published reference pair", () => {
    // #767676 on white is the well-known 4.54:1 AA boundary gray.
    expect(contrastRatio("#767676", "#ffffff")).toBeCloseTo(4.54, 2);
  });

  it("refuses a translucent or malformed color", () => {
    expect(() => relativeLuminance("rgba(234,246,243,.10)")).toThrow(/opaque/);
    expect(() => relativeLuminance("#fff")).toThrow(/opaque/);
  });
});
