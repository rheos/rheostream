// Test support only: not reachable through package.json `exports`.
import { CONTRACT_V1, type ThemeFile, type TokenKind } from "./contract";

const SAMPLE: Record<TokenKind, string> = {
  color: "#123456",
  length: "4px",
  "font-stack": "system-ui, sans-serif",
};

/** A complete, valid built-in theme: every contract token, chrome included. */
export function builtInTheme(overrides: Record<string, string> = {}): ThemeFile {
  const tokens: Record<string, string> = {};
  for (const token of CONTRACT_V1) tokens[token.name] = SAMPLE[token.kind];
  return {
    contract: 1,
    id: "sample-built-in",
    name: "Sample built-in",
    scheme: "dark",
    tokens: { ...tokens, ...overrides },
  };
}

/** A complete, valid third-party theme: every non-chrome token, no chrome. */
export function thirdPartyTheme(overrides: Record<string, string> = {}): ThemeFile {
  const tokens: Record<string, string> = {};
  for (const token of CONTRACT_V1) {
    if (!token.reserved) tokens[token.name] = SAMPLE[token.kind];
  }
  return {
    contract: 1,
    id: "sample-third-party",
    name: "Sample third-party",
    scheme: "light",
    tokens: { ...tokens, ...overrides },
  };
}
