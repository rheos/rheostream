import {
  CONTRACT_V1,
  CONTRACT_VERSION,
  type ContractToken,
  type TokenKind,
} from "./contract";

export type ThemeErrorReason =
  | "contract-version"
  | "missing"
  | "unknown"
  | "invalid-value"
  | "reserved";

/** `token` is the contract token name, or `null` for a theme-level field. */
export interface ThemeError {
  token: string | null;
  reason: ThemeErrorReason;
}

export type ThemeValidation = { ok: true } | { ok: false; errors: ThemeError[] };

export interface ValidateOptions {
  /**
   * Built-in-ness is the caller's knowledge (a registry of built-in themes), never
   * a field the theme file can claim for itself.
   */
  origin: "built-in" | "third-party";
}

const THEME_KEYS = new Set(["contract", "id", "name", "description", "scheme", "tokens"]);
const TRACKING_TOKEN = "font.tracking-display";

const HEX_COLOR = /^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$/;
// Whole-value and case-sensitive, exactly as the contract states it. The alpha
// alternative `0?\.\d+` admits `.10`, the form the seed palette is written in.
const RGB_COLOR =
  /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(0|1|0?\.\d+|1\.0+)\s*)?\)$/;
const LENGTH = /^-?\d+(\.\d+)?(px|rem|em)$/;

const FONT_BRAND_VAR = "var(--rs-font-brand)";
const BARE_FAMILY = /^[A-Za-z0-9_-]+(?: [A-Za-z0-9_-]+)*$/;
// A closed quote on both ends, and inside it no line break, no copy of the
// opening quote and none of `; { } ( ) \ <`.
const QUOTED_FAMILY = /^(?:'[^'\r\n;{}()\\<]+'|"[^"\r\n;{}()\\<]+")$/;
const LINE_BREAK = /[\r\n]/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isColor(value: string): boolean {
  if (HEX_COLOR.test(value)) return true;
  const match = RGB_COLOR.exec(value);
  if (match === null) return false;
  return [match[1], match[2], match[3]].every((channel) => Number(channel) <= 255);
}

function isLength(value: string, token: string): boolean {
  if (!LENGTH.test(value)) return false;
  return !value.startsWith("-") || token === TRACKING_TOKEN;
}

/**
 * A comma list of bare family names, closed-quoted names, or exactly
 * `var(--rs-font-brand)`. A quoted name containing a comma is refused (it splits
 * into two unterminated segments) — refusing is the safe direction.
 */
function isFontStack(value: string): boolean {
  if (LINE_BREAK.test(value)) return false;
  return value.split(",").every((raw) => {
    const segment = raw.trim();
    return (
      segment === FONT_BRAND_VAR || BARE_FAMILY.test(segment) || QUOTED_FAMILY.test(segment)
    );
  });
}

const GRAMMAR: Record<TokenKind, (value: string, token: string) => boolean> = {
  color: (value) => isColor(value),
  length: isLength,
  "font-stack": (value) => isFontStack(value),
};

function checkThemeFields(data: Record<string, unknown>, errors: ThemeError[]): void {
  for (const key of Object.keys(data)) {
    if (!THEME_KEYS.has(key)) errors.push({ token: null, reason: "unknown" });
  }
  const { id, name, description, scheme } = data;
  if (typeof id !== "string" || id === "") errors.push({ token: null, reason: "invalid-value" });
  if (typeof name !== "string" || name === "") {
    errors.push({ token: null, reason: "invalid-value" });
  }
  if (description !== undefined && typeof description !== "string") {
    errors.push({ token: null, reason: "invalid-value" });
  }
  // `scheme` compiles to `color-scheme`, so it is grammar-checked like a token.
  if (scheme !== "dark" && scheme !== "light") {
    errors.push({ token: null, reason: "invalid-value" });
  }
}

function checkToken(
  token: ContractToken,
  tokens: Record<string, unknown>,
  origin: ValidateOptions["origin"],
  errors: ThemeError[],
): void {
  const present = Object.hasOwn(tokens, token.name);
  if (token.reserved && origin === "third-party") {
    // Refused whatever the value: chrome belongs to built-in themes only.
    if (present) errors.push({ token: token.name, reason: "reserved" });
    return;
  }
  if (!present) {
    errors.push({ token: token.name, reason: "missing" });
    return;
  }
  const value = tokens[token.name];
  if (typeof value !== "string" || !GRAMMAR[token.kind](value, token.name)) {
    errors.push({ token: token.name, reason: "invalid-value" });
  }
}

/**
 * Decide whether `data` is a v1 theme. This is the CSS-injection guard: the
 * compiled output lands in a `<style>` element and values are never escaped,
 * only grammar-checked here.
 */
export function validateTheme(data: unknown, options: ValidateOptions): ThemeValidation {
  if (!isRecord(data) || data.contract !== CONTRACT_VERSION) {
    return { ok: false, errors: [{ token: null, reason: "contract-version" }] };
  }

  const errors: ThemeError[] = [];
  checkThemeFields(data, errors);

  const { tokens } = data;
  if (!isRecord(tokens)) {
    errors.push({ token: null, reason: "invalid-value" });
    return { ok: false, errors };
  }

  for (const token of CONTRACT_V1) checkToken(token, tokens, options.origin, errors);

  const known = new Set(CONTRACT_V1.map((token) => token.name));
  for (const key of Object.keys(tokens)) {
    if (!known.has(key)) errors.push({ token: key, reason: "unknown" });
  }

  return errors.length === 0 ? { ok: true } : { ok: false, errors };
}
