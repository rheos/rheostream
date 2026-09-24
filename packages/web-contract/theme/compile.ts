import { CONTRACT_V1, cssPropertyName, type ThemeFile } from "./contract";

function declaration(token: string, value: string | undefined): string {
  if (value === undefined) {
    // Only reachable when the caller skipped validateTheme; failing loudly beats
    // shipping a stylesheet with a silently absent custom property.
    throw new Error(`theme token ${token} has no value`);
  }
  return `  ${cssPropertyName(token)}: ${value};`;
}

/**
 * The `<style>` element's text for `base` (a built-in theme, which the caller has
 * already validated with `origin: "built-in"`), with `overlay` layered over every
 * non-chrome token when given.
 *
 * The output is two `:root` rules, and their order is a security property:
 *
 * 1. The chrome rule comes first, in its own already-closed rule, and its values
 *    are read from `base.tokens` only. There is no code path here that reads a
 *    `chrome.*` key off `overlay`, so even a theme that got past validateTheme
 *    could not change the confirmation chrome.
 * 2. Every other token follows in a second rule, plus `color-scheme`.
 *
 * validateTheme's grammar check is the first line of defence against CSS
 * injection. This ordering is the second: a malformed overlay value that somehow
 * ran to end-of-input (an unterminated string, say) can only corrupt what comes
 * after it, which is the second rule. The browser has finished parsing the
 * chrome rule by then, so nothing later can reopen or swallow it.
 */
export function compileTheme(base: ThemeFile, overlay?: ThemeFile): string {
  const chrome: string[] = [];
  const rest: string[] = [];
  for (const token of CONTRACT_V1) {
    if (token.reserved) {
      chrome.push(declaration(token.name, base.tokens[token.name]));
    } else {
      rest.push(
        declaration(token.name, overlay?.tokens[token.name] ?? base.tokens[token.name]),
      );
    }
  }
  rest.push(`  color-scheme: ${overlay?.scheme ?? base.scheme};`);
  return [":root {", ...chrome, "}", ":root {", ...rest, "}", ""].join("\n");
}
