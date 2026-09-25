import contractV1 from "./contract-v1.json";

/** The value grammar a token's value is checked against. */
export type TokenKind = "color" | "length" | "font-stack";

export interface ContractToken {
  name: string;
  kind: TokenKind;
  /** Set only on the `chrome.*` tokens: built-in themes carry them, nothing else may. */
  reserved?: boolean;
}

/** The only contract version this package validates and compiles. */
export const CONTRACT_VERSION = 1;

/**
 * Theme contract v1, in `contract-v1.json` order. The JSON import widens `kind`
 * to `string`; `contract.test.ts` pins every entry's kind to one of `TokenKind`,
 * which is what makes this narrowing safe.
 */
export const CONTRACT_V1: readonly ContractToken[] = contractV1 as readonly ContractToken[];

export type ThemeScheme = "dark" | "light";

/** A theme is data. `validateTheme` decides whether an unknown value is one. */
export interface ThemeFile {
  contract: typeof CONTRACT_VERSION;
  id: string;
  name: string;
  description?: string;
  scheme: ThemeScheme;
  tokens: Readonly<Record<string, string>>;
}

/** `a.b-c` -> `--rs-a-b-c`. */
export function cssPropertyName(token: string): string {
  return `--rs-${token.replaceAll(".", "-")}`;
}
