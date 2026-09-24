import { describe, expect, it } from "vitest";

import { CONTRACT_V1, cssPropertyName } from "./contract";

describe("theme contract v1", () => {
  it("lists exactly the 43 tokens of the six groups", () => {
    const names = CONTRACT_V1.map((token) => token.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toHaveLength(43);

    const group = (prefix: string) => names.filter((name) => name.startsWith(prefix));
    const state = ["color.success", "color.warning", "color.danger", "color.info"];
    const controls = names.filter((name) => name.startsWith("color.control-"));
    const palette = group("color.").filter(
      (name) => !state.includes(name) && !controls.includes(name),
    );
    expect(palette).toHaveLength(10);
    expect(state.every((name) => names.includes(name))).toBe(true);
    expect(controls).toHaveLength(4);
    expect(group("font.")).toHaveLength(8);
    expect([...group("radius."), ...group("space."), ...group("size.")]).toHaveLength(11);
    expect(group("chrome.")).toHaveLength(6);
  });

  it("gives every token a known kind", () => {
    for (const token of CONTRACT_V1) {
      expect(["color", "length", "font-stack"]).toContain(token.kind);
    }
  });

  it("reserves exactly the chrome tokens", () => {
    const reserved = CONTRACT_V1.filter((token) => token.reserved).map((token) => token.name);
    expect(reserved).toEqual([
      "chrome.surface",
      "chrome.ink",
      "chrome.border",
      "chrome.danger",
      "chrome.font",
      "chrome.font-size",
    ]);
    const kinds = Object.fromEntries(
      CONTRACT_V1.filter((token) => token.reserved).map((token) => [token.name, token.kind]),
    );
    expect(kinds).toEqual({
      "chrome.surface": "color",
      "chrome.ink": "color",
      "chrome.border": "color",
      "chrome.danger": "color",
      "chrome.font": "font-stack",
      "chrome.font-size": "length",
    });
  });

  it("compiles a token name to its custom property", () => {
    expect(cssPropertyName("color.surface-1")).toBe("--rs-color-surface-1");
    expect(cssPropertyName("space.3")).toBe("--rs-space-3");
    expect(cssPropertyName("chrome.font-size")).toBe("--rs-chrome-font-size");
  });
});
