import { describe, expect, it } from "vitest";

import { CONTRACT_V1 } from "./contract";
import { builtInTheme, thirdPartyTheme } from "./testing";
import { validateTheme } from "./validate";

const BUILT_IN = { origin: "built-in" } as const;
const THIRD_PARTY = { origin: "third-party" } as const;

function withoutToken(theme: ReturnType<typeof builtInTheme>, name: string) {
  const tokens: Record<string, string> = { ...theme.tokens };
  delete tokens[name];
  return { ...theme, tokens };
}

/** Validate one token value on an otherwise valid built-in theme. */
function checkValue(token: string, value: string) {
  return validateTheme(builtInTheme({ [token]: value }), BUILT_IN);
}

const refusedAs = (token: string, reason: string) => ({
  ok: false,
  errors: [{ token, reason }],
});

const fieldRefused = (field: string, reason: string) => ({
  ok: false,
  errors: [{ token: null, field, reason }],
});

describe("validateTheme — shape", () => {
  it("accepts a complete built-in theme and returns it typed", () => {
    const theme = builtInTheme();
    expect(validateTheme(theme, BUILT_IN)).toEqual({ ok: true, theme });
  });

  it("accepts a chrome-free third-party theme and keeps its description", () => {
    const theme = { ...thirdPartyTheme(), description: "A synthetic sample." };
    expect(validateTheme(theme, THIRD_PARTY)).toEqual({ ok: true, theme });
  });

  it("returns a fresh copy, not the input object", () => {
    const theme = builtInTheme();
    const result = validateTheme(theme, BUILT_IN);
    if (!result.ok) throw new Error("expected a valid theme");
    expect(result.theme).not.toBe(theme);
    expect(result.theme.tokens).not.toBe(theme.tokens);
  });

  it("refuses any contract other than 1 with one contract-version error", () => {
    const refused = fieldRefused("contract", "contract-version");
    expect(validateTheme({ ...builtInTheme(), contract: 2 }, BUILT_IN)).toEqual(refused);
    expect(validateTheme({ ...builtInTheme(), contract: "1" }, BUILT_IN)).toEqual(refused);
    expect(validateTheme(null, BUILT_IN)).toEqual(refused);
  });

  it("names the missing token (AC 3)", () => {
    const theme = withoutToken(thirdPartyTheme(), "color.accent");
    expect(validateTheme(theme, THIRD_PARTY)).toEqual(refusedAs("color.accent", "missing"));
  });

  it("requires every chrome token of a built-in theme", () => {
    const theme = withoutToken(builtInTheme(), "chrome.border");
    expect(validateTheme(theme, BUILT_IN)).toEqual(refusedAs("chrome.border", "missing"));
  });

  it("refuses a key that is not on the contract as unknown", () => {
    const theme = builtInTheme({ "color.accnet": "#123456" });
    expect(validateTheme(theme, BUILT_IN)).toEqual(refusedAs("color.accnet", "unknown"));
  });

  it("names an unknown top-level key", () => {
    const theme = { ...builtInTheme(), author: "Sample Author" };
    expect(validateTheme(theme, BUILT_IN)).toEqual(fieldRefused("author", "unknown"));
  });

  it.each([
    ["id", { id: "" }],
    ["name", { name: 3 }],
    ["description", { description: false }],
    ["scheme", { scheme: "dark; }" }],
    ["tokens", { tokens: ["#123456"] }],
  ])("names the invalid theme-level field %s", (field, patch) => {
    const theme = { ...builtInTheme(), ...patch };
    expect(validateTheme(theme, BUILT_IN)).toEqual(fieldRefused(field, "invalid-value"));
  });
});

describe("validateTheme — reserved chrome (AC 5)", () => {
  it.each(CONTRACT_V1.filter((token) => token.reserved).map((token) => token.name))(
    "refuses a third-party theme that sets %s",
    (name) => {
      const builtIn = builtInTheme();
      const theme = thirdPartyTheme({ [name]: builtIn.tokens[name] ?? "" });
      expect(validateTheme(theme, THIRD_PARTY)).toEqual(refusedAs(name, "reserved"));
    },
  );
});

describe("validateTheme — color grammar", () => {
  it.each([
    "#06181b",
    "#06181B",
    "#06181b80",
    "rgb(1,2,3)",
    "rgba( 1 , 2 , 3 , 0.5 )",
    "rgba(1,\t2,\n3)",
  ])("accepts %j", (value) => {
    expect(checkValue("color.ground", value).ok).toBe(true);
  });

  it("pins the seed alpha string verbatim, plus 0.1 and a bare integer alpha", () => {
    expect(checkValue("color.hairline", "rgba(234,246,243,.10)").ok).toBe(true);
    expect(checkValue("color.hairline", "rgba(234,246,243,0.1)").ok).toBe(true);
    expect(checkValue("color.hairline", "rgba(234,246,243,1)").ok).toBe(true);
  });

  it("refuses a planted CSS-injection attempt", () => {
    expect(checkValue("color.hairline", "rgba(1,2,3,.1);}")).toEqual(
      refusedAs("color.hairline", "invalid-value"),
    );
  });

  it.each([
    "rgba(256,0,0,.1)",
    "rgba(1,2,3,1.5)",
    "RGBA(1,2,3,.1)",
    "#12345",
    "#1234567",
    "red",
    "#123456;}",
    "rgb(1, 2,3)",
    "rgb(1, 2,3)",
  ])("refuses %j", (value) => {
    expect(checkValue("color.ground", value)).toEqual(refusedAs("color.ground", "invalid-value"));
  });

  it("refuses a non-string value", () => {
    const theme = { ...builtInTheme(), tokens: { ...builtInTheme().tokens, "color.ink": 7 } };
    expect(validateTheme(theme, BUILT_IN)).toEqual(refusedAs("color.ink", "invalid-value"));
  });
});

describe("validateTheme — length grammar", () => {
  it.each(["0px", "1.5rem", "0.875em", "12px"])("accepts %s", (value) => {
    expect(checkValue("space.2", value).ok).toBe(true);
  });

  it.each(["12", "12pt", "1.px", "calc(1px)", "4px;}"])("refuses %s", (value) => {
    expect(checkValue("space.2", value)).toEqual(refusedAs("space.2", "invalid-value"));
  });

  it("allows a negative length only on font.tracking-display", () => {
    expect(checkValue("font.tracking-display", "-0.02em").ok).toBe(true);
    expect(checkValue("radius.md", "-2px")).toEqual(refusedAs("radius.md", "invalid-value"));
  });
});

describe("validateTheme — font-stack grammar", () => {
  it.each([
    "system-ui, sans-serif",
    "Segoe UI, Helvetica Neue, Arial",
    "'Inter Tight', \"IBM Plex Mono\", monospace",
    "var(--rs-font-brand), system-ui",
  ])("accepts %s", (value) => {
    expect(checkValue("font.sans", value).ok).toBe(true);
  });

  it.each([
    '"Foo',
    "'Foo",
    "Inter,\nsans-serif",
    "Inter,\rsans-serif",
    "'Inter\nTight'",
    "'Inter\fTight'",
    "Inter,\fsans-serif",
    "Inter, sans-serif",
    "Inter; color: red",
    "Inter } :root {",
    "var(--rs-anything)",
    "url(x)",
    "'</style>'",
    "Inter\\,Arial",
    "",
    "Inter,",
  ])("refuses %j", (value) => {
    expect(checkValue("font.sans", value)).toEqual(refusedAs("font.sans", "invalid-value"));
  });
});
