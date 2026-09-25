import {
  CONTRACT_V1,
  CONTRACT_VERSION,
  type ContractToken,
  type ThemeFile,
  type ThemeScheme,
} from "./contract";

export type ThemeErrorReason =
  | "contract-version"
  | "missing"
  | "unknown"
  | "invalid-value"
  | "reserved";

/**
 * `token` names the contract token at fault. For a theme-level problem `token` is
 * `null` and `field` names the top-level key instead (`contract`, `id`, `name`,
 * `description`, `scheme`, `tokens`, or the unknown key itself).
 */
export interface ThemeError {
  token: string | null;
  field?: string;
  reason: ThemeErrorReason;
}

/** On success, `theme` is a fresh, typed copy holding only the checked fields. */
export type ThemeValidation =
  | { ok: true; theme: ThemeFile }
  | { ok: false; errors: ThemeError[] };

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
// Whole-value and case-sensitive, the contract's pattern with its `\s` narrowed to
// CSS whitespace (space, tab, LF, CR, FF). JavaScript's `\s` also matches NBSP,
// U+2028 and other Unicode spaces that CSS does not treat as whitespace. The alpha
// alternative `0?\.\d+` admits `.10`, the form the seed palette is written in.
const RGB_COLOR =
  /^rgba?\([ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*,[ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*,[ \t\n\r\f]*(\d{1,3})[ \t\n\r\f]*(?:,[ \t\n\r\f]*(0|1|0?\.\d+|1\.0+)[ \t\n\r\f]*)?\)$/;
const LENGTH = /^-?\d+(\.\d+)?(px|rem|em)$/;

const FONT_BRAND_VAR = "var(--rs-font-brand)";
const BARE_FAMILY = /^[A-Za-z0-9_-]+(?: [A-Za-z0-9_-]+)*$/;
// A closed quote on both ends, and inside it no line break, no copy of the
// opening quote and none of `; { } ( ) \ <`.
const QUOTED_FAMILY = /^(?:'[^'\r\n\f;{}()\\<]+'|"[^"\r\n\f;{}()\\<]+")$/;
// CSS counts form feed as a newline too: unescaped inside a string, it ends the
// string as a bad-string token, the same hazard as `\n` or `\r`.
const LINE_BREAK = /[\r\n\f]/;
// Space and tab only around a font-stack comma. String.prototype.trim would also
// strip Unicode spaces CSS does not treat as whitespace.
const SEGMENT_PADDING = /^[ \t]+|[ \t]+$/g;

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
 * into two unterminated segments), which is the safe direction.
 */
function isFontStack(value: string): boolean {
  if (LINE_BREAK.test(value)) return false;
  return value.split(",").every((raw) => {
    const segment = raw.replace(SEGMENT_PADDING, "");
    return (
      segment === FONT_BRAND_VAR || BARE_FAMILY.test(segment) || QUOTED_FAMILY.test(segment)
    );
  });
}

/**
 * Whether `value` fits `token`'s value grammar. The one grammar source: both
 * validateTheme and compileTheme check values through it.
 */
export function isValidTokenValue(token: ContractToken, value: string): boolean {
  switch (token.kind) {
    case "color":
      return isColor(value);
    case "length":
      return isLength(value, token.name);
    case "font-stack":
      return isFontStack(value);
  }
}

/** `scheme` compiles to `color-scheme`, so it is grammar-checked like a token. */
export function isThemeScheme(value: unknown): value is ThemeScheme {
  return value === "dark" || value === "light";
}

function requiredString(
  data: Record<string, unknown>,
  field: string,
  errors: ThemeError[],
): string | undefined {
  const value = data[field];
  if (typeof value === "string" && value !== "") return value;
  errors.push({ token: null, field, reason: "invalid-value" });
  return undefined;
}

function checkToken(
  token: ContractToken,
  tokens: Record<string, unknown>,
  origin: ValidateOptions["origin"],
  errors: ThemeError[],
  checked: Record<string, string>,
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
  if (typeof value !== "string" || !isValidTokenValue(token, value)) {
    errors.push({ token: token.name, reason: "invalid-value" });
    return;
  }
  checked[token.name] = value;
}

/**
 * Decide whether `data` is a v1 theme. This is the first CSS-injection guard: the
 * compiled output lands in a `<style>` element and values are never escaped,
 * only grammar-checked (compileTheme re-checks them as the second).
 */
export function validateTheme(data: unknown, options: ValidateOptions): ThemeValidation {
  if (!isRecord(data) || data.contract !== CONTRACT_VERSION) {
    return {
      ok: false,
      errors: [{ token: null, field: "contract", reason: "contract-version" }],
    };
  }

  const errors: ThemeError[] = [];
  for (const key of Object.keys(data)) {
    if (!THEME_KEYS.has(key)) errors.push({ token: null, field: key, reason: "unknown" });
  }
  const id = requiredString(data, "id", errors);
  const name = requiredString(data, "name", errors);
  const { description, scheme, tokens } = data;
  if (description !== undefined && typeof description !== "string") {
    errors.push({ token: null, field: "description", reason: "invalid-value" });
  }
  if (!isThemeScheme(scheme)) {
    errors.push({ token: null, field: "scheme", reason: "invalid-value" });
  }
  if (!isRecord(tokens)) {
    errors.push({ token: null, field: "tokens", reason: "invalid-value" });
    return { ok: false, errors };
  }

  const checked: Record<string, string> = {};
  for (const token of CONTRACT_V1) checkToken(token, tokens, options.origin, errors, checked);

  const known = new Set(CONTRACT_V1.map((token) => token.name));
  for (const key of Object.keys(tokens)) {
    if (!known.has(key)) errors.push({ token: key, reason: "unknown" });
  }

  if (errors.length > 0 || id === undefined || name === undefined || !isThemeScheme(scheme)) {
    return { ok: false, errors };
  }
  const theme: ThemeFile = { contract: CONTRACT_VERSION, id, name, scheme, tokens: checked };
  if (typeof description === "string") theme.description = description;
  return { ok: true, theme };
}
