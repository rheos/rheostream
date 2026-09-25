import { CONTRACT_V1, cssPropertyName, type ContractToken, type ThemeFile } from "./contract";
import { isThemeScheme, isValidTokenValue } from "./validate";

function declaration(token: ContractToken, value: string | undefined): string {
  if (value === undefined) {
    // Only reachable when the caller skipped validateTheme; failing loudly beats
    // shipping a stylesheet with a silently absent custom property.
    throw new Error(`theme token ${token.name} has no value`);
  }
  if (!isValidTokenValue(token, value)) {
    throw new Error(`theme token ${token.name} has a value outside its ${token.kind} grammar`);
  }
  return `  ${cssPropertyName(token.name)}: ${value};`;
}

/**
 * The `<style>` element's text for `base` (a built-in theme, which the caller has
 * already validated with `origin: "built-in"`), with `overlay` layered over every
 * non-chrome token when given.
 *
 * Two defences keep a theme from reaching the confirmation chrome, and neither
 * depends on the caller having run validateTheme:
 *
 * 1. Grammar. Every value emitted here, from `base` and `overlay` alike, and the
 *    `color-scheme` value, is re-checked against the same grammar validateTheme
 *    uses (`isValidTokenValue`, `isThemeScheme`), and a mismatch throws. None of
 *    the grammars admits `;`, `{` or `}`, so no value can end its declaration
 *    and add another, such as a second `--rs-chrome-*` in the later rule.
 * 2. Source. The chrome values are read from `base.tokens` only. There is no
 *    code path here that reads a `chrome.*` key off `overlay`, so a theme cannot
 *    set chrome by carrying chrome keys either.
 *
 * The output is two `:root` rules, chrome first in its own rule, then every
 * other token plus `color-scheme`. The ordering is layout, not a defence on its
 * own: a later rule's redeclaration of a custom property wins, which is why the
 * grammar re-check above exists.
 */
export function compileTheme(base: ThemeFile, overlay?: ThemeFile): string {
  const chrome: string[] = [];
  const rest: string[] = [];
  for (const token of CONTRACT_V1) {
    if (token.reserved) {
      chrome.push(declaration(token, base.tokens[token.name]));
    } else {
      rest.push(declaration(token, overlay?.tokens[token.name] ?? base.tokens[token.name]));
    }
  }
  const scheme: unknown = overlay?.scheme ?? base.scheme;
  if (!isThemeScheme(scheme)) {
    throw new Error("theme scheme is neither dark nor light");
  }
  rest.push(`  color-scheme: ${scheme};`);
  return [":root {", ...chrome, "}", ":root {", ...rest, "}", ""].join("\n");
}
